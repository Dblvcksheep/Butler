"""LLM providers behind one interface, with typed errors so the router can decide what to do."""
import os
from dataclasses import dataclass

import httpx


class LLMError(Exception):
    pass


class LLMAuthError(LLMError):
    """Key rejected (401/403): the credential is bad, don't keep retrying it."""


class LLMRateLimit(LLMError):
    pass


class LLMUnavailable(LLMError):
    """Timeout / network / 5xx."""


class LLMBadRequest(LLMError):
    """4xx other than auth/rate (e.g. unknown model, context too long)."""


def _check(r: httpx.Response, secret: str) -> None:
    c = r.status_code
    if c < 400:
        return
    body = r.text[:200].replace(secret, "***")
    if c in (401, 403):
        raise LLMAuthError(f"HTTP {c}")
    if c == 429:
        raise LLMRateLimit(f"HTTP {c}")
    if c >= 500:
        raise LLMUnavailable(f"HTTP {c}")
    raise LLMBadRequest(f"HTTP {c}: {body}")


class LLMProvider:
    def __init__(self, api_key: str, model: str, transport: httpx.BaseTransport | None = None):
        self.key, self.model = api_key, model
        self.last_usage: tuple[int, int] | None = None
        self.http = httpx.Client(timeout=60, transport=transport)

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        try:
            r = self.http.post(url, json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise LLMUnavailable(type(e).__name__) from e
        _check(r, self.key)
        return r.json()

    def complete(self, system: str, messages: list[dict], max_tokens: int = 1000) -> str:
        raise NotImplementedError

    def validate(self) -> None:
        """Cheap liveness/auth probe. Raises LLMAuthError if the key is rejected."""
        self.complete("", [{"role": "user", "content": "ping"}], max_tokens=1)


class AnthropicProvider(LLMProvider):
    def complete(self, system, messages, max_tokens=1000):
        payload = {"model": self.model, "max_tokens": max_tokens, "messages": messages}
        if system:
            payload["system"] = system
        d = self._post("https://api.anthropic.com/v1/messages", payload,
                       {"x-api-key": self.key, "anthropic-version": "2023-06-01"})
        u = d.get("usage") or {}
        self.last_usage = (u.get("input_tokens", 0), u.get("output_tokens", 0))
        return "".join(b.get("text", "") for b in d.get("content", []))


class OpenAICompatProvider(LLMProvider):
    """OpenAI Chat Completions and any server speaking the same protocol (Nebius, Qwen/DashScope)."""
    base_url = ""
    token_param = "max_tokens"

    def __init__(self, api_key, model, transport=None, base_url: str | None = None):
        super().__init__(api_key, model, transport)
        if base_url:
            self.base_url = base_url

    def complete(self, system, messages, max_tokens=1000):
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        d = self._post(f"{self.base_url.rstrip('/')}/chat/completions",
                       {"model": self.model, "messages": msgs, self.token_param: max_tokens},
                       {"Authorization": f"Bearer {self.key}"})
        u = d.get("usage") or {}
        self.last_usage = (u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        return (d["choices"][0]["message"].get("content") or "")


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    cls: type
    base_url: str
    model_hint: str


# Base URLs / param names: verify against each provider's current docs before shipping.
PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec("anthropic", "Anthropic (Claude)", AnthropicProvider, "", "e.g. claude-sonnet-5-5"),
    "openai": ProviderSpec("openai", "OpenAI (GPT)", OpenAICompatProvider,
                           "https://api.openai.com/v1", "a model id from your OpenAI account"),
    "nebius": ProviderSpec("nebius", "Nebius", OpenAICompatProvider,
                           "https://api.tokenfactory.nebius.com/v1", "an NVIDIA Nemotron model id from Token Factory (required for the Nebius x NVIDIA hackathon)"),
    "qwen": ProviderSpec("qwen", "Qwen (Alibaba DashScope)", OpenAICompatProvider,
                         "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "e.g. a qwen-* model id"),
}


class _OpenAI(OpenAICompatProvider):
    token_param = "max_completion_tokens"  # newer OpenAI models reject max_tokens


def build_provider(provider_id: str, api_key: str, model: str, transport=None) -> LLMProvider:
    spec = PROVIDERS[provider_id]
    if provider_id == "anthropic":
        return AnthropicProvider(api_key, model, transport)
    cls = _OpenAI if provider_id == "openai" else OpenAICompatProvider
    base = os.environ.get(f"{provider_id.upper()}_BASE_URL") or spec.base_url
    return cls(api_key, model, transport, base_url=base)
