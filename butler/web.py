"""Secure secret-entry pages (LLM keys, PayPal credentials). Run behind HTTPS. Secrets never travel via Telegram."""
import html
import threading

import httpx
from flask import Flask, Response, request

from .credentials import MODEL_RE, CredentialError, CredentialStore
from .graph.memory import Memory
from .llm.provider import PROVIDERS, LLMAuthError, LLMError, build_provider

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Butler</title>
<style>body{{font:16px system-ui;max-width:30rem;margin:3rem auto;padding:0 1rem}}
input,select,button{{width:100%;padding:.6rem;margin:.3rem 0 1rem;font:inherit;box-sizing:border-box}}
.err{{color:#b00020}}.ok{{color:#0a7d33}}small{{color:#555}}</style>{body}"""


def _page(body: str, status: int = 200) -> Response:
    return Response(PAGE.format(body=body), status=status, mimetype="text/html")


def _num(v: str):
    v = (v or "").strip()
    return float(v) if v else None


def create_app(creds: CredentialStore, graph=None, factory=build_provider, on_connected=None,
               allow_live: bool = False, paypal_token=None, email_check=None, search_check=None) -> Flask:
    app = Flask(__name__)

    if paypal_token is None:
        from .payments.paypal import PayPalClient
        paypal_token = lambda cid, sec, env: PayPalClient(cid, sec, env).access_token()

    from .integrations.email import check_smtp, normalize_secret
    from .integrations.search import check_search_key
    email_check = email_check or check_smtp
    search_check = search_check or check_search_key

    @app.after_request
    def headers(r):
        r.headers["Cache-Control"] = "no-store"
        r.headers["Referrer-Policy"] = "no-referrer"          # token is in the URL path
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
        return r

    def gone():
        return _page("<h2 class=err>Link expired or already used</h2><p>Ask Butler for a new link.</p>", 410)

    @app.get("/setup/<token>")
    def form(token):
        p = creds.peek(token)
        if not p:
            return gone()
        _, purpose = p
        if purpose == "paypal":
            return _page("""<h2>Connect PayPal</h2><p>Single-use link, 10 minutes. Stored encrypted, never shown again.
Use <b>Sandbox</b> credentials from developer.paypal.com first.</p><form method=post autocomplete=off>
<label>Environment<select name=env><option value=sandbox>Sandbox</option><option value=live>Live</option></select></label>
<label>Client ID<input name=client_id required maxlength=300></label>
<label>Client secret<input name=client_secret type=password required maxlength=300 autocomplete=off></label>
<button>Connect</button></form>""")
        if purpose == "email":
            return _page("""<h2>Connect email (SMTP)</h2><p>Single-use link, 10 minutes. Butler logs in to check the details but sends
nothing now. Stored encrypted. Every batch of emails still needs your approval in Telegram. For Gmail use an <b>app password</b>.</p>
<form method=post autocomplete=off>
<label>SMTP host<input name=host required maxlength=253 placeholder="smtp.gmail.com"></label>
<label>Port<input name=port required inputmode=numeric value="587"></label>
<label>Security<select name=security><option value=starttls>STARTTLS (587)</option><option value=ssl>SSL (465)</option>
<option value=none>None (localhost only)</option></select></label>
<label>Username<input name=username maxlength=200 autocomplete=off></label>
<label>Password / app password<input name=password type=password maxlength=300 autocomplete=off></label>
<label>From address<input name=from_addr required maxlength=200></label>
<label>From name (optional)<input name=from_name maxlength=80></label>
<button>Connect</button></form>""")
        if purpose == "search":
            return _page("""<h2>Connect web search (Brave Search API)</h2><p>Single-use link, 10 minutes. Stored encrypted.</p>
<form method=post autocomplete=off><label>API key<input name=api_key type=password required maxlength=300 autocomplete=off></label>
<button>Connect</button></form>""")
        pre = request.args.get("provider", "")
        opts = "".join(f"<option value='{html.escape(x.id)}'{' selected' if x.id == pre else ''}>"
                       f"{html.escape(x.label)}</option>" for x in PROVIDERS.values())
        return _page(f"""<h2>Add a model key</h2>
<p>Single-use link, expires in 10 minutes. Stored encrypted, never shown again. Every provider is optional; you need at least one.</p>
<form method=post autocomplete=off>
<label>Provider<select name=provider>{opts}</select></label>
<label>Model id<input name=model required maxlength=100 placeholder="exact model id from your provider"></label>
<label>API key<input name=api_key type=password required maxlength=512 autocomplete=off></label>
<label>Price per 1M input tokens (optional, for spend tracking)<input name=price_in inputmode=decimal></label>
<label>Price per 1M output tokens (optional)<input name=price_out inputmode=decimal></label>
<button>Save</button></form>""")

    @app.post("/setup/<token>")
    def submit(token):
        p = creds.peek(token)
        if not p:
            return gone()
        uid = creds.consume_token(token)       # single-use, atomic
        if not uid:
            return gone()
        return {"paypal": _paypal, "email": _email, "search": _search}.get(p[1], _llm)(uid)

    def _llm(uid):
        provider = request.form.get("provider", "")
        model = request.form.get("model", "").strip()
        key = request.form.get("api_key", "").strip()
        try:
            pin, pout = _num(request.form.get("price_in")), _num(request.form.get("price_out"))
        except ValueError:
            return _page("<h2 class=err>Prices must be numbers</h2><p>Ask Butler for a new link.</p>", 400)
        if provider not in PROVIDERS or not MODEL_RE.match(model):
            return _page("<h2 class=err>Invalid provider or model id</h2><p>Ask Butler for a new link.</p>", 400)
        note = ""
        try:                                    # validate before storing; only a definite auth failure rejects
            factory(provider, key, model).validate()
        except LLMAuthError:
            return _page("<h2 class=err>That key was rejected by the provider</h2><p>Ask Butler for a new link.</p>", 400)
        except LLMError:
            note = "<p>Saved, but I couldn't verify it right now (provider unavailable or model id unrecognised).</p>"
        try:
            cid = creds.add(uid, provider, model, key, pin, pout)
        except CredentialError as e:
            return _page(f"<h2 class=err>{html.escape(str(e))}</h2><p>Ask Butler for a new link.</p>", 400)
        if graph is not None:
            Memory(graph.tenant(uid), uid).set_credential_ref(cid, provider, model, key[-4:])
        return _page(f"<h2 class=ok>Saved</h2>{note}<p>Return to Telegram (/models).</p>")

    def _paypal(uid):
        env = request.form.get("env", "sandbox")
        cid_, sec = request.form.get("client_id", "").strip(), request.form.get("client_secret", "").strip()
        if env not in ("sandbox", "live") or not cid_ or not sec:
            return _page("<h2 class=err>Missing or invalid fields</h2><p>Ask Butler for a new link.</p>", 400)
        if env == "live" and not allow_live:
            return _page("<h2 class=err>Live mode is disabled on this server</h2><p>Use Sandbox.</p>", 400)
        try:
            paypal_token(cid_, sec, env)        # proves the credentials are real
        except httpx.HTTPStatusError:
            return _page("<h2 class=err>PayPal rejected those credentials</h2><p>Ask Butler for a new link.</p>", 400)
        except httpx.HTTPError:
            return _page("<h2 class=err>Couldn't reach PayPal to verify</h2><p>Ask Butler for a new link.</p>", 502)
        creds.set_service(uid, "paypal", {"client_id": cid_, "client_secret": sec, "env": env})
        if on_connected:                        # discovery can take a while (npx): do it in the background
            threading.Thread(target=on_connected, args=(uid, "paypal"), daemon=True).start()
        return _page(f"<h2 class=ok>PayPal connected ({html.escape(env)})</h2><p>Butler will message you when its tools are ready.</p>")

    def _connected(uid, service):
        if on_connected:
            threading.Thread(target=on_connected, args=(uid, service), daemon=True).start()

    def _email(uid):
        try:
            sec = normalize_secret({k: request.form.get(k, "") for k in
                                    ("host", "port", "security", "from_addr", "from_name", "username", "password")})
        except ValueError as e:
            return _page(f"<h2 class=err>{html.escape(str(e))}</h2><p>Ask Butler for a new link.</p>", 400)
        try:
            email_check(sec)                    # log in only; nothing is sent
        except ValueError as e:
            return _page(f"<h2 class=err>{html.escape(str(e))}</h2><p>Ask Butler for a new link.</p>", 400)
        except ConnectionError:
            return _page("<h2 class=err>Couldn't reach that mail server</h2><p>Ask Butler for a new link.</p>", 502)
        creds.set_service(uid, "email", sec)
        _connected(uid, "email")
        return _page("<h2 class=ok>Email connected</h2><p>Return to Telegram. Butler never sends without your approval of each batch.</p>")

    def _search(uid):
        key = request.form.get("api_key", "").strip()
        if not key:
            return _page("<h2 class=err>Missing API key</h2><p>Ask Butler for a new link.</p>", 400)
        try:
            search_check(key)
        except ValueError as e:
            return _page(f"<h2 class=err>{html.escape(str(e))}</h2><p>Ask Butler for a new link.</p>", 400)
        except ConnectionError:
            return _page("<h2 class=err>Couldn't reach the search provider to verify</h2><p>Ask Butler for a new link.</p>", 502)
        creds.set_service(uid, "search", {"api_key": key})
        _connected(uid, "search")
        return _page("<h2 class=ok>Web search connected</h2><p>Return to Telegram.</p>")

    return app


def make_app() -> Flask:
    """Production entry (gunicorn 'butler.wsgi:app'): full runtime, discovery runs in the background after connect."""
    from .config import Settings
    from .runtime import build_runtime
    s = Settings.from_env()
    rt = build_runtime(s)

    def connected(uid, service):
        try:
            res = rt.connect_service(uid, service)
            rt.notifier and rt.notifier(uid, f"{service} connected: {len(res['tools'])} tool(s) available (/tools).")
        except Exception as e:
            rt.notifier and rt.notifier(uid, f"{service} credentials saved, but tool discovery failed: {str(e)[:200]}")

    return create_app(rt.creds, rt.orch.graph, on_connected=connected, allow_live=s.allow_live)


def main():   # dev server only
    from .config import Settings
    make_app().run(host="127.0.0.1", port=Settings.from_env().web_port)


if __name__ == "__main__":
    main()
