# Graph data model and queries

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


## Store (`butler/graph/store.py`)

### `ensure_schema`

```cypher
CREATE INDEX FOR (u:User) ON (u.id)
```

```cypher
CREATE INDEX FOR (p:Preference) ON (p.key)
```

```cypher
CREATE INDEX FOR (c:Company) ON (c.key)
```

## Memory (`butler/graph/memory.py`)

### `remember`

```cypher
MATCH (:User {id:$uid})-[:HAS_PREFERENCE]->(p:Preference {key:$key, scope:$scope, status:'active'}) RETURN p.id
```

```cypher
MATCH (u:User {id:$uid}) CREATE (u)-[:HAS_PREFERENCE]->(:Preference {id:$id,key:$key,value:$value,scope:$scope,source:$source,confidence:$conf,status:'active',created:$now})
```

```cypher
MATCH (n:Preference {id:$new}), (o:Preference {id:$old}) SET o.status='superseded' CREATE (n)-[:SUPERSEDES]->(o)
```

### `active`

```cypher
MATCH (:User {id:$uid})-[:HAS_PREFERENCE]->(p:Preference {status:'active'}) WHERE p.scope IN $scopes RETURN p.key, p.value, p.scope, p.source ORDER BY p.created DESC
```

### `forget`: Hard delete (including superseded history) -- 'forget' means gone.

```cypher
MATCH (:User {id:$uid})-[:HAS_PREFERENCE]->(p:Preference {key:$key}) WHERE $scope IS NULL OR p.scope=$scope DETACH DELETE p
```

```cypher
MATCH (:User {id:$uid})-[:HAS_PREFERENCE]->(p:Preference {key:$key}) WHERE $scope IS NULL OR p.scope=$scope RETURN count(p)
```

### `why`: Graph trace for a preference: current value and what it superseded.

```cypher
MATCH (:User {id:$uid})-[:HAS_PREFERENCE]->(p:Preference {key:$key, status:'active'}) OPTIONAL MATCH (p)-[:SUPERSEDES*1..]->(o:Preference) RETURN p.value, p.scope, p.source, p.created, o.value ORDER BY o.created DESC
```

### `record_contact`

```cypher
MATCH (u:User {id:$uid}) MERGE (c:Company {key:$k}) ON CREATE SET c.name=$name MERGE (u)-[r:CONTACTED]->(c) SET r.at=$now, r.channel=$ch
```

### `contact_trace`

```cypher
MATCH (:User {id:$uid})-[r:CONTACTED]->(c:Company {key:$k}) RETURN c.name, r.at, r.channel
```

### `set_credential_ref`

```cypher
MATCH (u:User {id:$uid}) MERGE (c:CredentialReference {id:$cid}) SET c.provider=$p, c.model=$m, c.last4=$l MERGE (u)-[:HAS_CREDENTIAL]->(c)
```

### `remove_credential_ref`

```cypher
MATCH (:User {id:$uid})-[:HAS_CREDENTIAL]->(c:CredentialReference {id:$cid}) DETACH DELETE c
```

### `record_task`

```cypher
MATCH (u:User {id:$uid}) MERGE (t:Task {id:$tid}) ON CREATE SET t.last_status='pending' SET t.objective=$o, t.schedule=$s, t.status=$st MERGE (u)-[:OWNS]->(t)
```

### `record_run`

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(t:Task {id:$tid}) CREATE (t)-[:HAS_RUN]->(:Run {id:$rid, status:$st, provider:$p, model:$m, at:$now})
```

### `task_history`

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(:Task {id:$tid})-[:HAS_RUN]->(r:Run) RETURN r.id, r.status, r.provider, r.model, r.at ORDER BY r.at DESC LIMIT $n
```

### `sync_tools`

```cypher
MATCH (u:User {id:$uid}) MERGE (s:Service {name:$svc}) MERGE (t:Tool {id:$tid}) ON CREATE SET t.ok=0, t.fail=0 SET t.risk=$risk, t.risk_rank=$rank, t.approval=$ap, t.financial=$fin, t.status='available' MERGE (s)-[:PROVIDES]->(t) MERGE (c:Capability {name:$cap}) MERGE (t)-[:HAS_CAPABILITY]->(c) MERGE (u)-[:AUTHORIZED_FOR]->(t)
```

### `revoke_service`

```cypher
MATCH (u:User {id:$uid})-[r:AUTHORIZED_FOR]->(:Tool)<-[:PROVIDES]-(:Service {name:$svc}) DELETE r
```

### `resolve_capability`: Graph-driven tool selection: authorized tools with this capability, safest and most reliable first.

```cypher
MATCH (:User {id:$uid})-[:AUTHORIZED_FOR]->(t:Tool)-[:HAS_CAPABILITY]->(:Capability {name:$cap}) WHERE t.status='available' RETURN t.id, t.risk, t.ok, t.fail ORDER BY t.risk_rank ASC, (t.ok - t.fail) DESC, t.id ASC
```

### `capabilities`

```cypher
MATCH (:User {id:$uid})-[:AUTHORIZED_FOR]->(t:Tool)-[:HAS_CAPABILITY]->(c:Capability) RETURN c.name, collect(t.id) ORDER BY c.name
```

### `tool_outcome`

```cypher
MATCH (u:User {id:$uid}), (t:Tool {id:$tid}) SET t.ok = t.ok + $a, t.fail = t.fail + $b MERGE (u)-[r:USED]->(t) SET r.last=$now
```

### `record_dependency`: (child)-[:DEPENDS_ON]->(parent): READ by the scheduler (blocking_parents), /blocked and downstream().

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(c:Task {id:$c}), (:User {id:$uid})-[:OWNS]->(p:Task {id:$p}) MERGE (c)-[:DEPENDS_ON]->(p)
```

### `record_steps`: steps: [{id, agent, status, depends_on}] -> (Run)-[:HAS_STEP]->(Step), (Agent)-[:EXECUTED]->(Step),

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(:Task {id:$tid})-[:HAS_RUN]->(r:Run {id:$rid}) CREATE (r)-[:HAS_STEP]->(st:Step {id:$sid, run:$rid, agent:$agent, status:$status}) MERGE (g:Agent {name:$agent}) CREATE (g)-[:EXECUTED]->(st)
```

```cypher
MATCH (a:Step {run:$rid, id:$a}), (b:Step {run:$rid, id:$b}) CREATE (a)-[:HANDED_OFF_TO]->(b)
```

### `handoffs`

```cypher
MATCH (a:Step {run:$rid})-[:HANDED_OFF_TO]->(b:Step {run:$rid}) RETURN a.agent, a.id, b.agent, b.id
```

### `set_task_status`: Keep the graph's view of a task current: status (scheduled/paused/cancelled/...) and last_status

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(t:Task {id:$tid}) SET t.status=$st
```

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(t:Task {id:$tid}) SET t.last_status=$ls
```

### `blocking_parents`: READINESS: direct dependencies whose latest run has not completed. The scheduler won't claim a task

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(c:Task {id:$tid})-[:DEPENDS_ON]->(p:Task) WHERE coalesce(p.last_status, 'pending') <> 'completed' RETURN p.id, p.last_status, p.objective
```

### `blocked_tasks`: Every task with a not-yet-completed ancestor (transitive), with the chain depth of each blocker.

```cypher
MATCH path = (c:Task)-[:DEPENDS_ON*1..8]->(p:Task) WHERE (:User {id:$uid})-[:OWNS]->(c) AND coalesce(p.last_status, 'pending') <> 'completed' RETURN c.id, c.objective, p.id, p.last_status, p.objective, length(path)
```

### `downstream`: Everything that (transitively) depends on this task.

```cypher
MATCH (d:Task)-[:DEPENDS_ON*1..8]->(:Task {id:$tid}) WHERE (:User {id:$uid})-[:OWNS]->(d) RETURN DISTINCT d.id, d.objective
```

### `record_plan`: (Task)-[:PLANS]->(PlanStep)-[:USES_TOOL]->(Tool), (PlanStep)-[:NEEDS]->(Capability), (PlanStep)-[:AFTER]->(PlanStep)

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(t:Task {id:$tid}) CREATE (t)-[:PLANS]->(:PlanStep {id:$pid, step:$sid, agent:$agent, kind:$kind, capability:$cap})
```

```cypher
MATCH (p:PlanStep {id:$pid}), (tool:Tool {id:$tool}) MERGE (p)-[:USES_TOOL]->(tool)
```

```cypher
MATCH (p:PlanStep {id:$pid}) MERGE (c:Capability {name:$cap}) MERGE (p)-[:NEEDS]->(c)
```

```cypher
MATCH (a:PlanStep {id:$a}), (b:PlanStep {id:$b}) MERGE (b)-[:AFTER]->(a)
```

### `impact`: IMPACT: live tasks whose plans use a tool of this service, plus tasks that depend on those.

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(t:Task)-[:PLANS]->(:PlanStep)-[:USES_TOOL]->(:Tool)<-[:PROVIDES]-(:Service {name:$svc}) WHERE t.status IN ['scheduled', 'paused'] OPTIONAL MATCH (d:Task)-[:DEPENDS_ON*1..8]->(t) RETURN DISTINCT t.id, t.objective, collect(DISTINCT d.id)
```

### `explain_task`: WHY: the plan steps, ranked tool candidates (with reliability counts), preferences in force,

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(t:Task {id:$tid})-[:PLANS]->(s:PlanStep) OPTIONAL MATCH (s)-[:USES_TOOL]->(tool:Tool) RETURN s.step, s.agent, s.kind, s.capability, tool.id, t.objective, t.status ORDER BY s.step
```

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(:Task {id:$tid})-[:PLANS]->(b:PlanStep)-[:AFTER]->(a:PlanStep) RETURN b.step, a.step
```

```cypher
MATCH (:User {id:$uid})-[:OWNS]->(:Task {id:$tid})-[:DEPENDS_ON]->(p:Task) RETURN p.id, coalesce(p.last_status, 'pending')
```

### `contacted_batch`

```cypher
UNWIND $keys AS k OPTIONAL MATCH (:User {id:$uid})-[r:CONTACTED]->(c:Company {key:k}) RETURN k, c.name, r.at
```
