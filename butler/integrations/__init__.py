"""Direct (non-MCP) tools. Each is a DirectTool: a catalog spec + an execute() + an optional prepare().

prepare() runs BEFORE an approval is requested: it validates, filters and previews, so the human approves exactly
what will be done (and the approval fingerprint covers the filtered arguments). execute() does the work."""
from dataclasses import dataclass
from typing import Callable


@dataclass
class Prepared:
    args: dict                  # the (possibly filtered) arguments that will be approved and executed
    preview: str = ""           # shown to the human in the approval message
    empty: str = ""             # non-empty = nothing left to do: finish the step with this text, no approval


@dataclass
class DirectTool:
    spec: dict                              # catalog entry (see tools/store.py upsert_tool)
    execute: Callable                       # (user_id, args, run_id) -> str   (raises ToolFailed/ToolTransient/...)
    prepare: Callable | None = None         # (ctx, args) -> Prepared
    irreversible: bool = False              # if the outcome is unknown after an error, mark 'unresolved', never retry


# services the user connects with /connect (they need credentials entered on the secure page)
DIRECT_SERVICES = {
    "email": {"label": "Email (SMTP)", "secret": "email"},
    "search": {"label": "Web search (Brave)", "secret": "search"},
}
