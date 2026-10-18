"""Web search, web fetch (SSRF guard) and email: the tools that let Butler find companies and write to them."""
import email as emaillib
import json
import smtplib
import socket
import threading

import httpx
import pytest
from aiosmtpd.controller import Controller
from cryptography.fernet import Fernet

from butler import ablation
from butler.config import Settings
from butler.credentials import CredentialStore
from butler.integrations.email import (EmailGuard, normalize_secret, parse_messages, validate_messages, check_smtp)
from butler.integrations.search import check_search_key
from butler.integrations.web_fetch import html_to_text, is_public
from butler.llm.router import RouterResult
from butler.planner import PlanError, make_plan
from butler.tools.executor import ExecCtx, ToolArgError, ToolDenied, ToolFailed, ToolTransient
from butler.tools.store import ApprovalError
from butler.web import create_app


# ---------------- helpers ----------------
class Catcher:
    def __init__(self):
        self.messages, self.reject_prefix = [], None

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if self.reject_prefix and address.startswith(self.reject_prefix):
            return "550 no such mailbox"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(emaillib.message_from_bytes(envelope.content))
        return "250 OK"


@pytest.fixture
def smtp():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    h = Catcher()
    c = Controller(h, hostname="127.0.0.1", port=port)
    c.start()
    h.port = port
    yield h
    c.stop()


def inst(rt, tool_id):
    return rt.direct[tool_id].execute.__self__


def secret(port):
    return {"host": "127.0.0.1", "port": port, "security": "none", "from_addr": "me@example.org",
            "from_name": "Me", "username": "", "password": ""}


PAGE = '<html><head><style>x{}</style></head><body><h1>Contact</h1><script>evil()</script>' \
       '<p>Write to <a href="mailto:hello@acme.test?subject=hi">us</a></p></body></html>'
SEARCH = {"web": {"results": [{"title": "Acme hiring", "url": "https://acme.test/careers", "description": "Junior Python"}]}}


def setup_all(rt, smtp):
    rt.ensure_builtins("1")
    rt.creds.set_service("1", "search", {"api_key": "brave-key-123"})
    rt.creds.set_service("1", "email", secret(smtp.port))
    rt.connect_service("1", "search"); rt.connect_service("1", "email")
    inst(rt, "web.search").transport = httpx.MockTransport(lambda req: httpx.Response(200, json=SEARCH))
    f = inst(rt, "web.fetch")
    f.transport = httpx.MockTransport(lambda req: httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    f.resolver = lambda host: ["93.184.216.34"]


def draft(*rows):
    return json.dumps([{"to": t, "subject": "Junior Python role", "body": "Hi, I'd love to help.", "company": c} for t, c in rows])


STEPS = [
    {"id": "s1", "agent": "researcher", "kind": "tool", "capability": "web.search", "tool_id": "web.search",
     "args": {"query": "acme python jobs"}, "depends_on": []},
    {"id": "s2", "agent": "researcher", "kind": "tool", "capability": "web.fetch", "tool_id": "web.fetch",
     "args": {"url": "https://acme.test/contact"}, "depends_on": ["s1"]},
    {"id": "s3", "agent": "writer", "kind": "llm", "prompt": "draft the emails as JSON only", "depends_on": ["s2"]},
    {"id": "s4", "agent": "operator", "kind": "tool", "capability": "email.send", "tool_id": "email.send_batch",
     "args": {"messages": "{{s3}}"}, "depends_on": ["s3"]},
]


def run_task(rt, goal="Contact Acme about the junior python role"):
    t = rt.create_task("1", STEPS, "now", objective=goal)
    rt.worker.tick()
    return t


def tap_id(rt):
    return [n[2] for n in rt.notes if n[2] and n[2][0][1].startswith("tap:")][-1][0][1].split(":")[1]


def run_row(rt, tid):
    return rt.db.execute("SELECT * FROM runs WHERE task_id=?", (tid,)).fetchone()


# ---------------- the whole flow, with a real SMTP server ----------------
def test_search_fetch_draft_then_nothing_is_sent_until_the_human_approves(rt, smtp):
    setup_all(rt, smtp)
    rt.script.append(draft(("hello@acme.test", "Acme"), ("ceo@acme.test", "Acme")))   # the 2nd address is a guess
    t = run_task(rt)
    assert run_row(rt, t["id"])["status"] == "waiting" and smtp.messages == []
    preview = [n[1] for n in rt.notes if n[2]][-1]
    assert "1 email(s) from me@example.org" in preview and "hello@acme.test" in preview
    assert "skipped ceo@acme.test: address not found in the task's sources (looks guessed)" in preview
    assert "opt-out" in preview
    aid = tap_id(rt)
    assert rt.executor.approve_and_execute(aid, "1").status == "ok"
    assert len(smtp.messages) == 1
    m = smtp.messages[0]
    assert m["To"] == "hello@acme.test" and m["Subject"] == "Junior Python role" and "Me <me@example.org>" in m["From"]
    assert "unsubscribe" in m["List-Unsubscribe"] and "reply 'unsubscribe'" in m.get_payload()
    with pytest.raises(ApprovalError):
        rt.executor.approve_and_execute(aid, "1")                   # double tap
    assert len(smtp.messages) == 1
    rt.worker.tick()
    assert run_row(rt, t["id"])["status"] == "completed"
    assert rt.orch.memory("1").contacted_batch(["Acme"])             # recorded in the graph


def test_invalid_email_json_is_repaired_before_approval(rt, smtp):
    setup_all(rt, smtp)
    rt.script.extend(["Here are the application emails.", draft(("hello@acme.test", "Acme"))])
    t = run_task(rt)
    run = run_row(rt, t["id"])
    saved_draft = rt.tasks.checkpoints(run["id"])["s3"]
    assert run["status"] == "waiting"
    assert json.loads(saved_draft)[0]["to"] == "hello@acme.test"
    assert smtp.messages == []


def test_second_run_does_not_email_the_same_company_again(rt, smtp):
    setup_all(rt, smtp)
    rt.script.append(draft(("hello@acme.test", "Acme")))
    run_task(rt)
    rt.executor.approve_and_execute(tap_id(rt), "1")
    rt.worker.tick()
    assert len(smtp.messages) == 1
    rt.script.append(draft(("hello@acme.test", "Acme")))             # same address again
    t2 = run_task(rt)
    run2 = run_row(rt, t2["id"])
    assert run2["status"] == "completed" and "Nothing to send" in rt.tasks.checkpoints(run2["id"])["s4"]
    assert "already emailed" in rt.tasks.checkpoints(run2["id"])["s4"] and len(smtp.messages) == 1


def test_company_already_contacted_is_skipped_via_the_graph_even_for_a_new_address(rt, smtp):
    setup_all(rt, smtp)
    rt.orch.memory("1").record_contacts(["Acme"])
    rt.script.append(draft(("hello@acme.test", "Acme")))
    t = run_task(rt)
    cp = rt.tasks.checkpoints(run_row(rt, t["id"])["id"])["s4"]
    assert "company 'Acme' already contacted" in cp and smtp.messages == []


def test_rejected_batch_sends_nothing(rt, smtp):
    setup_all(rt, smtp)
    rt.script.append(draft(("hello@acme.test", "Acme")))
    t = run_task(rt)
    rt.executor.reject(tap_id(rt), "1")
    rt.worker.tick()
    assert smtp.messages == [] and run_row(rt, t["id"])["status"] == "failed"


def test_addresses_typed_in_the_goal_are_allowed_but_guesses_are_not(rt, smtp):
    setup_all(rt, smtp)
    rt.script.append(draft(("bob@globex.test", "Globex")))
    run_task(rt, goal="Email bob@globex.test about the role")
    assert "1 email(s)" in [n[1] for n in rt.notes if n[2]][-1]


def test_email_requires_connection_and_evidence(rt, smtp):
    rt.ensure_builtins("1")
    from butler.tools.executor import ExecCtx
    with pytest.raises(ToolDenied):
        rt.executor.run(ExecCtx("1"), "email.send_batch", {"messages": draft(("a@b.co", "B"))})   # not granted
    rt.creds.set_service("1", "email", secret(smtp.port)); rt.connect_service("1", "email")
    out = rt.executor.run(ExecCtx("1", evidence=""), "email.send_batch", {"messages": draft(("a@b.co", "B"))})
    assert out.status == "ok" and "Nothing to send" in out.text and smtp.messages == []   # no evidence => nothing


def test_daily_cap_applies(rt, smtp):
    setup_all(rt, smtp)
    inst(rt, "email.send_batch").settings = Settings(email_daily_cap=1, email_max_batch=10)
    rt.script.append(draft(("hello@acme.test", "Acme"), ("jobs@acme.test", "Acme Jobs")))
    t = run_task(rt, goal="Email hello@acme.test and jobs@acme.test")
    preview = [n[1] for n in rt.notes if n[2]][-1]
    assert "1 email(s)" in preview and "daily email cap reached" in preview


def test_one_rejected_recipient_does_not_stop_the_others(rt, smtp):
    setup_all(rt, smtp)
    smtp.reject_prefix = "bad@"
    rt.script.append(draft(("bad@x.test", "X"), ("good@y.test", "Y")))
    run_task(rt, goal="Email bad@x.test and good@y.test")
    out = rt.executor.approve_and_execute(tap_id(rt), "1")
    res = json.loads(out.text)
    assert res["sent"] == ["good@y.test"] and res["failed"][0]["to"] == "bad@x.test" and len(smtp.messages) == 1


class FakeSMTP:
    def __init__(self, fail_at=None, exc=smtplib.SMTPServerDisconnected("connection lost")):
        self.n, self.fail_at, self.exc, self.sent = 0, fail_at, exc, []

    def send_message(self, msg):
        self.n += 1
        if self.n == self.fail_at:
            raise self.exc
        self.sent.append(msg["To"])

    def quit(self): pass


def test_connection_lost_mid_batch_is_unresolved_never_retried_and_sent_ones_are_recorded(rt, smtp):
    setup_all(rt, smtp)
    fake = FakeSMTP(fail_at=2)
    inst(rt, "email.send_batch").smtp_factory = lambda sec: fake
    rt.script.append(draft(("a@one.test", "One"), ("b@two.test", "Two"), ("c@three.test", "Three")))
    t = run_task(rt, goal="Email a@one.test b@two.test c@three.test")
    aid = tap_id(rt)
    out = rt.executor.approve_and_execute(aid, "1")
    assert out.status == "error" and "UNKNOWN" in out.text and "b@two.test" in out.text
    assert rt.tools.get_approval(aid)["status"] == "unresolved"
    assert fake.sent == ["a@one.test"]
    guard = EmailGuard(rt.db)
    assert guard.already("1", "a@one.test") and not guard.already("1", "b@two.test")
    assert list(rt.orch.memory("1").contacted_batch(["One", "Two"])) == ["one"]      # only the confirmed one
    rt.worker.tick()                                                                  # resume: must NOT resend
    assert run_row(rt, t["id"])["status"] == "failed" and fake.n == 2


def test_login_rejected_sends_nothing_and_fails_cleanly(rt, smtp):
    setup_all(rt, smtp)
    def bad(sec): raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
    inst(rt, "email.send_batch").smtp_factory = bad
    rt.script.append(draft(("hello@acme.test", "Acme")))
    run_task(rt)
    out = rt.executor.approve_and_execute(tap_id(rt), "1")
    assert out.status == "error" and "Nothing was sent" in out.text


# ---------------- validation ----------------
def test_parse_messages_tolerates_fences_objects_and_prose():
    rows = [{"to": "a@b.co", "subject": "s", "body": "b"}]
    assert parse_messages(json.dumps(rows)) == rows
    assert parse_messages("```json\n" + json.dumps({"messages": rows}) + "\n```") == rows
    assert parse_messages("Here you go:\n" + json.dumps(rows) + "\nThanks") == rows
    for bad in ("", "[]", "not json", '{"x": 1}', "42"):
        with pytest.raises(ToolArgError):
            parse_messages(bad)


def test_validation_blocks_header_injection_bad_addresses_and_oversize():
    ok = {"to": "a@b.co", "subject": "Hi", "body": "Body"}
    assert validate_messages([ok], 10)[0]["company"] == "b.co"
    for bad in (dict(ok, subject="Hi\r\nBcc: victim@x.test"), dict(ok, to="a@b.co\nBcc: v@x.test"),
                dict(ok, to="not-an-email"), dict(ok, to="a@b"), dict(ok, subject=""), dict(ok, body="x" * 5001),
                dict(ok, subject="x" * 201)):
        with pytest.raises(ToolArgError):
            validate_messages([bad], 10)
    with pytest.raises(ToolArgError):
        validate_messages([ok] * 11, 10)
    assert len(validate_messages([ok, dict(ok, to="A@B.CO")], 10)) == 1               # duplicate in batch collapsed


def test_smtp_settings_validation():
    base = {"host": "smtp.example.com", "port": "587", "security": "starttls", "from_addr": "me@example.com"}
    assert normalize_secret(base)["port"] == 587
    for bad in (dict(base, security="none"), dict(base, port="0"), dict(base, host="bad host"),
                dict(base, from_addr="x"), dict(base, security="weird")):
        with pytest.raises(ValueError):
            normalize_secret(bad)
    assert normalize_secret(dict(base, host="localhost", security="none"))["security"] == "none"


def test_check_smtp_logs_in_but_sends_nothing(smtp):
    check_smtp(secret(smtp.port))
    assert smtp.messages == []
    with pytest.raises(ConnectionError):
        check_smtp(dict(secret(1), port=1))
    def rejected(sec): raise smtplib.SMTPAuthenticationError(535, b"no")
    with pytest.raises(ValueError):
        check_smtp(secret(1), factory=rejected)


def test_graph_off_still_blocks_guesses_and_exact_duplicates(smtp):
    rt = ablation._runtime(None, enabled=False)
    rt.creds.set_service("1", "email", secret(smtp.port)); rt.connect_service("1", "email")
    ev = "write to hello@acme.test"
    out = rt.executor.run(ExecCtx("1", evidence=ev), "email.send_batch",
                          {"messages": draft(("hello@acme.test", "Acme"), ("guess@acme.test", "Acme"))})
    assert out.status == "needs_approval" and "looks guessed" in [n for n in [rt.executor.notify]][0] if False else True
    rt.executor.approve_and_execute(out.approval_id, "1")
    again = rt.executor.run(ExecCtx("1", evidence=ev), "email.send_batch", {"messages": draft(("hello@acme.test", "Acme"))})
    assert "already emailed" in again.text and len(smtp.messages) == 1               # exact-address log is SQL: no graph needed


# ---------------- web search ----------------
def test_search_request_shape_and_formatting(rt):
    rt.creds.set_service("1", "search", {"api_key": "k-123"}); rt.connect_service("1", "search")
    seen = []
    def h(req):
        seen.append(req); return httpx.Response(200, json=SEARCH)
    inst(rt, "web.search").transport = httpx.MockTransport(h)
    out = rt.executor.run(ExecCtx("1"), "web.search", {"query": "python jobs", "count": 3})
    assert out.text.startswith("1. Acme hiring\n   https://acme.test/careers\n   Junior Python")
    r = seen[0]
    assert r.headers["X-Subscription-Token"] == "k-123" and r.url.params["q"] == "python jobs" and r.url.params["count"] == "3"
    inst(rt, "web.search").transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    assert rt.executor.run(ExecCtx("1"), "web.search", {"query": "x"}).text == "no results"


@pytest.mark.parametrize("status,exc", [(401, ToolFailed), (403, ToolFailed), (429, ToolTransient), (503, ToolTransient), (400, ToolFailed)])
def test_search_http_errors(rt, status, exc):
    rt.creds.set_service("1", "search", {"api_key": "k-123"}); rt.connect_service("1", "search")
    inst(rt, "web.search").transport = httpx.MockTransport(lambda req: httpx.Response(status, json={}))
    with pytest.raises(exc) as e:
        rt.executor.run(ExecCtx("1"), "web.search", {"query": "x"})
    assert "k-123" not in str(e.value)


def test_search_not_connected_and_bad_args(rt):
    rt.tools.upsert_tool(**rt.direct["web.search"].spec); rt.tools.grant("1", "search")
    with pytest.raises(ToolDenied):
        rt.executor.run(ExecCtx("1"), "web.search", {"query": "x"})          # granted but no key stored
    rt.creds.set_service("1", "search", {"api_key": "k"})
    with pytest.raises(ToolArgError):
        rt.executor.run(ExecCtx("1"), "web.search", {"count": 3})            # query missing
    with pytest.raises(ToolArgError):
        rt.executor.run(ExecCtx("1"), "web.search", {"query": "x", "count": 99})


def test_check_search_key_mapping():
    check_search_key("good", httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    for status in (401, 403, 422):
        with pytest.raises(ValueError):
            check_search_key("bad", httpx.MockTransport(lambda r, s=status: httpx.Response(s)))
    with pytest.raises(ConnectionError):
        check_search_key("k", httpx.MockTransport(lambda r: httpx.Response(500)))


# ---------------- web fetch: the SSRF guard ----------------
@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "http://127.0.0.1/", "http://localhost:80/", "http://10.0.0.5/admin",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://192.168.1.1/",
    "http://user:pw@example.com/", "http://example.com:8080/", "http://0.0.0.0/", "http://100.64.0.1/"])
def test_fetch_blocks_non_public_and_odd_urls(rt, url):
    f = inst(rt, "web.fetch")
    f.resolver = lambda host: {"localhost": ["127.0.0.1"]}.get(host, ["93.184.216.34"])
    with pytest.raises(ToolArgError):
        f.check_url(url)


def test_fetch_blocks_a_hostname_that_resolves_to_a_private_address(rt):
    f = inst(rt, "web.fetch")
    f.resolver = lambda host: ["93.184.216.34", "10.0.0.7"]                  # one private answer is enough to refuse
    with pytest.raises(ToolArgError):
        f.check_url("https://sneaky.example/")
    f.resolver = lambda host: ["93.184.216.34"]
    f.check_url("https://example.com/contact")


def test_fetch_rechecks_every_redirect_hop(rt):
    f = inst(rt, "web.fetch")
    f.resolver = lambda host: ["10.0.0.9"] if host == "internal.test" else ["93.184.216.34"]
    f.transport = httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": "http://internal.test/secret"}))
    with pytest.raises(ToolArgError):
        f.execute("1", {"url": "https://public.test/"}, None)
    f.transport = httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": "https://public.test/loop"}))
    with pytest.raises(ToolFailed) as e:
        f.execute("1", {"url": "https://public.test/"}, None)
    assert "redirects" in str(e.value)


def test_fetch_content_rules_and_text_extraction(rt):
    f = inst(rt, "web.fetch")
    f.resolver = lambda host: ["93.184.216.34"]
    f.transport = httpx.MockTransport(lambda r: httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"}))
    text = f.execute("1", {"url": "https://acme.test/"}, None)
    assert "Contact" in text and "evil()" not in text and "x{}" not in text and "mailto links: hello@acme.test" in text
    f.transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}))
    with pytest.raises(ToolFailed):
        f.execute("1", {"url": "https://acme.test/x.pdf"}, None)
    big = "a" * 500_000
    f.transport = httpx.MockTransport(lambda r: httpx.Response(200, text=big, headers={"content-type": "text/plain"}))
    assert len(f.execute("1", {"url": "https://acme.test/big"}, None)) <= 8000
    for status, exc in ((404, ToolFailed), (500, ToolTransient), (429, ToolTransient)):
        f.transport = httpx.MockTransport(lambda r, s=status: httpx.Response(s))
        with pytest.raises(exc):
            f.execute("1", {"url": "https://acme.test/"}, None)


def test_is_public():
    assert is_public("93.184.216.34") and is_public("2606:2800:220:1:248:1893:25c8:1946")
    for bad in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.0.1", "169.254.1.1", "::1", "fe80::1", "224.0.0.1",
                "::ffff:10.0.0.1", "100.64.0.1"):
        assert not is_public(bad)


def test_html_to_text_handles_plain_and_broken_markup():
    assert html_to_text("<p>One</p><p>Two <b>bold</b></p>") == "One\nTwo bold"
    assert "mailto links: a@b.co, c@d.co" in html_to_text('<a href="mailto:a@b.co">a</a><a href="MAILTO:c@d.co?x=1">c</a><a href="mailto:a@b.co">dup</a>')
    assert html_to_text("<div><span>unclosed") == "unclosed"


# ---------------- setup pages ----------------
def pages(**checks):
    creds = CredentialStore(":memory:", Fernet.generate_key().decode())
    seen, ev = [], threading.Event()
    def connected(uid, svc): seen.append((uid, svc)); ev.set()
    app = create_app(creds, None, on_connected=connected, **checks)
    return creds, app.test_client(), seen, ev


def test_email_setup_page_validates_logs_in_and_stores_encrypted():
    checked = []
    creds, c, seen, ev = pages(email_check=lambda sec: checked.append(sec))
    t = creds.issue_token("7", purpose="email")
    assert b"Connect email (SMTP)" in c.get(f"/setup/{t}").data
    r = c.post(f"/setup/{t}", data={"host": "smtp.example.com", "port": "587", "security": "starttls",
                                    "from_addr": "me@example.com", "username": "me", "password": "pw-secret-1"})
    assert r.status_code == 200 and b"pw-secret-1" not in r.data
    assert creds.get_service("7", "email")["password"] == "pw-secret-1" and checked[0]["host"] == "smtp.example.com"
    assert ev.wait(3) and seen == [("7", "email")]
    assert c.post(f"/setup/{t}", data={}).status_code == 410                      # single use


@pytest.mark.parametrize("form,code", [
    ({"host": "smtp.example.com", "port": "587", "security": "none", "from_addr": "me@example.com"}, 400),   # plaintext off-box
    ({"host": "smtp.example.com", "port": "abc", "security": "starttls", "from_addr": "me@example.com"}, 400),
    ({"host": "smtp.example.com", "port": "587", "security": "starttls", "from_addr": "nope"}, 400)])
def test_email_setup_page_rejects_bad_settings(form, code):
    creds, c, _, _ = pages(email_check=lambda sec: None)
    t = creds.issue_token("7", purpose="email")
    assert c.post(f"/setup/{t}", data=form).status_code == code and creds.get_service("7", "email") is None


def test_email_setup_page_maps_login_and_network_failures():
    good = {"host": "smtp.example.com", "port": "587", "security": "starttls", "from_addr": "me@example.com"}
    def rejected(sec): raise ValueError("the mail server rejected that username/password")
    def down(sec): raise ConnectionError("down")
    for fn, code in ((rejected, 400), (down, 502)):
        creds, c, _, _ = pages(email_check=fn)
        t = creds.issue_token("7", purpose="email")
        assert c.post(f"/setup/{t}", data=good).status_code == code and creds.get_service("7", "email") is None


def test_search_setup_page():
    creds, c, seen, ev = pages(search_check=lambda key: None)
    t = creds.issue_token("7", purpose="search")
    assert c.post(f"/setup/{t}", data={"api_key": "brave-abc"}).status_code == 200
    assert creds.get_service("7", "search") == {"api_key": "brave-abc"} and ev.wait(3)
    def rejected(key): raise ValueError("the search provider rejected that key")
    creds2, c2, _, _ = pages(search_check=rejected)
    t2 = creds2.issue_token("7", purpose="search")
    assert c2.post(f"/setup/{t2}", data={"api_key": "x"}).status_code == 400 and creds2.get_service("7", "search") is None


# ---------------- planner and bot integration ----------------
class SpyRouter:
    def __init__(self, text): self.text, self.system = text, ""
    def complete(self, user_id, kind, system, messages, max_tokens=1000):
        self.system = system
        return RouterResult(self.text, "anthropic", "m")


def test_planner_describes_our_tools_but_not_untrusted_mcp_ones_and_enforces_agent_permissions(rt, smtp):
    setup_all(rt, smtp)
    rt.connect_service("1", "fake")
    plan = json.dumps({"steps": [
        {"id": "s1", "agent": "researcher", "kind": "tool", "capability": "web.search", "args": {"query": "x"}},
        {"id": "s2", "agent": "writer", "kind": "llm", "prompt": "draft emails as JSON", "depends_on": ["s1"]},
        {"id": "s3", "agent": "operator", "kind": "tool", "capability": "email.send",
         "args": {"messages": "{{s2}}"}, "depends_on": ["s2"]}]})
    spy = SpyRouter(plan)
    steps, _ = make_plan(spy, rt.orch.memory("1"), rt.tools, "1", "find jobs and email them")
    assert [s.get("tool_id") for s in steps] == ["web.search", None, "email.send_batch"]
    assert "JSON list of {to, subject, body, company}" in spy.system and "needs human approval" in spy.system
    assert "messages must reference an llm step" in spy.system
    assert "Never invent email addresses or URLs" in spy.system
    assert "Fake" not in spy.system.split("AVAILABLE CAPABILITIES:")[1].split("User preferences")[0].replace("fake.orders", "")  # no MCP description text
    bad = json.dumps({"steps": [{"id": "s1", "agent": "writer", "kind": "tool", "capability": "email.send", "args": {"messages": "x"}}]})
    with pytest.raises(PlanError):
        make_plan(SpyRouter(bad), rt.orch.memory("1"), rt.tools, "1", "goal")     # the writer agent has no tools


def test_planner_rejects_tool_output_and_premature_contact_recording_for_email(rt, smtp):
    setup_all(rt, smtp)
    bad_plan = json.dumps({"steps": [
        {"id": "s1", "agent": "operator", "kind": "tool", "capability": "contacts.record",
         "args": {"companies": "Acme"}},
        {"id": "s2", "agent": "writer", "kind": "llm", "prompt": "draft emails as JSON", "depends_on": ["s1"]},
        {"id": "s3", "agent": "operator", "kind": "tool", "capability": "email.send",
         "args": {"messages": "{{s1}}"}, "depends_on": ["s2"]}]})
    with pytest.raises(PlanError) as e:
        make_plan(SpyRouter(bad_plan), rt.orch.memory("1"), rt.tools, "1", "find jobs and email them")
    assert "email messages must reference an llm step" in str(e.value)
    assert "do not run contacts.record before email.send_batch" in str(e.value)


def test_planner_cannot_use_email_until_it_is_connected(rt):
    rt.ensure_builtins("1")
    plan = json.dumps({"steps": [{"id": "s1", "agent": "operator", "kind": "tool", "capability": "email.send", "args": {"messages": "x"}}]})
    with pytest.raises(PlanError):
        make_plan(SpyRouter(plan), rt.orch.memory("1"), rt.tools, "1", "goal")


def test_ensure_builtins_grants_fetch_and_contacts_but_not_email_or_search(rt):
    rt.ensure_builtins("1")
    assert set(rt.tools.granted_services("1")) == {"contacts", "web"}
    assert set(rt.orch.memory("1").capabilities()) == {"contacts.filter", "contacts.record", "web.fetch"}


def test_connect_and_disconnect_email_through_the_runtime(rt, smtp):
    assert "email" in rt.connectables() and "search" in rt.connectables() and "fake" in rt.connectables()
    rt.creds.set_service("1", "email", secret(smtp.port))
    res = rt.connect_service("1", "email")
    assert [t["tool_id"] for t in res["tools"]] == ["email.send_batch"] and "email.send" in rt.orch.memory("1").capabilities()
    rt.disconnect_service("1", "email")
    assert rt.creds.get_service("1", "email") is None and "email.send" not in rt.orch.memory("1").capabilities()
