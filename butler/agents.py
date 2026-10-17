"""Specialised agent roles. An agent is a role prompt + a preferred model route + the capabilities it may use.
Handoff = a step's output becomes (data) input to the steps that depend on it; recorded in the graph."""
from dataclasses import dataclass
from fnmatch import fnmatch


@dataclass(frozen=True)
class Agent:
    name: str
    system: str
    kind: str                       # which model route (task kind) this agent uses
    capabilities: tuple[str, ...]   # fnmatch globs of capabilities it may use; () = no tools

    def allows(self, capability: str) -> bool:
        return any(fnmatch(capability, g) for g in self.capabilities)


AGENTS: dict[str, Agent] = {a.name: a for a in (
    Agent("assistant", "You are a general assistant.", "chat", ()),
    Agent("researcher", "You are a careful researcher. Use only the inputs provided; say what is uncertain; "
          "never invent sources.", "research", ("web.*", "*.read", "*.search", "*.list", "contacts.*")),
    Agent("writer", "You are a writer. Produce the requested text in the user's preferred style.", "drafting", ()),
    Agent("analyst", "You are an analyst. Extract, compare and summarise the inputs precisely.", "extraction", ("contacts.*",)),
    Agent("operator", "You carry out actions with tools. Be exact with arguments.", "chat", ("*",)),
)}
