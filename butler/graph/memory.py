"""Preference memory + outreach history, all as graph structure.

Schema:
  (User)-[:HAS_PREFERENCE]->(Preference {key,value,scope,source,confidence,status,created})
  (Preference)-[:SUPERSEDES]->(Preference)          # corrections keep provenance
  (User)-[:CONTACTED {at,channel}]->(Company {key,name})
"""
import re
import time
import uuid

from .store import TenantGraph


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


class Memory:
    def __init__(self, tg: TenantGraph, user_id: str):
        self.tg, self.uid = tg, str(user_id)

    # ---- preferences ----
    def remember(self, key: str, value: str, scope: str = "global",
                 source: str = "explicit", confidence: float = 1.0) -> str:
        key, scope = norm(key), scope.strip().lower() or "global"
        if not key or not value.strip():
            raise ValueError("key and value required")
        old = self.tg.query(
            "MATCH (:User {id:$uid})-[:HAS_PREFERENCE]->(p:Preference "
            "{key:$key, scope:$scope, status:'active'}) RETURN p.id",
            {"uid": self.uid, "key": key, "scope": scope})
        pid = uuid.uuid4().hex[:12]
        self.tg.query(
            "MATCH (u:User {id:$uid}) CREATE (u)-[:HAS_PREFERENCE]->(:Preference "
            "{id:$id,key:$key,value:$value,scope:$scope,source:$source,"
            "confidence:$conf,status:'active',created:$now})",
            {"uid": self.uid, "id": pid, "key": key, "value": value.strip(), "scope": scope,
             "source": source, "conf": confidence, "now": time.time()})
        for (oid,) in old:
            self.tg.query(
                "MATCH (n:Preference {id:$new}), (o:Preference {id:$old}) "
                "SET o.status='superseded' CREATE (n)-[:SUPERSEDES]->(o)",
                {"new": pid, "old": oid})
        return pid

    def active(self, scopes: tuple[str, ...] = ("global",)) -> list[dict]:
        rows = self.tg.query(
            "MATCH (:User {id:$uid})-[:HAS_PREFERENCE]->(p:Preference {status:'active'}) "
            "WHERE p.scope IN $scopes RETURN p.key, p.value, p.scope, p.source ORDER BY p.created DESC",
            {"uid": self.uid, "scopes": list(scopes)})
        return [dict(key=r[0], value=r[1], scope=r[2], source=r[3]) for r in rows]

    def forget(self, key: str, scope: str | None = None) -> int:
        """Hard delete (including superseded history) -- 'forget' means gone."""
        params = {"uid": self.uid, "key": norm(key), "scope": scope}
        n = self.tg.query(
            "MATCH (:User {id:$uid})-[:HAS_PREFERENCE]->(p:Preference {key:$key}) "
            "WHERE $scope IS NULL OR p.scope=$scope RETURN count(p)", params)[0][0]
        self.tg.query(
            "MATCH (:User {id:$uid})-[:HAS_PREFERENCE]->(p:Preference {key:$key}) "
            "WHERE $scope IS NULL OR p.scope=$scope DETACH DELETE p", params)
        return n

    def why(self, key: str) -> list[dict]:
        """Graph trace for a preference: current value and what it superseded."""
        rows = self.tg.query(
            "MATCH (:User {id:$uid})-[:HAS_PREFERENCE]->(p:Preference {key:$key, status:'active'}) "
            "OPTIONAL MATCH (p)-[:SUPERSEDES*1..]->(o:Preference) "
            "RETURN p.value, p.scope, p.source, p.created, o.value ORDER BY o.created DESC",
            {"uid": self.uid, "key": norm(key)})
        return [dict(value=r[0], scope=r[1], source=r[2], created=r[3], replaced=r[4]) for r in rows]

    # ---- outreach history (duplicate prevention) ----
    def record_contact(self, company: str, channel: str = "email") -> None:
        self.tg.query(
            "MATCH (u:User {id:$uid}) MERGE (c:Company {key:$k}) ON CREATE SET c.name=$name "
            "MERGE (u)-[r:CONTACTED]->(c) SET r.at=$now, r.channel=$ch",
            {"uid": self.uid, "k": norm(company), "name": company.strip(),
             "now": time.time(), "ch": channel})

    def contact_trace(self, company: str) -> dict | None:
        rows = self.tg.query(
            "MATCH (:User {id:$uid})-[r:CONTACTED]->(c:Company {key:$k}) RETURN c.name, r.at, r.channel",
            {"uid": self.uid, "k": norm(company)})
        return dict(company=rows[0][0], at=rows[0][1], channel=rows[0][2]) if rows else None

    # ---- credential references (metadata only, never secrets) ----
    def set_credential_ref(self, cid: int, provider: str, model: str, last4: str) -> None:
        self.tg.query(
            "MATCH (u:User {id:$uid}) MERGE (c:CredentialReference {id:$cid}) "
            "SET c.provider=$p, c.model=$m, c.last4=$l MERGE (u)-[:HAS_CREDENTIAL]->(c)",
            {"uid": self.uid, "cid": cid, "p": provider, "m": model, "l": last4})

    def remove_credential_ref(self, cid: int) -> None:
        self.tg.query("MATCH (:User {id:$uid})-[:HAS_CREDENTIAL]->(c:CredentialReference {id:$cid}) "
                      "DETACH DELETE c", {"uid": self.uid, "cid": cid})

    # ---- tasks and runs ----
    def record_task(self, tid: str, objective: str, schedule: str, status: str = "scheduled") -> None:
        self.tg.query("MATCH (u:User {id:$uid}) MERGE (t:Task {id:$tid}) ON CREATE SET t.last_status='pending' "
                      "SET t.objective=$o, t.schedule=$s, t.status=$st MERGE (u)-[:OWNS]->(t)",
                      {"uid": self.uid, "tid": tid, "o": objective, "s": schedule, "st": status})

    def record_run(self, tid: str, rid: int, status: str, provider: str, model: str) -> None:
        self.tg.query("MATCH (:User {id:$uid})-[:OWNS]->(t:Task {id:$tid}) "
                      "CREATE (t)-[:HAS_RUN]->(:Run {id:$rid, status:$st, provider:$p, model:$m, at:$now})",
                      {"uid": self.uid, "tid": tid, "rid": rid, "st": status, "p": provider,
                       "m": model, "now": time.time()})

    def task_history(self, tid: str, limit: int = 5) -> list[dict]:
        rows = self.tg.query("MATCH (:User {id:$uid})-[:OWNS]->(:Task {id:$tid})-[:HAS_RUN]->(r:Run) "
                             "RETURN r.id, r.status, r.provider, r.model, r.at ORDER BY r.at DESC LIMIT $n",
                             {"uid": self.uid, "tid": tid, "n": limit})
        return [dict(id=r[0], status=r[1], provider=r[2], model=r[3], at=r[4]) for r in rows]

    # ---- tools as graph structure: Service -PROVIDES-> Tool -HAS_CAPABILITY-> Capability; User -AUTHORIZED_FOR-> Tool
    # (a mirror of the authoritative grants table; selection reads it, enforcement does not trust it)
    RISK_RANK = {"low": 0, "medium": 1, "high": 2}

    def sync_tools(self, service: str, tools: list[dict]) -> None:
        for t in tools:
            self.tg.query(
                "MATCH (u:User {id:$uid}) MERGE (s:Service {name:$svc}) MERGE (t:Tool {id:$tid}) "
                "ON CREATE SET t.ok=0, t.fail=0 "
                "SET t.risk=$risk, t.risk_rank=$rank, t.approval=$ap, t.financial=$fin, t.status='available' "
                "MERGE (s)-[:PROVIDES]->(t) MERGE (c:Capability {name:$cap}) MERGE (t)-[:HAS_CAPABILITY]->(c) "
                "MERGE (u)-[:AUTHORIZED_FOR]->(t)",
                {"uid": self.uid, "svc": service, "tid": t["tool_id"], "risk": t["risk"],
                 "rank": self.RISK_RANK[t["risk"]], "ap": bool(t["approval"]), "fin": bool(t["financial"]),
                 "cap": t["capability"]})

    def revoke_service(self, service: str) -> None:
        self.tg.query("MATCH (u:User {id:$uid})-[r:AUTHORIZED_FOR]->(:Tool)<-[:PROVIDES]-(:Service {name:$svc}) DELETE r",
                      {"uid": self.uid, "svc": service})

    def resolve_capability(self, capability: str) -> list[dict]:
        """Graph-driven tool selection: authorized tools with this capability, safest and most reliable first."""
        rows = self.tg.query(
            "MATCH (:User {id:$uid})-[:AUTHORIZED_FOR]->(t:Tool)-[:HAS_CAPABILITY]->(:Capability {name:$cap}) "
            "WHERE t.status='available' RETURN t.id, t.risk, t.ok, t.fail "
            "ORDER BY t.risk_rank ASC, (t.ok - t.fail) DESC, t.id ASC", {"uid": self.uid, "cap": capability})
        return [dict(tool_id=r[0], risk=r[1], ok=r[2], fail=r[3]) for r in rows]

    def capabilities(self) -> dict[str, list[str]]:
        rows = self.tg.query("MATCH (:User {id:$uid})-[:AUTHORIZED_FOR]->(t:Tool)-[:HAS_CAPABILITY]->(c:Capability) "
                             "RETURN c.name, collect(t.id) ORDER BY c.name", {"uid": self.uid})
        return {r[0]: list(r[1]) for r in rows}

    def tool_outcome(self, tool_id: str, ok: bool) -> None:
        self.tg.query("MATCH (u:User {id:$uid}), (t:Tool {id:$tid}) SET t.ok = t.ok + $a, t.fail = t.fail + $b "
                      "MERGE (u)-[r:USED]->(t) SET r.last=$now",
                      {"uid": self.uid, "tid": tool_id, "a": 1 if ok else 0, "b": 0 if ok else 1, "now": time.time()})

    # ---- task dependencies and agent handoffs ----
    def record_dependency(self, child: str, parent: str) -> None:
        """(child)-[:DEPENDS_ON]->(parent): READ by the scheduler (blocking_parents), /blocked and downstream()."""
        self.tg.query("MATCH (:User {id:$uid})-[:OWNS]->(c:Task {id:$c}), (:User {id:$uid})-[:OWNS]->(p:Task {id:$p}) "
                      "MERGE (c)-[:DEPENDS_ON]->(p)", {"uid": self.uid, "c": child, "p": parent})

    def record_steps(self, tid: str, rid: int, steps: list[dict]) -> None:
        """steps: [{id, agent, status, depends_on}] -> (Run)-[:HAS_STEP]->(Step), (Agent)-[:EXECUTED]->(Step),
        (Step)-[:HANDED_OFF_TO]->(Step) for each dependency."""
        for s in steps:
            self.tg.query("MATCH (:User {id:$uid})-[:OWNS]->(:Task {id:$tid})-[:HAS_RUN]->(r:Run {id:$rid}) "
                          "CREATE (r)-[:HAS_STEP]->(st:Step {id:$sid, run:$rid, agent:$agent, status:$status}) "
                          "MERGE (g:Agent {name:$agent}) CREATE (g)-[:EXECUTED]->(st)",
                          {"uid": self.uid, "tid": tid, "rid": rid, "sid": s["id"], "agent": s["agent"],
                           "status": s["status"]})
        for s in steps:
            for d in s.get("depends_on", []):
                self.tg.query("MATCH (a:Step {run:$rid, id:$a}), (b:Step {run:$rid, id:$b}) "
                              "CREATE (a)-[:HANDED_OFF_TO]->(b)", {"rid": rid, "a": d, "b": s["id"]})

    def handoffs(self, rid: int) -> list[tuple]:
        return [tuple(r) for r in self.tg.query(
            "MATCH (a:Step {run:$rid})-[:HANDED_OFF_TO]->(b:Step {run:$rid}) RETURN a.agent, a.id, b.agent, b.id",
            {"rid": rid})]


    # ================= graph-driven decisions =================
    enabled = True

    def set_task_status(self, tid: str, status: str | None = None, last: str | None = None) -> None:
        """Keep the graph's view of a task current: status (scheduled/paused/cancelled/...) and last_status
        (result of its latest run). The scheduler's readiness check reads last_status."""
        if status is not None:
            self.tg.query("MATCH (:User {id:$uid})-[:OWNS]->(t:Task {id:$tid}) SET t.status=$st",
                          {"uid": self.uid, "tid": tid, "st": status})
        if last is not None:
            self.tg.query("MATCH (:User {id:$uid})-[:OWNS]->(t:Task {id:$tid}) SET t.last_status=$ls",
                          {"uid": self.uid, "tid": tid, "ls": last})

    def blocking_parents(self, tid: str) -> list[dict]:
        """READINESS: direct dependencies whose latest run has not completed. The scheduler won't claim a task
        while this is non-empty."""
        rows = self.tg.query(
            "MATCH (:User {id:$uid})-[:OWNS]->(c:Task {id:$tid})-[:DEPENDS_ON]->(p:Task) "
            "WHERE coalesce(p.last_status, 'pending') <> 'completed' RETURN p.id, p.last_status, p.objective",
            {"uid": self.uid, "tid": tid})
        return [dict(id=r[0], status=r[1], objective=r[2]) for r in rows]

    def blocked_tasks(self) -> list[dict]:
        """Every task with a not-yet-completed ancestor (transitive), with the chain depth of each blocker."""
        rows = self.tg.query(
            "MATCH path = (c:Task)-[:DEPENDS_ON*1..8]->(p:Task) "
            "WHERE (:User {id:$uid})-[:OWNS]->(c) AND coalesce(p.last_status, 'pending') <> 'completed' "
            "RETURN c.id, c.objective, p.id, p.last_status, p.objective, length(path)", {"uid": self.uid})
        out: dict[str, dict] = {}
        for cid, cobj, pid, pst, pobj, depth in rows:
            e = out.setdefault(cid, {"id": cid, "objective": cobj, "blockers": []})
            e["blockers"].append({"id": pid, "status": pst, "objective": pobj, "depth": depth})
        for e in out.values():
            e["blockers"].sort(key=lambda b: (b["depth"], b["id"]))
            e["root_causes"] = [b for b in e["blockers"] if b["status"] in ("failed", "cancelled")]
        return sorted(out.values(), key=lambda e: e["id"])

    def downstream(self, tid: str) -> list[dict]:
        """Everything that (transitively) depends on this task."""
        rows = self.tg.query(
            "MATCH (d:Task)-[:DEPENDS_ON*1..8]->(:Task {id:$tid}) WHERE (:User {id:$uid})-[:OWNS]->(d) "
            "RETURN DISTINCT d.id, d.objective", {"uid": self.uid, "tid": tid})
        return [dict(id=r[0], objective=r[1]) for r in rows]

    # ---- plans in the graph: the basis for impact analysis and /why ----
    def record_plan(self, tid: str, steps: list[dict]) -> None:
        """(Task)-[:PLANS]->(PlanStep)-[:USES_TOOL]->(Tool), (PlanStep)-[:NEEDS]->(Capability), (PlanStep)-[:AFTER]->(PlanStep)"""
        for s in steps:
            pid, cap = f"{tid}:{s['id']}", s.get("capability") or ""
            self.tg.query(
                "MATCH (:User {id:$uid})-[:OWNS]->(t:Task {id:$tid}) "
                "CREATE (t)-[:PLANS]->(:PlanStep {id:$pid, step:$sid, agent:$agent, kind:$kind, capability:$cap})",
                {"uid": self.uid, "tid": tid, "pid": pid, "sid": s["id"], "agent": s.get("agent", "assistant"),
                 "kind": s.get("kind", "llm"), "cap": cap})
            if s.get("tool_id"):
                self.tg.query("MATCH (p:PlanStep {id:$pid}), (tool:Tool {id:$tool}) MERGE (p)-[:USES_TOOL]->(tool)",
                              {"pid": pid, "tool": s["tool_id"]})
            if cap:
                self.tg.query("MATCH (p:PlanStep {id:$pid}) MERGE (c:Capability {name:$cap}) MERGE (p)-[:NEEDS]->(c)",
                              {"pid": pid, "cap": cap})
        for s in steps:
            for d in s.get("depends_on", []):
                self.tg.query("MATCH (a:PlanStep {id:$a}), (b:PlanStep {id:$b}) MERGE (b)-[:AFTER]->(a)",
                              {"a": f"{tid}:{d}", "b": f"{tid}:{s['id']}"})

    def impact(self, service: str) -> list[dict]:
        """IMPACT: live tasks whose plans use a tool of this service, plus tasks that depend on those."""
        rows = self.tg.query(
            "MATCH (:User {id:$uid})-[:OWNS]->(t:Task)-[:PLANS]->(:PlanStep)-[:USES_TOOL]->(:Tool)"
            "<-[:PROVIDES]-(:Service {name:$svc}) WHERE t.status IN ['scheduled', 'paused'] "
            "OPTIONAL MATCH (d:Task)-[:DEPENDS_ON*1..8]->(t) "
            "RETURN DISTINCT t.id, t.objective, collect(DISTINCT d.id)", {"uid": self.uid, "svc": service})
        return [dict(id=r[0], objective=r[1], downstream=[x for x in r[2] if x]) for r in rows]

    def explain_task(self, tid: str) -> dict | None:
        """WHY: the plan steps, ranked tool candidates (with reliability counts), preferences in force,
        dependencies, and the last run's agent handoff chain: all read from graph paths."""
        steps = self.tg.query(
            "MATCH (:User {id:$uid})-[:OWNS]->(t:Task {id:$tid})-[:PLANS]->(s:PlanStep) "
            "OPTIONAL MATCH (s)-[:USES_TOOL]->(tool:Tool) "
            "RETURN s.step, s.agent, s.kind, s.capability, tool.id, t.objective, t.status ORDER BY s.step",
            {"uid": self.uid, "tid": tid})
        if not steps:
            return None
        order = self.tg.query(
            "MATCH (:User {id:$uid})-[:OWNS]->(:Task {id:$tid})-[:PLANS]->(b:PlanStep)-[:AFTER]->(a:PlanStep) "
            "RETURN b.step, a.step", {"uid": self.uid, "tid": tid})
        parents = self.tg.query(
            "MATCH (:User {id:$uid})-[:OWNS]->(:Task {id:$tid})-[:DEPENDS_ON]->(p:Task) "
            "RETURN p.id, coalesce(p.last_status, 'pending')", {"uid": self.uid, "tid": tid})
        out_steps = []
        for step, agent, kind, cap, tool, _, _ in steps:
            e = {"step": step, "agent": agent, "kind": kind, "capability": cap or None, "tool": tool,
                 "after": [a for b, a in order if b == step], "candidates": []}
            if cap:
                e["candidates"] = self.resolve_capability(cap)       # ranked live from the graph
            out_steps.append(e)
        hist = self.task_history(tid, 1)
        return {"id": tid, "objective": steps[0][5], "status": steps[0][6], "steps": out_steps,
                "depends_on": [dict(id=p, status=st) for p, st in parents],
                "preferences": self.active(("global",)),
                "last_run": hist[0] if hist else None,
                "handoffs": self.handoffs(hist[0]["id"]) if hist else []}

    # ---- outreach dedupe in bulk ----
    def contacted_batch(self, names: list[str]) -> dict[str, dict]:
        keys = {norm(n): n for n in names if norm(n)}
        rows = self.tg.query(
            "UNWIND $keys AS k OPTIONAL MATCH (:User {id:$uid})-[r:CONTACTED]->(c:Company {key:k}) "
            "RETURN k, c.name, r.at", {"uid": self.uid, "keys": list(keys)})
        return {r[0]: {"name": r[1], "at": r[2]} for r in rows if r[1] is not None}

    def record_contacts(self, names: list[str], channel: str = "email") -> int:
        n = 0
        for name in names:
            if norm(name):
                self.record_contact(name, channel)
                n += 1
        return n


class GraphDisabled(ValueError):
    pass


class NullMemory:
    """Used when BUTLER_GRAPH=off. Every graph-backed capability degrades visibly: this is what the product
    looks like without FalkorDB (see `python -m butler.cli ablate`)."""
    enabled = False

    def __init__(self, *a, **k):
        pass

    def remember(self, *a, **k):
        raise GraphDisabled("memory needs the graph (BUTLER_GRAPH is off)")

    def active(self, *a, **k): return []
    def forget(self, *a, **k): return 0
    def why(self, *a, **k): return []
    def record_contact(self, *a, **k): return None
    def contact_trace(self, *a, **k): return None
    def contacted_batch(self, *a, **k): return {}
    def record_contacts(self, *a, **k): return 0
    def set_credential_ref(self, *a, **k): return None
    def remove_credential_ref(self, *a, **k): return None
    def record_task(self, *a, **k): return None
    def record_run(self, *a, **k): return None
    def task_history(self, *a, **k): return []
    def sync_tools(self, *a, **k): return None
    def revoke_service(self, *a, **k): return None
    def resolve_capability(self, *a, **k): return []
    def capabilities(self, *a, **k): return {}
    def tool_outcome(self, *a, **k): return None
    def record_dependency(self, *a, **k): return None
    def record_steps(self, *a, **k): return None
    def handoffs(self, *a, **k): return []
    def set_task_status(self, *a, **k): return None
    def blocking_parents(self, *a, **k): return []       # no readiness check: dependents run as soon as triggered
    def blocked_tasks(self, *a, **k): return None        # None = unavailable (vs [] = nothing blocked)
    def downstream(self, *a, **k): return []
    def record_plan(self, *a, **k): return None
    def impact(self, *a, **k): return None
    def explain_task(self, *a, **k): return None
