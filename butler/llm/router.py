"""Pick a model per task kind from the user's own configured keys; fall back down the chain on failure."""
import time
from dataclasses import dataclass, field

from ..credentials import CredentialStore
from .provider import (LLMAuthError, LLMBadRequest, LLMError, LLMRateLimit, LLMUnavailable,
                       build_provider)


@dataclass
class Attempt:
    provider: str
    model: str
    ok: bool
    error: str = ""


@dataclass
class RouterResult:
    text: str
    provider: str
    model: str
    attempts: list[Attempt] = field(default_factory=list)


class BudgetExceeded(Exception):
    pass


class AllModelsFailed(Exception):
    def __init__(self, msg: str, attempts: list[Attempt], no_models: bool = False):
        super().__init__(msg)
        self.attempts, self.no_models = attempts, no_models


class ModelRouter:
    def __init__(self, creds: CredentialStore, factory=build_provider, clock=time.time, cooldown: float = 60.0):
        self.creds, self.factory, self.clock, self.cooldown = creds, factory, clock, cooldown
        self.cooldown_until: dict[int, float] = {}

    def _check_budget(self, user_id: str) -> None:
        b = self.creds.get_budget(user_id)
        if b["daily_tokens"] is None and b["daily_cost"] is None:
            return
        u = self.creds.usage_since(user_id, self.clock() - 86400)
        if b["daily_tokens"] is not None and u["tokens"] >= b["daily_tokens"]:
            raise BudgetExceeded(f"Daily token budget reached ({u['tokens']}/{b['daily_tokens']}). Raise it with /budget.")
        if b["daily_cost"] is not None and u["cost"] >= b["daily_cost"]:
            raise BudgetExceeded(f"Daily cost budget reached ({u['cost']:.4f}/{b['daily_cost']}). Raise it with /budget.")

    def candidates(self, user_id: str, task_kind: str) -> list[dict]:
        rows = [r for r in self.creds.list_creds(user_id) if r["status"] == "active"]
        order = self.creds.get_order(user_id, task_kind) or self.creds.get_order(user_id, "default")

        def key(r):
            return (order.index(r["provider"]) if r["provider"] in order else len(order), r["priority"], r["id"])

        rows.sort(key=key)
        now = self.clock()
        hot = [r for r in rows if self.cooldown_until.get(r["id"], 0) <= now]
        cold = [r for r in rows if r not in hot]   # cooling-down models are still a last resort
        return hot + cold

    def complete(self, user_id: str, task_kind: str, system: str, messages: list[dict],
                 max_tokens: int = 1000) -> RouterResult:
        attempts: list[Attempt] = []
        self._check_budget(user_id)
        cands = self.candidates(user_id, task_kind)
        if not cands:
            raise AllModelsFailed("No usable models. Add a key with /addkey.", attempts, no_models=True)
        for r in cands:
            secret = self.creds.secret(user_id, r["id"])
            prov = None
            try:
                prov = self.factory(r["provider"], secret, r["model"])
                text = prov.complete(system, messages, max_tokens)

                if not isinstance(text, str) or not text.strip():
                    raise LLMUnavailable("Provider returned an empty response")

                text = text.strip()
            except LLMAuthError as e:
                self.creds.set_status(r["id"], "invalid")          # stop using a rejected key
                err = str(e)
            except LLMBadRequest as e:
                err = str(e)                                        # maybe this model only; try next
            except (LLMRateLimit, LLMUnavailable) as e:
                self.cooldown_until[r["id"]] = self.clock() + self.cooldown
                err = str(e)
            except Exception as e:                                  # includes bare LLMError, bugs
                self.cooldown_until[r["id"]] = self.clock() + self.cooldown
                err = f"{type(e).__name__}: {e}"
            else:
                self.cooldown_until.pop(r["id"], None)
                usage = getattr(prov, "last_usage", None)
                cost = None
                if usage and r.get("price_in") is not None and r.get("price_out") is not None:
                    cost = (usage[0] * r["price_in"] + usage[1] * r["price_out"]) / 1_000_000
                self.creds.record_call(user_id, r["id"], task_kind, True, usage=usage, cost=cost)
                attempts.append(Attempt(r["provider"], r["model"], True))
                return RouterResult(text, r["provider"], r["model"], attempts)
            err = err.replace(secret, "***")
            attempts.append(Attempt(r["provider"], r["model"], False, err))
            self.creds.record_call(user_id, r["id"], task_kind, False, err)
        raise AllModelsFailed("All configured models failed: "
                              + "; ".join(f"{a.provider}/{a.model}: {a.error}" for a in attempts), attempts)


def classify(text: str) -> str:
    t = text.lower()
    if "```" in t or any(w in t for w in ("bug", "traceback", "function", "python", "code", "stack trace")):
        return "code"
    if any(w in t for w in ("draft", "write ", "email", "rewrite", "message to")):
        return "drafting"
    if any(w in t for w in ("research", "find ", "search", "compare", "latest")):
        return "research"
    if any(w in t for w in ("summarize", "extract", "list the")):
        return "extraction"
    return "chat"
