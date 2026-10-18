"""email.send_batch: plain-text email over the user's own SMTP account, with the safety checks an unattended
agent needs. Nothing is sent without a human approval of the exact batch.

prepare() (before approval) and execute() (after) both enforce:
  * recipient provenance: an address must literally appear in the task's goal or in this run's web.* outputs
    (so a model cannot invent an address),
  * no duplicates: exact address emailed before (SQL log) or company already contacted (graph),
  * daily cap and per-batch cap,
  * header-injection protection (no CR/LF in to/subject), an opt-out line, plain text only.
SMTP cannot be made idempotent: if the connection drops mid-batch the rest is 'unknown' and is NEVER retried."""
import hashlib
import json
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
import ast
import hashlib
import json
from ..db import Db
from ..graph.memory import norm
from ..tools.executor import ToolArgError, ToolDenied, ToolFailed, ToolTransient
from . import DirectTool, Prepared

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
OPT_OUT = "\n\n--\nIf you'd rather not hear from me, reply 'unsubscribe' and I won't contact you again."
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _try_load(s: str):
    err = None
    for variant in (s, re.sub(r",\s*([}\]])", r"\1", s)):      # second try: drop trailing commas
        try:
            return json.loads(variant, strict=False)           # strict=False allows raw newlines inside strings
        except ValueError as e:
            err = e
    try:                                                       # Python-style list/dict (single quotes, True/None)
        v = ast.literal_eval(s)
        if isinstance(v, (list, dict)):
            return v
    except (ValueError, SyntaxError):
        pass
    raise err


def parse_messages(raw) -> list[dict]:
    """Tolerant: a JSON list, or {"messages": [...]}, possibly wrapped in a markdown fence or prose."""
    if isinstance(raw, str):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        t = t.replace("\u201c", '"').replace("\u201d", '"') if t[:1] not in ('[', '{') else t
        candidates = [t]
        for o, c in (("[", "]"), ("{", "}")):
            a, b = t.find(o), t.rfind(c)
            if a != -1 and b > a:
                candidates.append(t[a:b + 1])
        err = None
        for cand in candidates:
            try:
                raw = _try_load(cand)
                break
            except ValueError as e:
                err = e
        else:
            raise ToolArgError(f"messages is not valid JSON ({str(err)[:120]}). Expected a JSON list of "
                               f"{{to, subject, body, company}} with double quotes, escaped line breaks (\\n) "
                               f"inside strings, and no trailing commas")
    if isinstance(raw, dict):
        raw = raw.get("messages")
    if not isinstance(raw, list) or not raw:
        raise ToolArgError("messages must be a non-empty JSON list")
    return raw


def validate_messages(raw: list, max_batch: int) -> list[dict]:
    if len(raw) > max_batch:
        raise ToolArgError(f"at most {max_batch} emails per batch")
    out, seen = [], set()
    for i, m in enumerate(raw, 1):
        if not isinstance(m, dict):
            raise ToolArgError(f"message {i} is not an object")
        to, subj, body = (str(m.get(k, "")).strip() for k in ("to", "subject", "body"))
        for name, v in (("to", to), ("subject", subj)):
            if "\r" in v or "\n" in v or "\0" in v:
                raise ToolArgError(f"message {i}: line breaks are not allowed in '{name}'")
        if not EMAIL_RE.match(to):
            raise ToolArgError(f"message {i}: '{to[:60]}' is not a valid email address")
        if not (1 <= len(subj) <= 200) or not (1 <= len(body) <= 5000):
            raise ToolArgError(f"message {i}: subject must be 1-200 chars and body 1-5000 chars")
        if to.lower() in seen:
            continue                                                   # duplicate inside the batch: send once
        seen.add(to.lower())
        company = str(m.get("company", "")).strip()[:120] or to.split("@", 1)[1].lower()
        out.append({"to": to, "subject": subj, "body": body, "company": company})
    return out


def normalize_secret(sec: dict) -> dict:
    """Validate the SMTP settings from the setup page."""
    host = str(sec.get("host", "")).strip()
    try:
        port = int(sec.get("port", 0))
    except (TypeError, ValueError):
        port = 0
    security = str(sec.get("security", "starttls")).lower()
    from_addr = str(sec.get("from_addr", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9.\-:]{1,253}", host) or not (1 <= port <= 65535):
        raise ValueError("invalid host or port")
    if security not in ("starttls", "ssl", "none"):
        raise ValueError("security must be starttls, ssl or none")
    if security == "none" and host not in LOCAL_HOSTS:
        raise ValueError("unencrypted SMTP is only allowed for localhost")
    if not EMAIL_RE.match(from_addr):
        raise ValueError("invalid from address")
    return {"host": host, "port": port, "security": security, "from_addr": from_addr,
            "from_name": str(sec.get("from_name", "")).strip()[:80], "username": str(sec.get("username", "")).strip(),
            "password": str(sec.get("password", ""))}


def smtp_connect(sec: dict, timeout: float = 20.0):
    ctx = ssl.create_default_context()
    if sec["security"] == "ssl":
        s = smtplib.SMTP_SSL(sec["host"], sec["port"], timeout=timeout, context=ctx)
    else:
        s = smtplib.SMTP(sec["host"], sec["port"], timeout=timeout)
        if sec["security"] == "starttls":
            s.starttls(context=ctx)
    if sec.get("username"):
        s.login(sec["username"], sec["password"])
    return s


def check_smtp(sec: dict, factory=None) -> None:
    """Setup-page check: log in, send NOTHING. ValueError = credentials rejected; ConnectionError = unreachable."""
    try:
        s = (factory or smtp_connect)(sec)
    except smtplib.SMTPAuthenticationError:
        raise ValueError("the mail server rejected that username/password")
    except (OSError, smtplib.SMTPException) as e:
        raise ConnectionError(str(e)[:200])
    try:
        s.quit()
    except Exception:
        pass


class EmailGuard:
    """SQL log of what was sent (address hashes only: no addresses, no content)."""

    def __init__(self, db):
        self.db = Db.coerce(db)
        self.db.ddl("""CREATE TABLE IF NOT EXISTS email_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
                       user_id TEXT, addr_hash TEXT)""")

    @staticmethod
    def _h(user_id: str, addr: str) -> str:
        return hashlib.sha256(f"{user_id}:{addr.lower()}".encode()).hexdigest()

    def sent_today(self, user_id: str, now: float | None = None) -> int:
        return self.db.execute("SELECT COUNT(*) AS n FROM email_log WHERE user_id=? AND ts>?",
                               (str(user_id), (now or time.time()) - 86400)).fetchone()["n"]

    def already(self, user_id: str, addr: str) -> bool:
        return self.db.execute("SELECT 1 AS x FROM email_log WHERE user_id=? AND addr_hash=?",
                               (str(user_id), self._h(user_id, addr))).fetchone() is not None

    def record(self, user_id: str, addr: str) -> None:
        self.db.execute("INSERT INTO email_log(ts,user_id,addr_hash) VALUES(?,?,?)",
                        (time.time(), str(user_id), self._h(user_id, addr)))


class EmailTool:
    SPEC = dict(
        tool_id="email.send_batch", service="email", name="send_batch", capability="email.send", risk="high",
        approval=True, financial=False,
        description="Send plain-text emails from your own SMTP account (you approve each batch). Arg: messages = a JSON "
                    "list of {to, subject, body, company}. Recipient addresses must appear in the task goal or in fetched "
                    "pages (never guess). Already-contacted recipients are skipped. Max per batch and per day apply.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["messages"],
                      "properties": {"messages": {"type": "string", "maxLength": 60000}}})

    def __init__(self, creds, db, memory_for, settings, smtp_factory=None):
        self.creds, self.memory_for, self.settings = creds, memory_for, settings
        self.guard, self.smtp_factory = EmailGuard(db), smtp_factory

    def tool(self) -> DirectTool:
        return DirectTool(self.SPEC, self.execute, self.prepare, irreversible=True)

    def _secret(self, user_id: str) -> dict:
        sec = self.creds.get_service(user_id, "email")
        if not sec:
            raise ToolDenied("email isn't connected (/connect email)")
        return sec

    def _filter(self, user_id: str, msgs: list[dict], evidence: str | None):
        """Returns (keep, skipped[(addr, reason)]). evidence=None skips the provenance check (already approved)."""
        mem = self.memory_for(user_id)
        contacted = mem.contacted_batch([m["company"] for m in msgs]) if mem.enabled else {}
        remaining = self.settings.email_daily_cap - self.guard.sent_today(user_id)
        keep, skipped, ev = [], [], (evidence or "").lower()
        for m in msgs:
            to = m["to"].lower()
            if evidence is not None and to not in ev:
                skipped.append((m["to"], "address not found in the task's sources (looks guessed)"))
            elif self.guard.already(user_id, to):
                skipped.append((m["to"], "already emailed"))
            elif norm(m["company"]) in contacted:
                skipped.append((m["to"], f"company '{m['company']}' already contacted"))
            elif len(keep) >= remaining:
                skipped.append((m["to"], "daily email cap reached"))
            else:
                keep.append(m)
        return keep, skipped

    def prepare(self, ctx, args: dict) -> Prepared:
        sec = self._secret(ctx.user_id)
        msgs = validate_messages(parse_messages(args["messages"]), self.settings.email_max_batch)
        keep, skipped = self._filter(ctx.user_id, msgs, ctx.evidence)
        note = "".join(f"\n  skipped {a}: {why}" for a, why in skipped[:6])
        if not keep:
            return Prepared(args, empty="Nothing to send." + note)
        first = keep[0]
        preview = (f"{len(keep)} email(s) from {sec['from_addr']}:\n"
                   + "\n".join(f"- to {m['to']}: {m['subject'][:70]}" for m in keep[:10])
                   + f"\n\nFirst message:\nSubject: {first['subject']}\n{first['body'][:500]}"
                   + ("\n(an opt-out line is added to every email)" if keep else "") + note)
        return Prepared({"messages": json.dumps(keep)}, preview)

    @staticmethod
    def _build(sec: dict, m: dict) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = formataddr((sec.get("from_name", ""), sec["from_addr"])) if sec.get("from_name") else sec["from_addr"]
        msg["To"], msg["Subject"] = m["to"], m["subject"]
        msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid(domain=sec["from_addr"].split("@")[1])
        msg["List-Unsubscribe"] = f"<mailto:{sec['from_addr']}?subject=unsubscribe>"
        msg.set_content(m["body"] + OPT_OUT)
        return msg

    def execute(self, user_id: str, args: dict, run_id) -> str:
        sec = self._secret(user_id)
        msgs = validate_messages(parse_messages(args["messages"]), self.settings.email_max_batch)
        batch, skipped = self._filter(user_id, msgs, None)      # state may have changed since approval: only ever FEWER
        if not batch:
            return json.dumps({"sent": [], "failed": [], "unknown": [], "skipped": [a for a, _ in skipped]})
        try:
            smtp = (self.smtp_factory or smtp_connect)(sec)
        except smtplib.SMTPAuthenticationError:
            raise ToolFailed("the mail server rejected the stored login: reconnect with /connect email. Nothing was sent.")
        except (OSError, smtplib.SMTPException) as e:
            raise ToolFailed(f"couldn't connect to the mail server ({type(e).__name__}). Nothing was sent.")
        sent, failed, unknown = [], [], []
        try:
            for i, m in enumerate(batch):
                try:
                    smtp.send_message(self._build(sec, m))
                except smtplib.SMTPServerDisconnected:                    # outcome of THIS message is unknown
                    unknown = [x["to"] for x in batch[i:]]
                    break
                except smtplib.SMTPException as e:                        # definite SMTP-level rejection of this message
                    failed.append({"to": m["to"], "error": str(e)[:120]})   # (NB: SMTPException subclasses OSError, so
                    continue                                              #  this clause must come before the OSError one)
                except OSError:                                           # socket-level error/timeout: unknown outcome
                    unknown = [x["to"] for x in batch[i:]]
                    break
                self.guard.record(user_id, m["to"])                       # log immediately: cap/dedupe stay correct on a crash
                sent.append(m)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        mem = self.memory_for(user_id)
        if sent and mem.enabled:
            mem.record_contacts([m["company"] for m in sent], "email")
        result = {"sent": [m["to"] for m in sent], "failed": failed, "unknown": unknown,
                  "skipped": [a for a, _ in skipped]}
        if unknown:
            raise ToolTransient(f"connection lost mid-batch. Sent: {result['sent']}. Outcome UNKNOWN (check your sent "
                                f"folder, not retried): {unknown}")
        if not sent:
            raise ToolFailed("no email was sent: " + "; ".join(f"{f['to']}: {f['error']}" for f in failed))
        return json.dumps(result)
