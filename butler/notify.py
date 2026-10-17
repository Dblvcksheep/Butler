import httpx


class TelegramNotifier:
    """Sends Telegram messages straight through the Bot API, so any process (bot, web, Celery worker) can notify."""

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=f"https://api.telegram.org/bot{token}", timeout=15, transport=transport)

    def __call__(self, user_id, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        payload = {"chat_id": int(user_id), "text": text[:4000]}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in buttons]]}
        self.http.post("/sendMessage", json=payload).raise_for_status()
