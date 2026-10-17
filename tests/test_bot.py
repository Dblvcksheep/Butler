"""Drives the real Telegram handlers in-process through python-telegram-bot with a stubbed Bot API transport."""
import asyncio
import json
import re

import httpx
import pytest
from telegram import Update
from telegram.request import BaseRequest

from butler.config import Settings
from butler.payments.paypal import PayPalClient
from butler.telegram_bot import build


class Stub(BaseRequest):
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return 1

    async def initialize(self): pass
    async def shutdown(self): pass

    async def do_request(self, url, method, request_data=None, **kw):
        ep = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((ep, params))
        if ep == "getMe":
            res = {"id": 999, "is_bot": True, "first_name": "B", "username": "butler_bot"}
        elif ep in ("sendMessage", "editMessageText"):
            res = {"message_id": 5, "date": 0, "chat": {"id": 1, "type": "private"}, "text": params.get("text", "")}
        else:
            res = True
        return 200, json.dumps({"ok": True, "result": res}).encode()


class Harness:
    def __init__(self, rt):
        self.rt, self.stub, self.n = rt, Stub(), 0
        self.settings = Settings(telegram_token="123:ABC", allowed_users=frozenset({"1", "3"}),
                                 public_url="https://butler.test", worker_mode="celery", paypal_env="sandbox")
        self.app = build(self.settings, rt, request=self.stub)
        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(self.app.initialize())

    def _user(self, uid):
        return {"id": uid, "is_bot": False, "first_name": "T"}

    def _msg(self, uid, text, mid=1):
        m = {"message_id": mid, "date": 0, "chat": {"id": uid, "type": "private"}, "from": self._user(uid), "text": text}
        if text.startswith("/"):
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return m

    def _feed(self, update):
        self.stub.calls.clear()
        self.loop.run_until_complete(self.app.process_update(Update.de_json(update, self.app.bot)))
        return self.out()

    def send(self, text, uid=1):
        self.n += 1
        return self._feed({"update_id": self.n, "message": self._msg(uid, text)})

    def press(self, data, uid=1):
        self.n += 1
        return self._feed({"update_id": self.n, "callback_query": {
            "id": f"c{self.n}", "from": self._user(uid), "chat_instance": "x", "data": data,
            "message": self._msg(uid, "prompt", mid=7)}})

    def out(self):
        return [p.get("text", "") for ep, p in self.stub.calls if ep in ("sendMessage", "editMessageText")]

    def buttons(self):
        for ep, p in self.stub.calls:
            if "reply_markup" in p:
                rm = p["reply_markup"]
                rm = json.loads(rm) if isinstance(rm, str) else rm
                return [b["callback_data"] for row in rm["inline_keyboard"] for b in row]
        return []

    def endpoints(self):
        return [ep for ep, _ in self.stub.calls]


@pytest.fixture
def bot(rt):
    h = Harness(rt)
    yield h
    h.loop.run_until_complete(h.app.shutdown())
    h.loop.close()


def log_lines(rt):
    return rt.fake_log.read_text().splitlines() if rt.fake_log.exists() else []


PLAN_ORDER = json.dumps({"steps": [
    {"id": "s1", "agent": "operator", "kind": "tool", "capability": "fake.orders.create",
     "args": {"amount": "20", "currency": "USD"}, "depends_on": []}]})
PLAN_LLM = json.dumps({"steps": [{"id": "s1", "agent": "writer", "kind": "llm", "prompt": "write hi"}]})


def test_unauthorized_user_is_refused_and_nothing_runs(bot):
    assert bot.send("/start", uid=2) == ["Not authorized."]
    assert bot.send("hello", uid=2) == ["Not authorized."]
    assert bot.rt.db.execute("SELECT COUNT(*) AS n FROM llm_calls").fetchone()["n"] == 0


def test_start_without_any_model_prompts_for_a_key_and_chat_is_blocked(bot):
    assert "You have no model yet" in bot.send("/start", uid=3)[0]
    assert "at least one model key" in bot.send("hello", uid=3)[0]
    assert "at least one model key" in bot.send("/task daily 09:00 | x", uid=3)[0]
    assert "at least one model key" in bot.send("/do something", uid=3)[0]


def test_addkey_issues_a_single_use_https_link_and_never_asks_for_the_key_in_chat(bot):
    out = bot.send("/addkey qwen", uid=3)[0]
    m = re.search(r"https://butler\.test/setup/([\w\-]+)\?provider=qwen", out)
    assert m and bot.rt.creds.peek(m.group(1)) == ("3", "llm")
    assert "Error: provider must be one of" in bot.send("/addkey nonsense")[0]


def test_pasted_api_key_is_deleted_and_never_reaches_a_model(bot):
    out = bot.send("here: sk-abcdefghijklmnopqrstuvwxyz123456")
    assert "deleteMessage" in bot.endpoints() and "revoke" in out[0]
    assert bot.rt.db.execute("SELECT COUNT(*) AS n FROM llm_calls").fetchone()["n"] == 0


def test_chat_uses_graph_preferences_and_reports_trace(bot):
    assert bot.send("/remember email tone: natural, no filler") == ["Saved."]
    assert "email tone: natural" in bot.send("/prefs")[0]
    assert bot.send("hello there")[0].startswith("LLM:hello there")
    assert "HAS_PREFERENCE]->(email tone" in bot.send("/trace")[0]
    bot.send("/remember email tone: formal")
    assert "Replaced: natural, no filler" in bot.send("/why email tone")[0]
    assert bot.send("/forget email tone") == ["Deleted 2 record(s)."]
    assert bot.send("/prefs") == ["Nothing stored yet."]


def test_models_usage_price_budget_commands(bot):
    bot.send("hi")
    assert "anthropic / m1" in bot.send("/models")[0]
    assert "Price saved" in bot.send("/price 1 2 10")[0] and "$2.0/10.0" in bot.send("/models")[0]
    assert "1 calls" in bot.send("/usage")[0]
    bot.send("/budget tokens 5")
    assert "Daily token budget reached" in bot.send("hello again")[0]
    bot.send("/budget off")
    assert bot.send("hello again")[0].startswith("LLM:")
    assert "Order saved" in bot.send("/modelorder code anthropic")[0]
    assert "Error" in bot.send("/modelorder bogus anthropic")[0]


def test_connect_lists_and_revokes_tools(bot):
    assert "Fake connected" in bot.send("/connect fake")[-1]
    tools = bot.send("/tools")[0]
    assert "mcp.fake.create_order" in tools and "approval" in tools and "financial" in tools
    bot.send("/disconnect fake")
    assert "No tools connected" in bot.send("/tools")[0]


def test_plan_then_confirm_creates_task_and_pause_resume_cancel(bot):
    bot.send("/connect fake")
    bot.rt.script.append(PLAN_LLM)
    out = bot.send("/plan daily 09:00 tz=Africa/Lagos | say hi every morning")
    assert "Plan (written by anthropic" in out[0] and "[writer]" in out[0]
    ok, no = bot.buttons()
    assert "Task" in bot.press(ok)[0] and "created" in bot.out()[0]
    assert "expired" in bot.press(ok)[0]                          # draft is single-use
    tid = re.search(r"^(\w{8}) \[scheduled\]", bot.send("/tasks")[0]).group(1)
    assert "paused" in bot.send(f"/pause {tid}")[0] and "scheduled" in bot.send(f"/resume {tid}")[0]
    assert "cancelled" in bot.send(f"/cancel {tid}")[0]
    assert "Error" in bot.send(f"/resume {tid}")[0]               # cancelled is terminal
    assert "Error" in bot.send("/task sometimes | x")[0]
    bot.rt.script.append(PLAN_LLM)
    bot.send("/plan every 30m | x")
    assert "Discarded" in bot.press(bot.buttons()[1])[0]


def test_do_with_risky_tool_asks_approval_then_runs_exactly_once(bot):
    rt = bot.rt
    bot.send("/connect fake")
    rt.script.append(PLAN_ORDER)
    out = bot.send("/do order twenty dollars of widgets")
    assert "Running task" in out[0] and "needs your approval" in out[0] and log_lines(rt) == []
    tap = next(n[2][0][1] for n in rt.notes if n[2] and n[2][0][1].startswith("tap:"))
    assert bot.press(tap, uid=3) == ["Error: not your approval"]   # another allowed user can't approve it
    assert log_lines(rt) == []
    out = bot.press(tap)
    assert any("ORDER-OK:20USD" in t for t in out) and log_lines(rt) == ["create_order 20 USD"]
    assert any("via anthropic" in n[1] or "(tools)" in n[1] for n in rt.notes if "Task" in n[1])   # run resumed + finished
    assert any("Error" in t for t in bot.press(tap))              # double tap
    assert log_lines(rt) == ["create_order 20 USD"]


def test_rejecting_a_tool_approval_runs_nothing(bot):
    rt = bot.rt
    bot.send("/connect fake")
    rt.script.append(PLAN_ORDER)
    bot.send("/do order")
    rej = next(n[2][1][1] for n in rt.notes if n[2] and n[2][1][1].startswith("tnr:"))
    assert "Rejected" in bot.press(rej)[0]
    assert log_lines(rt) == [] and any("stopped" in n[1] for n in rt.notes)


def test_pay_requires_paypal_connection_then_approval_flow_never_overpays(bot):
    out = bot.send("/pay 20 USD a@b.co design work")
    assert "Confirm payout" in out[0]
    ok, no = bot.buttons()
    assert "PayPal isn't connected" in bot.press(ok)[-1]          # no creds => nothing sent
    sent = []

    def h(req):
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t"})
        sent.append(req.headers["PayPal-Request-Id"])
        return httpx.Response(201, json={"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}})

    bot.rt.payments._pp = PayPalClient("i", "s", transport=httpx.MockTransport(h))
    bot.send("/pay 20 USD a@b.co design work")
    ok2 = bot.buttons()[0]
    assert "not confirmed yet" in bot.press(ok2)[-1]               # accepted != paid
    assert len(sent) == 1
    assert "already" in bot.press("no:" + ok2.split(":")[1])[0]     # can't "cancel" a payment that was submitted
    assert "Error" in bot.send("/pay 999 USD a@b.co big")[0]       # over the per-transaction limit
    assert bot.press(ok2, uid=2) == []                             # not on the allow-list


def test_another_allowed_user_cannot_approve_or_cancel_my_payment(bot):
    bot.send("/pay 20 USD a@b.co design")
    ok = bot.buttons()[0]
    assert bot.press(ok, uid=3) == ["Error: not your payment"]
    assert bot.press("no:" + ok.split(":")[1], uid=3) == ["Error: not your payment"]
    assert bot.rt.db.execute("SELECT status FROM intents").fetchone()["status"] == "awaiting_approval"


def test_graph_commands_blocked_impact_why_and_guarded_disconnect(bot):
    rt = bot.rt
    bot.send("/connect fake")
    step = [{"id": "s1", "agent": "researcher", "kind": "tool", "capability": "data.search",
             "tool_id": "mcp.fake.list_items", "args": {"query": "x"}, "depends_on": []}]
    t = rt.create_task("1", step, "daily 09:00", objective="check items")
    d = rt.create_task("1", [{"id": "s1", "agent": "writer", "kind": "llm", "prompt": "report", "depends_on": []}],
                       f"after {t['id']}", objective="report on items")
    out = bot.send("/impact fake")[0]
    assert t["id"] in out and d["id"] in out
    guard = bot.send("/disconnect fake")[0]                        # graph query runs BEFORE anything is revoked
    assert "would break 1 task" in guard and "/disconnect fake confirm" in guard
    assert "mcp.fake.list_items" in bot.send("/tools")[0]          # still connected
    assert f"{d['id']} (report on items) is waiting on: {t['id']} [pending]" in bot.send("/blocked")[0]
    why = bot.send(f"/why task {t['id']}")[0]
    assert "needs data.search -> chose mcp.fake.list_items" in why and "graph ranking now" in why
    assert "Waits for" in bot.send(f"/why task {d['id']}")[0]
    cancel = bot.send(f"/cancel {t['id']}")[0]
    assert "will not run while it is cancelled" in cancel and d["id"] in cancel
    assert "root cause: " + t["id"] + " cancelled" in bot.send("/blocked")[0]
    assert "No runs recorded" in bot.send(f"/history {t['id']}")[0]
    assert "Disconnected" in bot.send("/disconnect fake")[0]       # nothing live uses it any more
    assert "Error" in bot.send("/disconnect")[0] and "Error" in bot.send("/impact")[0]


def test_task_command_with_two_parents_registers_graph_dependencies(bot):
    rt = bot.rt
    a = rt.create_task("1", [{"id": "s1", "agent": "assistant", "kind": "llm", "prompt": "a", "depends_on": []}],
                       "daily 09:00", objective="A")
    b = rt.create_task("1", [{"id": "s1", "agent": "assistant", "kind": "llm", "prompt": "b", "depends_on": []}],
                       "daily 10:00", objective="B")
    out = bot.send(f"/task after {a['id']},{b['id']} | combine A and B")[0]
    assert "scheduled" in out and "upstream" in out
    c_id = re.search(r"Task (\w{8})", out).group(1)
    assert {p["id"] for p in rt.orch.memory("1").blocking_parents(c_id)} == {a["id"], b["id"]}
    assert "Error" in bot.send("/task after deadbeef | x")[0]


# ---------- job-outreach flow through the real handlers ----------
from test_integrations import draft, setup_all, smtp  # noqa: E402,F401  (smtp is a fixture)


def test_connect_email_and_search_flow(bot):
    rt = bot.rt
    out = bot.send("/connect email")[0]
    tok = re.search(r"https://butler\.test/setup/([\w\-]+)", out).group(1)
    assert rt.creds.peek(tok) == ("1", "email")                       # the email form, not the LLM form
    assert "usage: /connect" in bot.send("/connect nonsense")[0] and "email|search" in bot.send("/connect nonsense")[0]
    rt.creds.set_service("1", "email", {"host": "127.0.0.1", "port": 2525, "security": "none",
                                        "from_addr": "me@example.org", "from_name": "", "username": "", "password": ""})
    assert "Email (SMTP) connected: 1 tool(s)" in bot.send("/connect email")[-1]
    tools = bot.send("/tools")[0]
    assert "email.send_batch" in tools and "approval" in tools
    stok = re.search(r"/setup/([\w\-]+)", bot.send("/connect search")[0]).group(1)   # no key yet: a secure link instead
    assert rt.creds.peek(stok) == ("1", "search")
    bot.send("/disconnect email")
    assert rt.creds.get_service("1", "email") is None and "email.send_batch" not in bot.send("/tools")[0]


def test_do_find_companies_and_email_them_end_to_end(bot, smtp):
    rt = bot.rt
    setup_all(rt, smtp)
    plan = json.dumps({"steps": [
        {"id": "s1", "agent": "researcher", "kind": "tool", "capability": "web.search", "args": {"query": "acme python jobs"}},
        {"id": "s2", "agent": "researcher", "kind": "tool", "capability": "web.fetch",
         "args": {"url": "https://acme.test/contact"}, "depends_on": ["s1"]},
        {"id": "s3", "agent": "writer", "kind": "llm", "prompt": "draft emails as JSON only", "depends_on": ["s2"]},
        {"id": "s4", "agent": "operator", "kind": "tool", "capability": "email.send", "args": {"messages": "{{s3}}"},
         "depends_on": ["s3"]}]})
    rt.script += [plan, draft(("hello@acme.test", "Acme"), ("made.up@acme.test", "Acme"))]
    out = bot.send("/do find python jobs at Acme and email them")
    assert "web.search" in out[0] and "web.fetch" in out[0] and "needs your approval" in out[0]
    assert smtp.messages == []                                          # nothing sent before approval
    preview = [n[1] for n in rt.notes if n[2]][-1]
    assert "hello@acme.test" in preview and "skipped made.up@acme.test" in preview
    tap = [n[2][0][1] for n in rt.notes if n[2] and n[2][0][1].startswith("tap:")][-1]
    other = bot.press(tap, uid=3)
    assert any("not your approval" in t for t in other) and smtp.messages == []
    done = bot.press(tap)
    assert any("hello@acme.test" in t for t in done) and len(smtp.messages) == 1
    assert smtp.messages[0]["To"] == "hello@acme.test"
    assert any("Task" in n[1] and "via" in n[1] for n in rt.notes)       # the run resumed and finished
    assert "Acme" in " ".join(rt.orch.memory("1").contacted_batch(["Acme"]).get("acme", {}).get("name", "") for _ in [0])
