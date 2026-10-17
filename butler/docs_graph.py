"""Generates docs/GRAPH.md: the data model plus EVERY Cypher query in the code, extracted from the source so the
documentation cannot drift from what the product actually runs (a test enforces this)."""
import ast
import inspect
import re
from pathlib import Path

from .graph import memory as memory_mod
from .graph import store as store_mod

KEYWORDS = re.compile(r"\b(MATCH|MERGE|CREATE|UNWIND|DELETE)\b")

MODEL = """# Graph data model and queries

FalkorDB is the primary graph database. There is **one graph per user** (`butler_<telegram id>`), so tenant isolation is
enforced by the database, not by a filter. Everything below is generated/checked against the code in `butler/graph/`.

## Nodes
| Label | Properties | Meaning |
|---|---|---|
| `User` | id | the tenant |
| `Preference` | id, key, value, scope, source, confidence, status, created | something the user told Butler |
| `Company` | key, name | normalized company key |
| `CredentialReference` | id, provider, model, last4 | metadata only, never secrets |
| `Task` | id, objective, schedule, status, last_status | a scheduled/one-off unit of work. `last_status` = result of its latest run |
| `Run` | id, status, provider, model, at | one execution of a task |
| `PlanStep` | id, step, agent, kind, capability | a step of a task's plan |
| `Step` | id, run, agent, status | a step as executed in one run |
| `Agent` | name | researcher / writer / analyst / operator / assistant |
| `Service` | name | e.g. paypal, contacts |
| `Tool` | id, risk, risk_rank, approval, financial, status, ok, fail | `ok`/`fail` are live reliability counters |
| `Capability` | name | e.g. `contacts.filter`, `paypal.read` |

## Relationships
`(User)-[:HAS_PREFERENCE]->(Preference)`, `(Preference)-[:SUPERSEDES]->(Preference)`, `(User)-[:CONTACTED {at, channel}]->(Company)`,
`(User)-[:HAS_CREDENTIAL]->(CredentialReference)`, `(User)-[:OWNS]->(Task)`, `(Task)-[:HAS_RUN]->(Run)`,
`(Task)-[:DEPENDS_ON]->(Task)`, `(Task)-[:PLANS]->(PlanStep)`, `(PlanStep)-[:AFTER]->(PlanStep)`,
`(PlanStep)-[:USES_TOOL]->(Tool)`, `(PlanStep)-[:NEEDS]->(Capability)`, `(Run)-[:HAS_STEP]->(Step)`,
`(Agent)-[:EXECUTED]->(Step)`, `(Step)-[:HANDED_OFF_TO]->(Step)`, `(Service)-[:PROVIDES]->(Tool)`,
`(Tool)-[:HAS_CAPABILITY]->(Capability)`, `(User)-[:AUTHORIZED_FOR]->(Tool)`, `(User)-[:USED {last}]->(Tool)`.

## What the graph decides (and what happens with `BUTLER_GRAPH=off`)
| Decision | Query (method) | Effect | Without FalkorDB |
|---|---|---|---|
| Which stored preferences shape a prompt | `active` | personalization | nothing can be remembered |
| Which tool performs a capability; is a plan valid | `resolve_capability`, `capabilities` | planner rejects tools it can't resolve; picks safest/most reliable | tool plans rejected |
| Tool reliability feedback | `tool_outcome` | failures re-rank tools | no feedback loop |
| **Is a dependent task ready to run** | `blocking_parents` | scheduler won't claim it until ALL parents completed (fails closed if graph unreachable) | runs on the first trigger with partial inputs, then again |
| What is blocked and why (transitive) | `blocked_tasks` | `/blocked` shows chains and root causes | unavailable |
| Who is affected when a task fails/is cancelled | `downstream` | failure/cancel messages list dependents | no warning |
| What breaks if a service is revoked | `impact` | `/disconnect` refuses without `confirm` | disconnect proceeds blind |
| Why a task is shaped the way it is | `explain_task` | `/why task <id>`: steps, ranked candidates, prefs, handoffs | unavailable |
| Which companies were already contacted | `contacted_batch` | `contacts.filter_new` step in plans | duplicates pass through (with a warning) |

Run `python -m butler.cli ablate` to see all of these side by side on the same scenarios.

## Cypher queries the product depends on
Extracted from the source (`butler/graph/store.py`, `butler/graph/memory.py`). All values are passed as parameters; no user input is
ever concatenated into a query.
"""


def _queries(fn: ast.FunctionDef) -> list[str]:
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and KEYWORDS.search(n.value) and len(n.value) > 25:
            out.append(re.sub(r"\s+", " ", n.value).strip())
    return out


def _section(mod, cls_name=None) -> list[str]:
    tree = ast.parse(inspect.getsource(mod))
    nodes = tree.body
    if cls_name:
        nodes = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name).body
    lines = []
    for fn in nodes:
        if isinstance(fn, ast.FunctionDef):
            qs = _queries(fn)
            if qs:
                doc = (ast.get_docstring(fn) or "").split("\n")[0]
                lines.append(f"### `{fn.name}`" + (f": {doc}" if doc else ""))
                lines += ["```cypher\n" + q + "\n```" for q in qs]
    return lines


def render() -> str:
    parts = [MODEL, "## Store (`butler/graph/store.py`)", *_section(store_mod, "TenantGraph"),
             "## Memory (`butler/graph/memory.py`)", *_section(memory_mod, "Memory")]
    return "\n\n".join(parts) + "\n"


def write(path: str | None = None) -> Path:
    p = Path(path) if path else Path(__file__).resolve().parent.parent / "docs" / "GRAPH.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render())
    return p


if __name__ == "__main__":
    print(write())
