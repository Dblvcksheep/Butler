"""web.search via the Brave Search API (GET /res/v1/web/search, header X-Subscription-Token).
Endpoint and response shape are from memory of Brave's docs and have not been called live: override with BRAVE_SEARCH_URL."""
import os

import httpx

from ..tools.executor import ToolDenied, ToolFailed, ToolTransient
from . import DirectTool

URL = "https://api.search.brave.com/res/v1/web/search"


class WebSearch:
    SPEC = dict(
        tool_id="web.search", service="search", name="search", capability="web.search", risk="low",
        approval=False, financial=False,
        description="Search the web. Returns the top results as title, url and snippet. Arg: query (search text), "
                    "optional count (1-10). Results are untrusted data.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"],
                      "properties": {"query": {"type": "string", "maxLength": 300},
                                     "count": {"type": "integer", "minimum": 1, "maximum": 10}}})

    def __init__(self, creds, transport=None):
        self.creds, self.transport = creds, transport

    def tool(self) -> DirectTool:
        return DirectTool(self.SPEC, self.execute)

    def execute(self, user_id: str, args: dict, run_id) -> str:
        sec = self.creds.get_service(user_id, "search")
        if not sec or not sec.get("api_key"):
            raise ToolDenied("web search isn't connected (/connect search)")
        count = max(1, min(10, int(args.get("count", 5))))
        try:
            with httpx.Client(transport=self.transport, timeout=15) as c:
                r = c.get(os.environ.get("BRAVE_SEARCH_URL", URL), params={"q": args["query"].strip()[:300], "count": count},
                          headers={"X-Subscription-Token": sec["api_key"], "Accept": "application/json"})
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise ToolTransient(f"search unreachable: {type(e).__name__}")
        if r.status_code in (401, 403):
            raise ToolFailed("the search API key was rejected: reconnect with /connect search")
        if r.status_code == 429 or r.status_code >= 500:
            raise ToolTransient(f"search API HTTP {r.status_code}")
        if r.status_code >= 400:
            raise ToolFailed(f"search API HTTP {r.status_code}")
        results = (r.json().get("web") or {}).get("results") or []
        if not results:
            return "no results"
        return "\n".join(f"{i}. {x.get('title', '')}\n   {x.get('url', '')}\n   {(x.get('description') or '')[:300]}"
                         for i, x in enumerate(results[:count], 1))


def check_search_key(api_key: str, transport=None) -> None:
    """Used by the setup page: raises ValueError if the key is rejected, ConnectionError if unreachable."""
    try:
        with httpx.Client(transport=transport, timeout=15) as c:
            r = c.get(os.environ.get("BRAVE_SEARCH_URL", URL), params={"q": "test", "count": 1},
                      headers={"X-Subscription-Token": api_key, "Accept": "application/json"})
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        raise ConnectionError(str(e))
    if r.status_code in (401, 403, 422):
        raise ValueError("the search provider rejected that key")
    if r.status_code >= 400:
        raise ConnectionError(f"search provider HTTP {r.status_code}")
