# Butler Architecture

This document describes Butler's design, component responsibilities,
graph-backed decisions, execution lifecycle, security boundaries, and
engineering trade-offs. Butler is an ongoing software project;
hackathons and demos are milestones, not its architectural boundary.

## 1. Design goals

Butler aims to be a persistent assistant that can reason over connected
context, use tools under explicit policy, and continue work after the
user leaves.

1.  **Connected context:** relationships among preferences,
    capabilities, tasks, dependencies, services, and outcomes influence
    decisions.
2.  **Durable execution:** tasks have explicit lifecycle state,
    checkpoints, bounded retries, recovery behavior, and approval waits.
3.  **Provider flexibility:** users configure supported LLM providers
    and provider ordering.
4.  **Controlled action:** the model proposes plans; application code
    validates and authorizes every action.
5.  **Explainability:** Butler can expose blockers, downstream effects,
    and relationships behind a decision.
6.  **Deployability:** SQLite/inline-worker configurations support
    simpler setups; PostgreSQL and Celery/Redis support multi-process
    deployments.

**The LLM is an untrusted advisor, not an authority.** Identity, grants,
budgets, approvals, idempotency, readiness, and execution state must be
enforced by application code and authoritative data.

## 2. System overview

``` text
                         User
                          |
                    Telegram bot
                          |                    HTTPS setup web app
                          |                   (provider/service secrets)
                          +-----------+-------------------+
                                      |
                                    Runtime
                                      |
              +-----------------------+----------------------+
              |              |              |               |
         Orchestrator    Model router     Planner       Tool executor
              |              |              |               |
              |           LLM APIs       Task runtime       |
              |                             |               |
              +-----------------------------+         MCP / direct APIs
                                      |
                              +-------+-------+
                              |               |
                           FalkorDB        SQL store
                         graph context   authoritative state
                              |
                      graph-backed decisions

                  Optional background work: Celery + Redis
```

  -----------------------------------------------------------------------
  Component                           Responsibility
  ----------------------------------- -----------------------------------
  Telegram bot                        Commands, messages, approval
                                      interactions, notifications.

  Runtime                             Builds and wires services; creates
                                      tasks and connects services.

  Orchestrator                        Coordinates context, model
                                      responses, and application logic.

  Model router                        Selects configured
                                      providers/models, applies ordering
                                      and budgets, handles typed
                                      failures, records usage.

  Planner                             Converts goals into bounded plans
                                      that are validated and persisted.

  Graph memory                        Stores user-scoped context and
                                      answers relationship-oriented
                                      queries in FalkorDB.

  Tool executor                       Policy gate for tool calls:
                                      capability/tool resolution, grants,
                                      schema validation, approval policy,
                                      execution records.

  Task worker                         Claims runs, checks dependencies,
                                      executes steps, checkpoints
                                      progress, retries, and recovers
                                      stale work.

  Payment service                     Prepares payment intents, applies
                                      limits/state transitions, requests
                                      approval, executes and reconciles.

  Setup web app                       Serves single-use HTTPS pages for
                                      provider and integration
                                      credentials.

  SQL store                           Owns users, credentials, grants,
                                      tasks/runs, approvals, payment
                                      intents, and other authoritative
                                      records.

  Celery application                  Dispatches and executes background
                                      work when enabled.

  CLI                                 Maintenance and diagnostics,
                                      including graph docs, graph
                                      ablation, and MCP discovery.
  -----------------------------------------------------------------------

Module names should be checked against the current source tree when
refactoring.

## 3. Data ownership: SQL and FalkorDB

The two stores serve different purposes.

### SQL is authoritative for

-   user identity and ownership;
-   encrypted credentials and integration grants;
-   task/run lifecycle records, checkpoints, and attempts;
-   approvals and argument fingerprints;
-   payment intents, payment state, and reconciliation;
-   records requiring atomic state transitions or idempotency.

A graph edge that mirrors authorization can help discover or rank tools,
but it does not replace the final SQL authorization check before
execution. SQLite supports straightforward single-machine use;
PostgreSQL is available for multi-worker deployments.

### FalkorDB supports connected context and decisions

The graph models user-scoped context and relationships among
preferences/memory, tasks and dependencies, plan steps and handoffs,
tools, capabilities, services, execution outcomes, and outreach
entities.

The graph is not just a log or visualization. Queries participate in
dependency readiness, transitive blocker analysis, downstream impact,
capability-to-tool resolution, duplicate-outreach checks, preference
retrieval, and path-based explanations.

### Consistency boundary

There is no distributed transaction across SQL and FalkorDB. The design
therefore uses explicit ownership and write-order rules:

-   SQL is written first for state that SQL owns.
-   Graph writes follow SQL writes.
-   Dependency registration and readiness fail closed; dependent work
    must not run if readiness cannot be safely established.
-   Some other graph writes are best-effort and can lag SQL after a
    graph-write failure.
-   Tool execution re-checks the authoritative SQL grant even if the
    graph helped find the tool.

Graph data can temporarily be stale. Changes to these rules should be
accompanied by failure-injection tests.

## 4. Graph model and query responsibilities

Treat [`docs/GRAPH.md`](docs/GRAPH.md) as the detailed reference for
labels, relationship names, and executable Cypher. It is generated from
source; regenerate it after query changes and run the documentation-sync
tests.

Core concepts include `User`, `Preference`/memory, `Task`, `PlanStep`,
`Tool`, `Capability`, `Service`, `Execution`/result, and
`Company`/contact. Relationship families include ownership, task
dependencies (`DEPENDS_ON`), plan ordering/handoffs, capability
availability (`AVAILABLE_THROUGH`), authorization mirrors
(`AUTHORIZED_FOR`), and execution/result provenance. Confirm exact names
in the generated graph documentation before using them in code or prose.

Graph-backed decisions include:

1.  **Dependency readiness:** establish whether parent tasks meet the
    conditions for a dependent task to run.
2.  **Transitive blockers:** find blocked tasks and the upstream
    relationships causing the block.
3.  **Downstream impact:** identify work affected by disconnecting or
    revoking a service.
4.  **Capability resolution:** discover candidate tools for a requested
    capability.
5.  **Path-based explanations:** combine task, step, service, and
    dependency relationships to explain availability or blockage.
6.  **Duplicate outreach:** use connected company/contact history to
    avoid repeated outreach within the supported workflow.
7.  **Preference retrieval:** retrieve scoped preferences relevant to an
    operation.

### Graph-off / ablation mode

The code includes a graph-disabled implementation and an ablation runner
for comparing selected scenarios with graph behavior enabled and
disabled. This tests whether the graph materially contributes to
behavior. Graph-off mode is diagnostic/degraded operation, not the
intended default; graph-dependent operations may report unavailable
rather than imply that no relationships exist.

## 5. Request and action lifecycle

1.  **Authenticate:** the Telegram handler checks the configured user
    allow-list.
2.  **Load context:** the runtime retrieves relevant preferences, task
    state, and capabilities.
3.  **Select a model:** the router considers configured providers,
    order, credentials, cooldowns, and budgets.
4.  **Propose:** the model returns a response or structured plan. It
    receives no unrestricted permissions.
5.  **Validate:** application code checks plan shape, supported
    capabilities, arguments, and limits.
6.  **Persist:** authoritative task and execution state is written to
    SQL; related graph relationships are updated.
7.  **Check readiness:** graph queries determine whether dependent work
    can run. Uncertain readiness must not silently permit execution.
8.  **Execute through the gate:** the tool executor resolves the tool,
    validates its schema, checks the SQL grant, and applies approval
    policy.
9.  **Checkpoint:** progress and results are persisted so recovery can
    distinguish completed work from unfinished work.
10. **Notify/explain:** the bot returns results or graph-backed
    explanations.

Plans are bounded and are not silently rewritten from untrusted tool
output. Tool results are data, not instructions that can add arbitrary
steps, permissions, or approvals.

## 6. Tasks and background work

Butler supports one-off tasks, scheduled tasks, and dependency-based
tasks. The runtime can use an inline worker or Celery/Redis.

-   **Atomic claims:** database transitions reduce the risk of two
    workers executing the same run.
-   **Checkpoints:** completed steps can be skipped during recovery
    where supported by the implementation.
-   **Bounded retries:** attempts are limited and should depend on
    failure type.
-   **Heartbeat recovery:** stale runs can be re-queued according to
    worker recovery logic.
-   **Approval waits:** runs waiting for human approval are not treated
    as ordinary abandoned runs.
-   **Dependency gating:** dependent work waits until graph-backed
    readiness is established.
-   **Pause/cancel semantics:** state changes depend on the run's actual
    state; an external action already submitted cannot necessarily be
    undone.

Inline and Celery workers must use the same task state machine. The
database is the correctness boundary, not the worker type.

## 7. Model routing and budgets

Provider credentials are stored per user and retrieved by the router;
application keys should not be hardcoded. The provider layer includes
OpenAI-compatible providers and a native Anthropic Messages API client.
Provider endpoints, model IDs, and request parameters can change, so
creating a provider object does not prove that a live request works.

Failure handling distinguishes authentication errors, rate limits,
temporary outages, invalid requests, and success. Authentication
failures can invalidate a credential; rate limits and outages can
trigger cooldowns; successful calls record usage/cost metadata.

Budgets are checked before calls. If concurrent requests are not
reserved atomically against a shared budget, simultaneous requests may
exceed the nominal limit. Treat request-level reservation as a separate
requirement until implemented and tested.

## 8. Tools, MCP, and integrations

Tools can be built in, implemented through direct APIs, or exposed by
MCP servers. MCP discovery does not grant permission to call every
discovered tool. Every call must pass the tool executor's capability
resolution, schema validation, grant check, and approval policy.

The current MCP client launches a subprocess per call. This reduces
shared session state but adds startup latency. A subprocess with a
reduced environment is not a complete security sandbox; third-party MCP
code must still be treated as trusted software and reviewed.

### Search, fetching, and email

Direct search/fetch/email tools include constraints such as URL/SSRF
checks, content and request limits, recipient provenance,
per-batch/daily caps, duplicate-outreach checks, and approval of the
prepared email batch. SMTP outcomes that cannot be determined safely
should remain unresolved instead of being blindly retried; SMTP has no
universal idempotency key.

### PayPal and payments

1.  Validate amount, currency, recipient, and limits.
2.  Prepare an intent.
3.  Show exact details to the user.
4.  Bind approval to those details.
5.  Re-check and execute only after approval.
6.  Reconcile using the same idempotency identity where supported.

Tasks may prepare an intent but must not independently authorize a real
transfer. Use sandbox credentials for testing. Do not describe a
provider flow as live-verified unless it has been exercised against the
actual provider.

## 9. Credential setup and security

Provider and integration secrets are entered through short-lived,
single-use HTTPS setup links rather than ordinary Telegram messages. The
implementation is designed to use hashed, atomically consumed setup
tokens; restrictive caching/referrer behavior; encryption at rest with
the configured master key; redacted provider errors; reduced HTTP-client
logging; owner checks; and approval fingerprints. The bot attempts to
delete messages that look like pasted API keys and warns users to revoke
exposed credentials.

`BUTLER_PUBLIC_URL` must begin with `https://` for setup links. In
deployment, terminate TLS at a properly configured reverse proxy. Keep
FalkorDB, PostgreSQL, and Redis private.

  -----------------------------------------------------------------------
  Threat                              Main control / residual boundary
  ----------------------------------- -----------------------------------
  Unauthorized Telegram user          Configured Telegram ID allow-list.

  Cross-user access                   User-scoped graph and SQL ownership
                                      checks.

  Credential leak                     HTTPS setup, encrypted storage,
                                      redacted errors/logging.

  Prompt injection                    External output is data; tools
                                      still pass policy checks.

  Invented tool/arguments             Capability resolution, schemas,
                                      grants, deny-by-default.

  Approval replay or argument changes Fingerprint bound to exact
                                      arguments; atomic claim and expiry.

  Duplicate payment                   State transitions, idempotency
                                      identity, unresolved state and
                                      reconciliation.

  Accidental live payment             Sandbox-by-default configuration
                                      and explicit live-mode gate.

  Runaway usage                       Per-user budgets and bounded
                                      plan/attempt counts.

  SSRF                                Scheme/port restrictions,
                                      public-address checks, redirect
                                      re-validation, size/time limits;
                                      DNS rebinding remains a residual
                                      concern.

  Unsafe email outreach               Provenance, caps, dedupe,
                                      exact-batch approval, no blind
                                      retry after unknown outcome.
  -----------------------------------------------------------------------

These safeguards are not a claim of complete security. Host compromise,
denial of service, third-party vulnerabilities, and malicious MCP server
code are not fully addressed.

## 10. Deployment

The recommended Compose deployment separates roles built from the
application image:

-   `bot`: Telegram polling;
-   `web`: setup pages;
-   `worker`: Celery execution;
-   `beat`: scheduled dispatch, if enabled;
-   `postgres`: relational database;
-   `redis`: broker/worker coordination;
-   `falkordb`: graph database.

The Compose configuration should persist database data in named volumes.
Bind the web port to loopback when using a reverse proxy, and do not
expose database/broker ports publicly.

``` bash
docker compose up -d --build
docker compose ps
docker compose logs -f web bot worker beat
```

One systemd unit can manage the Compose project by starting it and
stopping it on service shutdown. Match the unit to the actual checkout
path and Compose configuration. For local development, direct Python
entry points and an inline worker are also available; use the current
`.env.example`, `pyproject.toml`, and source tree for exact commands.

## 11. Testing and verification

The latest code walkthrough recorded 199 tests across database backends,
Telegram handlers, Celery, credentials/setup pages, graph decisions,
integrations, MCP, memory, orchestration, payments, planning, routing,
security/spend controls, and tasks. Re-run `pytest -q` and update this
count whenever the suite changes.

Tests have exercised selected real local components, including
PostgreSQL, Redis/Celery, FalkorDB, an in-process SMTP server, and an
MCP server over stdio. A Telegram handler tested with a stubbed Bot API
is not equivalent to a live Telegram end-to-end test.

Recorded verification gaps include some or all of: Telegram servers, LLM
provider APIs/current model IDs, Brave Search/public internet fetch,
real mail providers, PayPal OAuth/Payouts and MCP calls with real
credentials, optional fetch MCP, and load/long-running reliability.
These are verification gaps, not necessarily missing code. Document
controlled end-to-end tests before claiming production verification.

Use the current CLI help/source for exact diagnostic arguments. Graph
documentation generation, graph ablation, and MCP discovery are
available in the current implementation.

## 12. Known limitations and trade-offs

-   No distributed transaction spans SQL and FalkorDB; non-critical
    graph writes can lag or fail after SQL commits.
-   A recurring parent task's displayed terminal status can be stale
    while a retry is in progress.
-   Chat history is held in memory and may be lost on restart; semantic
    retrieval is not implemented for all memory paths.
-   Some graph lookups use label scans; consider indexes for `Task.id`,
    `Tool.id`, and `PlanStep.id` if graphs grow.
-   The database adapter uses one lock-guarded connection per process
    rather than a connection pool.
-   One MCP subprocess per call adds latency and is not a full sandbox.
-   Plans are sequential and immutable after creation; general automatic
    replanning after failure is not implemented.
-   Budgets are per user rather than atomically reserved per request;
    concurrent calls may exceed nominal limits.
-   Default timezone is UTC unless a timezone is supplied.
-   The MCP SDK is pinned to 1.x; upgrades require adapting and testing
    the client API.



## 13. Engineering principles

1.  Keep SQL authoritative for security-sensitive and transactional
    state.
2.  Make graph queries part of real product decisions, not decorative
    output.
3.  Route every external action through the same policy gate.
4.  Treat model output and external content as untrusted input.
5.  Bind approvals to exact arguments and re-check authorization at
    execution.
6.  Prefer idempotent state transitions and explicit unresolved outcomes
    over blind retries.
7.  Fail closed when dependencies or authorization cannot be
    established.
8.  Keep generated graph documentation synchronized with executable
    queries.
9.  Test retries, concurrency, recovery, and partial external
    failures---not only happy paths.
10. Distinguish automated test coverage from live provider verification.

## 14. Glossary

-   **Capability:** kind of action needed, independent of a particular
    tool.
-   **Tool:** concrete callable exposed by Butler or an integration.
-   **Service:** connected integration that provides tools or API
    operations.
-   **Grant:** authoritative permission for a user to access service
    tools.
-   **Approval:** human decision bound to a prepared action and exact
    arguments.
-   **Intent:** prepared payment operation awaiting execution or
    reconciliation.
-   **Run:** one execution of a task.
-   **Checkpoint:** persisted progress used for recovery.
-   **Heartbeat:** timestamp used to determine worker progress.
-   **Handoff:** relationship connecting one step's result to another
    step.
-   **Readiness:** whether dependencies are satisfied, determined
    through graph relationships.
-   **Fail closed:** refuse or wait when a safety condition cannot be
    established.
-   **Ablation:** compare behavior with a component enabled and
    disabled.
