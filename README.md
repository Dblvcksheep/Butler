# Butler

**A graph-native personal AI assistant that remembers context, plans
work, and takes actions with safeguards.**

Butler is a persistent, Telegram-first assistant. It connects language
models to personal context, tools, integrations, and durable tasks.
FalkorDB is an active part of its decision-making: relationships among
preferences, capabilities, tasks, dependencies, services, and outcomes
inform what Butler can do and help explain why.

Butler is an ongoing software project.

Live: [https://t.me/D_butler_bot](https://t.me/D_butler_bot)


Telegram Command Explanation in `docs/butler_command_reference.md`

## What it does

-   **Bring your own model provider:** configure supported providers
    through a secure setup page; route requests across models and record
    usage.
-   **Graph-backed context:** use FalkorDB for dependency readiness,
    blocker analysis, downstream impact, capability-to-tool resolution,
    duplicate-outreach checks, and explanations.
-   **Durable work:** create one-off, scheduled, and dependency-based
    tasks with checkpoints, bounded retries, recovery, and approval
    waits.
-   **Connected tools:** use built-in tools, direct APIs, and supported
    Model Context Protocol (MCP) integrations.
-   **Human-controlled actions:** application code---not the
    model---checks identity, permissions, schemas, limits, approvals,
    and execution state.

## Architecture

``` text
Telegram user ------> Bot / Runtime <------> HTTPS setup pages
                           |
              +------------+-------------+
              |            |             |
          Orchestrator  Model router   Planner / tasks
              |            |             |
              |         LLM APIs       Workers
              |                          |
              +----------+---------------+
                         |
                 Tool execution gate
                         |
              MCP servers / direct APIs

              +----------+----------+
              |                     |
           FalkorDB              SQL store
       graph context and      authoritative state:
       relationship logic     users, grants, approvals,
                              tasks, runs, payments
                  Optional Celery + Redis
```

SQL remains authoritative for sensitive state such as permissions,
approvals, payments, and execution records. FalkorDB is used for
graph-based context and decisions, not just visualization. See
[`architecture.md`](architecture.md) and
[`docs/GRAPH.md`](docs/GRAPH.md).

## Technology

-   Python, `python-telegram-bot`, Flask, HTTPX
-   FalkorDB and Cypher
-   SQLite for simple deployments; PostgreSQL for multi-worker
    deployments
-   Redis and Celery for background work when enabled
-   LLM provider adapters and MCP clients
-   Docker Compose for server deployment

## Quick start

### Requirements

Docker and Docker Compose, a Telegram bot token from
[@BotFather](https://t.me/BotFather), at least one supported LLM
provider key, and a public HTTPS URL if using secure credential setup.
Some MCP integrations also require Node.js/npm.

### 1. Get the source and configure it

``` bash
git clone <repository-url>
cd <repository-directory>
cp .env.example .env
```

Replace the clone placeholders with your repository details. Configure
the values documented in `.env.example`, including:

-   `TELEGRAM_BOT_TOKEN`
-   `BUTLER_ALLOWED_USERS` --- comma-separated numeric Telegram user IDs
-   `BUTLER_MASTER_KEY` --- encryption key; generate it using the
    instructions in `.env.example`
-   `BUTLER_PUBLIC_URL` --- public HTTPS origin for secure setup pages
-   database, FalkorDB, and Redis settings as required

Do not commit `.env` or paste secrets into Telegram. `/addkey` and
`/connect` require `BUTLER_PUBLIC_URL` to begin with `https://`.

### 2. Start the application

``` bash
docker compose up -d --build
docker compose ps
docker compose logs -f web bot worker beat
```

If service names differ in your checkout, use `docker compose ps` to see
the configured names. Keep databases and Redis private; expose the web
service through a properly configured HTTPS reverse proxy.

### 3. Open the Telegram bot

Send `/start` to your bot. The command overview is shown there. `/help`
can be registered as an alias for `/start`; if it is not registered in
your current checkout, use `/start`.

Configure a model through the secure flow:

``` text
/addkey openai
/models
```

Open the one-time HTTPS setup link promptly and enter the API key only
on that page.

## Common commands

Check `/start` in the running bot for authoritative syntax. Common
commands include:

  -----------------------------------------------------------------------
  Purpose                             Examples
  ----------------------------------- -----------------------------------
  Models                              `/addkey openai`, `/models`,
                                      `/removekey <id>`,
                                      `/modelorder default openai`

  Usage limits                        `/usage`, `/budget tokens <n>`,
                                      `/budget cost <amount>`,
                                      `/budget off`

  Chat/tasks                          ordinary message, `/do <goal>`,
                                      `/task ...`, `/plan ...`, `/tasks`

  Task control                        `/pause <id>`, `/resume <id>`,
                                      `/cancel <id>`

  Memory                              `/remember <key>: <value>`,
                                      `/forget <key>`, `/prefs`

  Graph explanations                  `/blocked`, `/impact <service>`,
                                      `/why task <id>`, `/history <id>`,
                                      `/trace`

  Integrations                        `/connect <service>`,
                                      `/disconnect <service>`, `/tools`

  Payments                            `/pay ...`, `/status <id>`
  -----------------------------------------------------------------------

Never send API keys, passwords, OAuth secrets, or other credentials as
ordinary Telegram messages.

## Graph data model

The graph models user context and relationships among preferences,
tasks, plan steps, tools, capabilities, services, executions, and
outreach entities. Exact labels, relationships, and executable Cypher
queries are documented in [`docs/GRAPH.md`](docs/GRAPH.md).

Graph-backed operations include dependency readiness, transitive
blockers, downstream impact when a service changes, capability-to-tool
resolution, path-based explanations, and supported duplicate-outreach
checks. The project includes graph-off/ablation functionality to compare
selected scenarios with and without graph behavior.

## Security principles

-   The model is an untrusted advisor; application code enforces policy.
-   Credentials are entered through HTTPS setup pages and encrypted at
    rest.
-   Telegram users are allow-listed; ownership checks apply to
    user-owned objects and approvals.
-   Sensitive actions require approval of the exact prepared details.
-   Tool access is deny-by-default and validated against grants and
    schemas.
-   Untrusted tool output is treated as data, not instructions.
-   Uncertain dependency or authorization state should fail closed.

These safeguards do not eliminate risks such as host compromise, denial
of service, third-party vulnerabilities, or malicious MCP server code.

## Testing

``` bash
pytest -q
```

Check the current test configuration for optional services and
integration requirements. Local tests---even those using real local
components---do not prove every external provider has been tested live.
Document which integrations have been verified against their actual
services.

## Project documentation

-   [`architecture.md`](architecture.md): system design, graph
    decisions, execution lifecycle, security boundaries, trade-offs, and
    limitations.
-   [`docs/GRAPH.md`](docs/GRAPH.md): graph model and Cypher queries
    generated from source.
-   [`.env.example`](.env.example): configuration reference.
-   [`docker-compose.yml`](docker-compose.yml): service deployment.

## Continuing development

Butler is designed to evolve beyond any individual demo or event.
Engineering priorities include improving SQL/graph consistency and
recovery, persistent context retrieval, live end-to-end integration
verification, observability and load testing, task recovery/replanning,
and stronger isolation for third-party MCP processes. Roadmap items are
not claims that the work is already implemented.

## AI-assisted development disclosure

AI coding tools were used extensively during development, including for
implementation, tests, and documentation. The maintainer is responsible
for project direction, integration, review, testing, and technical
decisions. Keep this disclosure accurate as contributors and tools
change.

## License

See [`LICENSE`](LICENSE).
