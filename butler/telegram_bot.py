import asyncio
import logging
import re
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from .cli import NVIDIA
from .config import Settings
from .credentials import TASK_KINDS, CredentialError
from .llm.provider import PROVIDERS
from .llm.router import AllModelsFailed, BudgetExceeded
from .payments.intents import PaymentError
from .planner import PlanError, describe_plan, make_plan
from .runtime import Runtime, build_runtime
from .tasks.store import TaskError
from .tools.executor import ToolArgError, ToolDenied
from .tools.store import ApprovalError

log = logging.getLogger("butler")
KEY_LIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{16,}|sk-ant-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{20,})")
NEED_MODEL = ("I need at least one model key to think with. Every provider (Claude, GPT, Nebius, Qwen) is "
              "optional, but add one with /addkey.")


def build(settings: Settings, rt: Runtime | None = None, request=None) -> Application:
    rt = rt or build_runtime(settings)
    builder = Application.builder().token(settings.telegram_token)
    if request is not None:                       # tests inject a stub transport; production uses the default
        builder = builder.request(request).get_updates_request(request)
    app = builder.build()
    uid = lambda u: str(u.effective_user.id)
    # allowed = lambda u: bool(u.effective_user) and str(u.effective_user.id) in settings.allowed_users

    async def reply(u: Update, text: str, **kw):
        await u.effective_message.reply_text(text[:4000], **kw)

    def need_model(u):
        if not rt.creds.has_active_model(uid(u)):
            raise ValueError(NEED_MODEL)

    def guard(fn):
        async def wrapped(u: Update, c: ContextTypes.DEFAULT_TYPE):
            # if not allowed(u):
            #     return await reply(u, "Not authorized.")
            try:
                await fn(u, c)
            except (AllModelsFailed, BudgetExceeded, PlanError) as e:
                await reply(u, f"Couldn't do that: {e}")
            except (PaymentError, ValueError, CredentialError, TaskError, ToolDenied, ToolArgError, ApprovalError) as e:
                await reply(u, f"Error: {e}")
            except Exception:
                log.exception("handler failed")
                await reply(u, "Something went wrong. It's been logged.")
            except IndexError:                      # a handler read a missing argument: log it, but be friendly
                log.exception("handler missing argument")
                await reply(u, "A required argument is missing. Send /start to see how each command is used.")
        return wrapped

    # ---------- onboarding / models ----------
    async def start(u, c):
        n = len(rt.creds.list_creds(uid(u)))
        first = ("You have no model yet. Add at least one with /addkey (Claude, GPT, Nebius or Qwen; all optional, "
                 "but I need one).\n\n" if not n else "")
        await reply(u, first + "Models: /addkey [provider] /models /removekey id /modelorder kind p1,p2 /price id in out\n"
                    "Spend: /usage  /budget tokens N | cost X | off\n"
                    "Tools: /connect paypal|email|search  /disconnect svc  /tools\n"
                    "Work: /do goal   /plan <schedule> | goal   /task <schedule> | step | step   /tasks /pause /resume /cancel\n"
                    "Graph: /blocked  /impact <service>  /why task <id>  /history <id>   (graph can be turned off: BUTLER_GRAPH=off)\n"
                    "  schedule: now | after <id>[,<id>..] | daily 09:00 [tz=Area/City] | weekly mon 09:00 | every 30m | once 2026-10-20T09:00\n"
                    "Memory: /remember key: value [| scope]  /forget key  /prefs  /why key  /trace  /contact  /contacted\n"
                    "Payments: /pay 20 USD name@example.com note  /status id\n\n"
                    "Never paste API keys here; /addkey gives you a secure one-time link.")

    async def link(u, purpose, extra=""):
        if not settings.public_url.startswith("https://"):
            raise ValueError("BUTLER_PUBLIC_URL must be an https:// URL to serve the secure setup pages")
        tok = rt.creds.issue_token(uid(u), purpose=purpose)
        await reply(u, f"Open this link (single use, expires in 10 minutes):\n{settings.public_url}/setup/{tok}{extra}")

    async def addkey(u, c):
        prov = c.args[0].lower() if c.args else ""
        if prov and prov not in PROVIDERS:
            raise ValueError("provider must be one of: " + ", ".join(PROVIDERS))
        await link(u, "llm", f"?provider={prov}" if prov else "")

    async def models(u, c):
        rows = rt.creds.list_creds(uid(u))
        if not rows:
            return await reply(u, "No models yet. Use /addkey.")
        lines = []
        for r in rows:
            tag = " [NVIDIA]" if r["provider"] == "nebius" and NVIDIA.search(r["model"]) else ""
            price = f" ${r['price_in']}/{r['price_out']} per 1M" if r["price_in"] is not None else ""
            lines.append(f"{r['id']}. {r['provider']} / {r['model']}{tag}  ...{r['last4']}  [{r['status']}]{price}")
        orders = [f"{k}: {','.join(rt.creds.get_order(uid(u), k))}" for k in TASK_KINDS if rt.creds.get_order(uid(u), k)]
        await reply(u, "\n".join(lines) + ("\n\nOrder overrides:\n" + "\n".join(orders) if orders else "")
                    + "\n\nFallback goes down this list (or your /modelorder) when a model fails.")

    async def removekey(u, c):
        if len(c.args) != 1 or not c.args[0].isdigit():
            raise ValueError("usage: /removekey <key id>   (the number shown by /models)")

        cid = int(c.args[0])
        r = rt.creds.remove(uid(u), cid)
        if not r:
            raise ValueError("no such key")
        rt.orch.memory(uid(u)).remove_credential_ref(cid)
        await reply(u, f"Removed {r['provider']} / {r['model']}.")

    async def modelorder(u, c):
        if len(c.args) < 2:
            raise ValueError(f"usage: /modelorder <{'|'.join(TASK_KINDS)}> provider1,provider2")
        rt.creds.set_order(uid(u), c.args[0].lower(), [p.strip().lower() for p in c.args[1].split(",")])
        await reply(u, "Order saved.")

    async def price(u, c):
        usage_msg = "usage: /price <key id> <USD per 1M input tokens> <USD per 1M output tokens>"
        try:
            ok = len(c.args) == 3 and rt.creds.set_price(uid(u), int(c.args[0]), float(c.args[1]), float(c.args[2]))
        except ValueError:                                  # not numbers
            ok = False
        if not ok:
            raise ValueError(usage_msg)
        await reply(u, "Price saved. Cost tracking uses prices you enter; I don't guess them.")


    async def usage(u, c):
        now = time.time()
        d, m = rt.creds.usage_since(uid(u), now - 86400), rt.creds.usage_since(uid(u), now - 30 * 86400)
        b = rt.creds.get_budget(uid(u))
        lines = [f"Last 24h: {d['calls']} calls, {d['tokens']} tokens, cost {d['cost']:.4f}"
                 + (f" (+{d['unpriced']} calls with no price set)" if d["unpriced"] else ""),
                 f"Last 30d: {m['calls']} calls, {m['tokens']} tokens, cost {m['cost']:.4f}"]
        lines += [f"  {r['provider']}/{r['model']}: {r['calls']} calls, {r['tokens']} tokens, {r['cost']:.4f}"
                  for r in rt.creds.usage_by_model(uid(u), now - 30 * 86400)]
        lines.append(f"Budget (daily): tokens={b['daily_tokens'] or 'off'}, cost={b['daily_cost'] or 'off'}")
        await reply(u, "\n".join(lines))

    async def budget(u, c):
        b = rt.creds.get_budget(uid(u))
        usage_msg = "usage: /budget tokens N | cost X | off"
        try:
            if c.args and c.args[0] == "off" and len(c.args) == 1:
                rt.creds.set_budget(uid(u), None, None)
            elif len(c.args) == 2 and c.args[0] == "tokens":
                rt.creds.set_budget(uid(u), int(c.args[1]), b["daily_cost"])
            elif len(c.args) == 2 and c.args[0] == "cost":
                rt.creds.set_budget(uid(u), b["daily_tokens"], float(c.args[1]))
            elif c.args:
                raise ValueError(usage_msg)
        except ValueError as e:
            raise ValueError(usage_msg if "usage" not in str(e) else str(e))
        b = rt.creds.get_budget(uid(u))
        await reply(u, f"Daily budget: tokens={b['daily_tokens'] or 'off'}, cost={b['daily_cost'] or 'off'} "
                       "(rolling 24h; cost only counts models with a /price set).")
    # ---------- tools ----------
    async def connect(u, c):
        svc = (c.args[0].lower() if c.args else "")
        known = rt.connectables()
        if svc not in known:
            raise ValueError("usage: /connect <" + "|".join(known) + ">")
        label, secret = known[svc]
        if secret and not rt.creds.get_service(uid(u), secret):
            return await link(u, svc)              # credentials first (secure page); registration runs after
        await reply(u, f"Connecting {label}...")
        res = await asyncio.to_thread(rt.connect_service, uid(u), svc)
        await reply(u, f"{label} connected: {len(res['tools'])} tool(s). /tools to see them."
                    + (f"\nHidden (not matched by a safety rule): {', '.join(res['hidden'][:10])}" if res["hidden"] else ""))

    async def disconnect(u, c):
        if not c.args:
            raise ValueError("usage: /disconnect <service> [confirm]")
        svc, confirm = c.args[0].lower(), len(c.args) > 1 and c.args[1].lower() == "confirm"
        imp = rt.orch.memory(uid(u)).impact(svc)               # graph query BEFORE revoking anything
        if imp and not confirm:
            return await reply(u, f"Disconnecting {svc} would break {len(imp)} task(s):\n" + "\n".join(
                f"- {x['id']} ({x['objective'][:40]})" + (f" and dependents {', '.join(x['downstream'])}" if x["downstream"] else "")
                for x in imp) + f"\nSend '/disconnect {svc} confirm' to proceed.")
        rt.disconnect_service(uid(u), svc)
        await reply(u, "Disconnected (stored credentials for it were deleted)."
                    + (" The graph is off, so I couldn't check which tasks this affects." if imp is None else ""))

    async def tools_cmd(u, c):
        out = []
        for svc in rt.tools.granted_services(uid(u)):
            for t in rt.tools.tools_for_service(svc):
                flags = f"risk={t['risk']}" + (", approval" if t["approval"] else "") + (", financial" if t["financial"] else "")
                out.append(f"{t['tool_id']}  [{t['capability']}]  {flags}")
        await reply(u, "\n".join(out) or "No tools connected. Try /connect paypal.")

    # ---------- memory ----------
    async def remember(u, c):
        body, _, scope = " ".join(c.args).partition("|")
        key, sep, value = body.partition(":")
        if not sep:
            raise ValueError("usage: /remember key: value [| scope]")
        rt.orch.memory(uid(u)).remember(key, value, scope.strip() or "global")
        await reply(u, "Saved.")

    async def forget(u, c):
        if not c.args:
            raise ValueError("usage: /forget <preference key>   (see /prefs)")
        await reply(u, f"Deleted {rt.orch.memory(uid(u)).forget(' '.join(c.args))} record(s).")

    async def prefs(u, c):
        p = rt.orch.memory(uid(u)).active(("global",) + tuple(a.lower() for a in c.args))
        await reply(u, "\n".join(f"- {x['key']}: {x['value']} [{x['scope']}, {x['source']}]" for x in p) or "Nothing stored yet.")

    async def why(u, c):
        if c.args and c.args[0].lower() == "task":
            if len(c.args) < 2:
                raise ValueError("usage: /why task <task_id>   (or /why <preference key>)")
            return await why_task(u, c.args[1])
        rows = rt.orch.memory(uid(u)).why(" ".join(c.args))
        if not rows:
            return await reply(u, "No active preference with that key.")
        r = rows[0]
        out = [f"Current: {r['value']} (source: {r['source']}, set {datetime.fromtimestamp(r['created']):%Y-%m-%d %H:%M})"]
        out += [f"Replaced: {x['replaced']}" for x in rows if x["replaced"]]
        await reply(u, "\n".join(out))

    async def trace(u, c):
        t = rt.orch.last_trace.get(uid(u))
        if not t:
            return await reply(u, "No chat yet to trace.")
        lines = [f"Answered by {t.provider}/{t.model}"]
        lines += [f"  skipped {a.provider}/{a.model}: {a.error}" for a in t.attempts if not a.ok]
        lines += (["Graph context used:"] + [f"  (User)-[:HAS_PREFERENCE]->({p['key']}: {p['value']}) [{p['scope']}]"
                                             for p in t.prefs]) if t.prefs else ["Graph context used: none"]
        await reply(u, "\n".join(lines))

    def company_name(c) -> str:
        name = " ".join(c.args).strip()
        if not re.search(r"[A-Za-z0-9]", name):             # an empty name would create a junk node in the graph
            raise ValueError("usage: /contact <company name>")
        return name

    async def contact(u, c):
        rt.orch.memory(uid(u)).record_contact(company_name(c))
        await reply(u, "Recorded as contacted.")

    async def contacted(u, c):
        t = rt.orch.memory(uid(u)).contact_trace(company_name(c))
        await reply(u, f"Yes: {t['company']} via {t['channel']} on {datetime.fromtimestamp(t['at']):%Y-%m-%d}."
                    if t else "No record of contacting them.")

    # ---------- payments (interactive, always human-approved) ----------
    async def pay_cmd(u, c):
        if len(c.args) < 3:
            raise ValueError("usage: /pay 20 USD name@example.com note...")
        amount, cur, email, *note = c.args
        i = rt.payments.prepare(uid(u), email, amount, cur, " ".join(note) or "Payment")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Approve", callback_data=f"ok:{i['id']}"),
                                    InlineKeyboardButton("Cancel", callback_data=f"no:{i['id']}")]])
        await reply(u, f"Confirm payout ({settings.paypal_env}):\n{i['amount']} {i['currency']} -> {i['recipient']}\n"
                       f"Note: {i['note']}", reply_markup=kb)

    async def status(u, c):
        if not c.args:
            raise ValueError("usage: /status <payment id>   (shown when you started the payment)")
        i = await asyncio.to_thread(rt.payments.reconcile, c.args[0], uid(u))
        await reply(u, f"{i['status']} (provider: {i['provider_status'] or 'n/a'})" + (f"\nerror: {i['error']}" if i["error"] else ""))

    # ---------- tasks, plans ----------
    def parse_head(head: str) -> tuple[str, str]:
        tz = "UTC"
        m = re.search(r"\btz=(\S+)", head)
        if m:
            tz, head = m.group(1), head.replace(m.group(0), "").strip()
        return head, tz

    def create_task(user_id, steps, schedule, tz, objective=None):
        return rt.create_task(user_id, steps, schedule, tz, objective=objective)

    def when(t):
        return ("when its upstream task completes" if t["next_run"] is None else
                datetime.fromtimestamp(t["next_run"]).astimezone().strftime("%Y-%m-%d %H:%M %Z"))

    async def task_cmd(u, c):
        need_model(u)
        head, *steps = [p.strip() for p in " ".join(c.args).split("|")]
        head, tz = parse_head(head)
        if not steps:
            raise ValueError("usage: /task <schedule> [tz=Area/City] | step 1 | step 2 ...  (or /plan to let me plan it)")
        t = create_task(uid(u), steps, head, tz)
        await reply(u, f"Task {t['id']} scheduled. Next run: {when(t)}. It keeps running after you leave this chat.")

    async def plan_cmd(u, c):
        need_model(u)
        rt.ensure_builtins(uid(u))
        head, _, goal = " ".join(c.args).partition("|")
        head, tz = parse_head(head.strip())
        if not goal.strip():
            raise ValueError("usage: /plan <schedule> [tz=Area/City] | goal")
        steps, res = await asyncio.to_thread(make_plan, rt.router, rt.orch.memory(uid(u)), rt.tools, uid(u), goal.strip())
        did = rt.tasks.save_draft(uid(u), goal.strip(), head, tz, steps)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Create task", callback_data=f"pl:ok:{did}"),
                                    InlineKeyboardButton("Discard", callback_data=f"pl:no:{did}")]])
        await reply(u, f"Plan (written by {res.provider}, validated by Butler):\n{describe_plan(steps, rt.tools)}\n\n"
                       f"Schedule: {head}", reply_markup=kb)

    async def do_cmd(u, c):
        need_model(u)
        rt.ensure_builtins(uid(u))
        goal = " ".join(c.args).strip()
        if not goal:
            raise ValueError("usage: /do goal")
        steps, res = await asyncio.to_thread(make_plan, rt.router, rt.orch.memory(uid(u)), rt.tools, uid(u), goal)
        t = create_task(uid(u), steps, "now", "UTC", objective=goal)
        await reply(u, f"Running task {t['id']} now:\n{describe_plan(steps, rt.tools)}\n(Anything risky will ask for your approval.)")
        await asyncio.to_thread(rt.worker.tick)

    async def tasks_cmd(u, c):
        rows = rt.tasks.list_tasks(uid(u))
        await reply(u, "\n".join(f"{t['id']} [{t['status']}] {t['sched_kind']} {t['sched_spec']} - {t['objective'][:50]}"
                                 for t in rows) or "No tasks.")

    def mk_state(to):
        async def h(u, c):
            if not c.args:
                raise ValueError("usage: /" + {"paused": "pause", "scheduled": "resume", "cancelled": "cancel"}[to] + " <task_id>")
            t = rt.tasks.set_status(c.args[0], uid(u), to)
            mem = rt.orch.memory(uid(u))
            mem.set_task_status(t["id"], status=to, last="cancelled" if to == "cancelled" else None)
            msg = f"Task {t['id']} is now {to}."
            if to in ("paused", "cancelled"):
                d = mem.downstream(t["id"])
                if d:
                    msg += "\nThese dependent tasks will not run while it is " + to + ": " + \
                           ", ".join(f"{x['id']} ({x['objective'][:30]})" for x in d)
            await reply(u, msg)
        return h

    # ---------- graph-driven views ----------
    GRAPH_OFF = "The graph is off (BUTLER_GRAPH=off), so this needs FalkorDB and isn't available."

    async def blocked_cmd(u, c):
        b = rt.orch.memory(uid(u)).blocked_tasks()
        if b is None:
            return await reply(u, GRAPH_OFF)
        if not b:
            return await reply(u, "Nothing is blocked.")
        lines = []
        for e in b:
            chain = ", ".join(f"{x['id']} [{x['status']}]" + ("" if x["depth"] == 1 else f" (via chain, depth {x['depth']})")
                              for x in e["blockers"])
            root = f"  root cause: {', '.join(x['id'] + ' ' + x['status'] for x in e['root_causes'])}" if e["root_causes"] else ""
            lines.append(f"{e['id']} ({e['objective'][:40]}) is waiting on: {chain}{root}")
        await reply(u, "\n".join(lines))

    async def impact_cmd(u, c):
        if not c.args:
            raise ValueError("usage: /impact <service>   e.g. /impact paypal")
        imp = rt.orch.memory(uid(u)).impact(c.args[0].lower())
        if imp is None:
            return await reply(u, GRAPH_OFF)
        if not imp:
            return await reply(u, f"No live tasks use {c.args[0]}.")
        await reply(u, f"Revoking {c.args[0]} would break:\n" + "\n".join(
            f"- {x['id']} ({x['objective'][:40]})" + (f"  and its dependents: {', '.join(x['downstream'])}" if x["downstream"] else "")
            for x in imp))

    async def history_cmd(u, c):
        if not c.args:
            raise ValueError("usage: /history <task_id>")
        mem = rt.orch.memory(uid(u))
        if not mem.enabled:
            return await reply(u, GRAPH_OFF)
        h = mem.task_history(c.args[0], 8)
        await reply(u, "\n".join(f"run {r['id']}: {r['status']} via {r['provider'] or '-'}/{r['model'] or '-'} "
                                 f"at {datetime.fromtimestamp(r['at']):%Y-%m-%d %H:%M}" for r in h) or "No runs recorded.")

    async def why_task(u, tid):
        mem = rt.orch.memory(uid(u))
        if not mem.enabled:
            return await reply(u, GRAPH_OFF)
        ex = mem.explain_task(tid)
        if not ex:
            return await reply(u, "No graph record for that task.")
        L = [f"Task {ex['id']} [{ex['status']}]: {ex['objective'][:80]}"]
        if ex["depends_on"]:
            L.append("Waits for: " + ", ".join(f"{d['id']} ({d['status']})" for d in ex["depends_on"]))
        L.append("Plan:")
        for s_ in ex["steps"]:
            line = f"  {s_['step']} [{s_['agent']}] {s_['kind']}" + (f" after {','.join(s_['after'])}" if s_["after"] else "")
            if s_["capability"]:
                line += f"\n      needs {s_['capability']} -> chose {s_['tool']}"
                if s_["candidates"]:
                    line += "\n      graph ranking now: " + " > ".join(
                        f"{x['tool_id']} (ok {x['ok']}/fail {x['fail']}, {x['risk']})" for x in s_["candidates"])
                    if s_["candidates"][0]["tool_id"] != s_["tool"]:
                        line += f"\n      note: the graph would now prefer {s_['candidates'][0]['tool_id']}"
            L.append(line)
        if ex["preferences"]:
            L.append("Preferences in force: " + "; ".join(f"{p['key']}={p['value']}" for p in ex["preferences"]))
        if ex["last_run"]:
            r = ex["last_run"]
            L.append(f"Last run {r['id']}: {r['status']} via {r['provider'] or '-'}/{r['model'] or '-'}")
            L += [f"  handoff: {a}:{sa} -> {b}:{sb}" for a, sa, b, sb in ex["handoffs"]]
        await reply(u, "\n".join(L))

    # ---------- button callbacks ----------
    async def button(u: Update, c):
        q = u.callback_query
        await q.answer()
        me = str(q.from_user.id)
        # if me not in settings.allowed_users:
        #     return
        kind, _, rest = q.data.partition(":")
        try:
            if kind == "no":                                    # payment cancel
                cur = rt.payments.store.owned(rest, me)
                if not rt.payments.store.transition(rest, ("awaiting_approval", "approved"), "cancelled"):
                    raise PaymentError(f"can't cancel: this payment is already {cur['status']}")
                return await q.edit_message_text("Cancelled.")
            if kind == "ok":                                    # payment approve (bound to exact details)
                rt.payments.store.approve(rest, me)
                await q.edit_message_text("Approved. Submitting...")
                i = await asyncio.to_thread(rt.payments.execute, rest, me)
                msg = {"succeeded": "Paid.", "processing": "Submitted to PayPal; not confirmed yet. /status " + rest,
                       "failed": f"Failed: {i['error']}",
                       "unresolved": "Outcome unknown (network issue). Not retried blindly. /status " + rest}
                return await q.message.reply_text(msg.get(i["status"], i["status"]))
            if kind == "tap":                                   # tool approval
                if rt.tools.get_approval(rest)["user_id"] != me:
                    raise ApprovalError("not your approval")     # check BEFORE telling anyone it was approved
                await q.edit_message_text("Approved. Running...")
                out = await asyncio.to_thread(rt.executor.approve_and_execute, rest, me)
                await q.message.reply_text(("Done:\n" if out.status == "ok" else "Problem: ") + out.text[:3000])
                return await asyncio.to_thread(rt.worker.tick)   # unblock the waiting task now
            if kind == "tnr":
                rt.executor.reject(rest, me)                    # owner check inside
                await q.edit_message_text("Rejected.")
                return await asyncio.to_thread(rt.worker.tick)
            if kind == "pl":
                act, _, did = rest.partition(":")
                d = rt.tasks.take_draft(did, me)
                if not d:
                    return await q.edit_message_text("That plan expired.")
                if act == "no":
                    return await q.edit_message_text("Discarded.")
                t = create_task(me, d["plan"], d["schedule"], d["tz"], objective=d["goal"])
                await q.edit_message_text(f"Task {t['id']} created. Next run: {when(t)}.")
        except (PaymentError, ApprovalError, TaskError, ValueError) as e:
            await q.message.reply_text(f"Error: {e}")

    async def chat(u, c):
        text = u.effective_message.text

        if KEY_LIKE.search(text):
            try:
                await u.effective_message.delete()
            except Exception:
                pass
            return await reply(
                u,
                "That looks like an API key. I removed the message, but treat that key as exposed: "
                "revoke it at the provider, then use /addkey for a secure link."
            )

        need_model(u)
        r = await asyncio.to_thread(rt.orch.chat, uid(u), text)

        failed = [a for a in r.attempts if not a.ok]
        response = (r.text or "").strip()

        if not response:
            response = "I couldn't generate a response. Please try again."
        elif failed:
            response += f"\n\n(via {r.provider}; {len(failed)} model(s) failed first)"

        await reply(u, response)

    for name, fn in [("start", start),("help", start), ("addkey", addkey), ("models", models), ("removekey", removekey),
                     ("modelorder", modelorder), ("price", price), ("usage", usage), ("budget", budget),
                     ("connect", connect), ("disconnect", disconnect), ("tools", tools_cmd),
                     ("remember", remember), ("forget", forget), ("prefs", prefs), ("why", why), ("trace", trace),
                     ("contact", contact), ("contacted", contacted), ("pay", pay_cmd), ("status", status),
                     ("blocked", blocked_cmd), ("impact", impact_cmd), ("history", history_cmd),
                     ("task", task_cmd), ("plan", plan_cmd), ("do", do_cmd), ("tasks", tasks_cmd),
                     ("pause", mk_state("paused")), ("resume", mk_state("scheduled")), ("cancel", mk_state("cancelled"))]:
        app.add_handler(CommandHandler(name, guard(fn)))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guard(chat)))

    async def post_init(application: Application):
        if settings.worker_mode != "inline":
            return                                              # Celery beat/worker do this instead

        async def loop_forever():
            while True:
                try:
                    await asyncio.to_thread(rt.worker.tick)
                except Exception:
                    log.exception("worker tick failed")
                await asyncio.sleep(settings.task_poll_seconds)
        application.create_task(loop_forever())

    app.post_init = post_init
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)       # httpx logs full URLs; keep tokens out of logs
    build(Settings.from_env()).run_polling()


if __name__ == "__main__":
    main()
