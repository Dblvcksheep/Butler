# Butler Telegram AI Assistant — Command Reference

This document explains Butler's command system, including AI model configuration, usage limits, external tools, task execution, graph-based dependencies, memory, and payments.

> **Important:** The examples below explain the intended meaning suggested by the command syntax. The exact behaviour depends on Butler's implementation, including its command handlers, integrations, permissions, and scheduler. Example model names, IDs, prices, and outputs are illustrative.

## Table of Contents

1. [Models: AI Providers and Model Routing](#1-models-ai-providers-and-model-routing)
2. [Spend: Usage Tracking and Budgets](#2-spend-usage-tracking-and-budgets)
3. [Tools: External Service Integrations](#3-tools-external-service-integrations)
4. [Work: Planning and Executing Tasks](#4-work-planning-and-executing-tasks)
5. [Graph: Dependencies and Impact Analysis](#5-graph-dependencies-and-impact-analysis)
6. [Memory: Persistent Information and Preferences](#6-memory-persistent-information-and-preferences)
7. [Payments: Payment Operations and Status](#7-payments-payment-operations-and-status)
8. [How the Components Work Together](#8-how-the-components-work-together)
9. [Suggested Testing Sequence](#9-suggested-testing-sequence)
10. [Implementation and Security Considerations](#10-implementation-and-security-considerations)

---

## 1. Models: AI Providers and Model Routing

This group configures which AI providers and models Butler can use, the order in which it tries them, and the prices it uses to estimate model costs.

Different models have different strengths, response speeds, availability, and prices. A model-routing system can choose a suitable model for each task and potentially fall back to another model if a request fails.

### `/addkey [provider]`

**Purpose:** Add an API key for an AI provider.

**Syntax:**

```text
/addkey [provider]
```

**Example:**

```text
/addkey openai
```

Butler should provide a secure, one-time link or another protected mechanism for entering the provider's API key. The key should not be pasted into an ordinary Telegram message.

Depending on the providers supported by Butler, you might configure providers such as OpenAI, Anthropic, or Google.

**Why it is useful:**

- Gives Butler permission to make requests to the provider's API.
- Enables models associated with that provider.
- Allows you to configure more than one provider for fallback or task-specific routing.

**Things to verify:**

- The key is stored securely and is not exposed in logs or model prompts.
- Butler confirms whether the key is valid.
- The key is associated with the correct user or account.
- The system clearly reports which models become available after the key is added.

### `/models`

**Purpose:** List configured or available models.

**Syntax:**

```text
/models
```

An illustrative response might look like:

```text
Configured Models

1. openai/example-model-a
2. anthropic/example-model-b
3. google/example-model-c
```

The names above are placeholders, not a statement about which models Butler currently supports.

This command helps you inspect the models Butler can potentially use. Depending on the implementation, it may list configured models, models reachable through saved provider keys, or both.

**Why it is useful:**

- Confirms which models are configured.
- Helps you identify a model ID for other commands.
- Helps diagnose missing keys, unavailable models, or configuration errors.

**Important distinction:** A model appearing in a configuration list does not necessarily mean the provider will accept requests for it. Access, model names, provider configuration, and account permissions must still be correct.

### `/removekey id`

**Purpose:** Remove a saved provider key or credential identified by an ID.

**Syntax:**

```text
/removekey <id>
```

**Example:**

```text
/removekey 2
```

This example assumes `2` is a valid key ID in Butler's configuration. Confirm which IDs the command expects before using it: a key ID and a model ID are not necessarily the same thing.

**Possible reasons to remove a key:**

- You no longer want Butler to use that provider.
- You need to replace an expired or compromised key.
- You want to revoke a provider connection.

Removing a key may make models that depend on it unavailable.

**Security note:** Removing a key from Butler's storage may not revoke the key at the provider. If a key is compromised, revoke it in the provider's own account or developer dashboard as well.

### `/modelorder kind p1,p2`

**Purpose:** Set the preferred provider or model order for a task category.

**Syntax:**

```text
/modelorder <kind> <provider1>,<provider2>,...
```

**Example:**

```text
/modelorder coding openai,anthropic,google
```

This expresses a preference for trying OpenAI first for `coding` tasks, then Anthropic, then Google.

A router might use this order to select a model and try alternatives when a provider fails. However, fallback behaviour depends on the router implementation. Some systems fall back only for certain errors; others may not retry all failures.

Possible task categories include `chat`, `coding`, `reasoning`, or `summarization`, but you must use the categories actually supported by Butler.

Another illustrative example:

```text
/modelorder chat google,openai
/modelorder coding anthropic,openai
```

**Why it is useful:**

- Prioritizes a preferred model for a particular kind of work.
- Allows different routing preferences for different tasks.
- Can improve resilience when more than one provider is configured.
- Can help balance quality, speed, and cost.

**Things to verify:**

- Whether `p1`, `p2`, and similar values mean providers or specific model IDs.
- Whether provider names are case-sensitive.
- What happens if a preferred provider has no usable model.
- Which errors trigger fallback and whether retries are limited.
- Whether the router records which model ultimately handled the task.

### `/price id in out`

**Purpose:** Configure the input and output prices used for cost estimation.

**Syntax:**

```text
/price <id> <input_price> <output_price>
```

**Illustrative example:**

```text
/price 1 2.50 10.00
```

If the implementation expects prices in USD per million tokens, this would mean:

- Model ID: `1`
- Input price: `$2.50` per million input tokens
- Output price: `$10.00` per million output tokens

**Always verify the unit expected by the implementation.** The command syntax alone does not establish whether prices are per token, per thousand tokens, or per million tokens.

#### Example cost calculation

Assume the configured prices are:

| Token type | Usage | Illustrative price per million tokens |
|---|---:|---:|
| Input | 10,000 | $2.50 |
| Output | 2,000 | $10.00 |

Input cost:

```text
10,000 / 1,000,000 × $2.50 = $0.025
```

Output cost:

```text
2,000 / 1,000,000 × $10.00 = $0.020
```

Total estimated cost:

```text
$0.025 + $0.020 = $0.045
```

The estimated cost is **$0.045**, or 4.5 cents, under these illustrative assumptions.

**Why it is useful:**

- Supports cost reporting.
- Helps enforce cost-based budgets.
- Makes it easier to compare the estimated cost of different models.

**Accuracy considerations:**

- Keep configured prices up to date.
- Confirm whether cached input tokens, reasoning tokens, tool calls, or other billable categories require separate handling.
- Use the actual token usage returned by the provider where possible.
- Make clear that Butler's estimate may differ from the provider's final bill.

---

## 2. Spend: Usage Tracking and Budgets

This group helps you inspect AI consumption and place limits on how much Butler can spend or how many tokens it can use.

### `/usage`

**Purpose:** Display recorded AI usage.

**Syntax:**

```text
/usage
```

An illustrative response might be:

```text
AI Usage Summary

Input tokens: 150,000
Output tokens: 45,000
Estimated cost: $0.82
```

These values are examples only.

Depending on the implementation, the report may show usage for the current user, a particular period, or the entire system.

**Useful usage metrics include:**

- Input tokens.
- Output tokens.
- Estimated cost.
- Usage by provider or model.
- Usage by task or workflow.
- Budget remaining.
- Time period covered by the report.

**Why it is useful:**

- Identifies expensive tasks or models.
- Helps investigate unexpected usage.
- Helps determine whether a budget is appropriate.
- Provides visibility into how much AI activity Butler generates.

A good implementation should make the reporting period clear and distinguish recorded usage from estimated cost.

### `/budget tokens N | cost X | off`

**Purpose:** Configure or disable an AI usage budget.

The command has three forms.

#### A. Token budget

```text
/budget tokens 100000
```

This might set a limit of 100,000 tokens. The implementation must define whether this means input tokens, output tokens, or their combined total.

It must also define the scope and reset period. For example, a budget might apply per user, per day, or to a particular task.

#### B. Cost budget

```text
/budget cost 5
```

If the implementation interprets the value as US dollars, this sets a cost limit of `$5`. The currency and period must be confirmed from the implementation.

A cost budget can be useful when a task might make many model calls, such as researching a topic, executing a plan, or repeatedly revising an answer.

#### C. Disable the budget

```text
/budget off
```

This disables the configured budget restriction, if that is how Butler implements the command.

Disabling a budget can be convenient during testing, but it removes an important safeguard against unexpected usage.

### How budget enforcement should work

A budget is only useful if Butler checks it at the right points.

A robust design should:

1. Check the relevant budget before starting a model request.
2. Estimate or reserve the expected cost when practical.
3. Record actual usage after the request.
4. Update the remaining budget.
5. Prevent or safely stop additional requests when the limit is reached.
6. Handle concurrent tasks so they cannot all spend against the same remaining budget without coordination.

**Important:** A Butler-side budget is not necessarily the same as a provider-enforced billing limit. Butler can only control the requests and usage it tracks correctly.

---

## 3. Tools: External Service Integrations

A language model can generate text, but it cannot automatically access an external account or perform an action just because a user requests it. Butler needs integrations that expose those capabilities.

### `/connect paypal|email|search`

**Purpose:** Connect an external service supported by Butler.

The command lists PayPal, email, and search as possible services. The actual authentication flow and capabilities depend on the integrations implemented in the project.

#### A. Connect PayPal

```text
/connect paypal
```

This could connect Butler to PayPal for supported payment-related operations.

Depending on the API and permissions, the integration might support actions such as:

- Retrieving payment information.
- Checking transaction status.
- Creating supported payment requests.
- Initiating supported transfers.

**Connecting PayPal does not automatically enable every PayPal capability.** Available operations depend on the integration, API permissions, account eligibility, and any safeguards implemented in Butler.

#### B. Connect email

```text
/connect email
```

This could connect Butler to an email service.

Depending on the integration, it might allow Butler to:

- Search messages.
- Read messages.
- Summarize email threads.
- Draft replies.
- Send messages.
- Retrieve information from previous conversations.

For example, you might ask Butler to find a recent client email and prepare a reply.

Reading messages, creating drafts, and sending messages are different levels of access. A careful integration should use appropriate permissions and distinguish between preparing an action and carrying it out.

#### C. Connect search

```text
/connect search
```

This could connect a web search API or another search service.

For example, Butler might search for recent framework updates, retrieve relevant sources, and summarize the results.

The command name alone does not establish whether the integration performs web search, searches a private knowledge base, or uses another retrieval service.

### `/disconnect svc`

**Purpose:** Disconnect or disable a connected service.

**Syntax:**

```text
/disconnect <service>
```

**Example:**

```text
/disconnect paypal
```

This could disable Butler's PayPal integration.

Possible reasons to disconnect a service include:

- You no longer need it.
- You want to reconnect using another account.
- You need to restrict Butler's access.
- You are troubleshooting an integration.

**Important:** Disconnecting a service inside Butler may not revoke the provider's authorization, cancel existing transactions, or undo actions already performed. Those behaviours depend on the integration. If access must be fully revoked, check the provider's own account settings as well.

### `/tools`

**Purpose:** List the tools known to Butler.

**Syntax:**

```text
/tools
```

A useful output might distinguish between:

1. **Implemented:** The integration exists in the codebase.
2. **Connected:** Credentials or authorization have been configured.
3. **Available:** The tool is currently usable by the assistant.

For example, the email integration might be implemented but not connected to an account. In that case, Butler should not claim it can read or send email yet.

**Why this command is useful:**

- Confirms which integrations are available.
- Helps troubleshoot authentication problems.
- Shows what external actions the assistant may be able to perform.
- Makes the assistant's capabilities more transparent.

---

## 4. Work: Planning and Executing Tasks

The Work commands turn Butler from a question-and-answer bot into a task-oriented assistant.

A task-oriented assistant can receive a goal, plan the work, use tools, track progress, and report the result. The extent to which Butler performs these steps automatically depends on its planner, task runner, integrations, and persistence layer.

### `/do goal`

**Purpose:** Ask Butler to work toward a goal.

**Syntax:**

```text
/do <goal>
```

**Example:**

```text
/do Research five competitors of my startup and prepare a comparison of their pricing and features.
```

A possible workflow is:

1. Interpret the requested outcome.
2. Decide which steps are necessary.
3. Search for relevant information.
4. Gather pricing and feature details.
5. Compare the results.
6. Produce a report.
7. Record the outcome, if task tracking is implemented.

Another example:

```text
/do Find recent emails from my clients, identify unanswered questions, and prepare draft replies.
```

This would require an email integration with suitable permissions.

**Safety distinction:** Preparing a draft is not the same as sending it. Butler should not silently perform consequential external actions unless the product's authorization model explicitly permits them.

### `/plan <schedule> | goal`

**Purpose:** Define a goal associated with a schedule or planning instruction.

**General syntax:**

```text
/plan <schedule> | <goal>
```

**Illustrative example:**

```text
/plan daily 09:00 [tz=Africa/Lagos] | Check my inbox and summarize important emails.
```

This describes a goal to be associated with a daily 9:00 a.m. schedule in the specified time zone.

The exact distinction between `/plan` and `/task` cannot be determined from the command syntax alone. For example, `/plan` might create a plan for a scheduled workflow, while `/task` might create an explicitly step-by-step job. Inspect the command handlers to confirm the actual behaviour.

### `/task <schedule> | step | step`

**Purpose:** Define a scheduled task with explicit steps.

**General syntax:**

```text
/task <schedule> | <step 1> | <step 2> | ...
```

**Example:**

```text
/task daily 09:00 [tz=Africa/Lagos] | Search for new AI engineering news | Summarize the three most important developments | Send me the summary
```

This describes three steps:

1. Search for new AI engineering news.
2. Summarize the three most important developments.
3. Send the summary.

Each step depends on the relevant tools and execution logic being implemented. If sending email or messages is involved, the required account connection and authorization must be in place.

### Schedule syntax

The command reference lists these schedule forms:

#### Run now

```text
now
```

This represents immediate execution. Which command accepts it must be determined by the implementation.

#### Run after another task

```text
after <id>[,<id>..]
```

Examples:

```text
after 12
```

```text
after 12,15
```

This indicates that a task depends on one or more other task IDs. The expected behaviour is that the dependent task waits until its prerequisites satisfy the scheduler's dependency rules.

A robust scheduler should define what happens when a prerequisite fails, is cancelled, or never completes.

#### Run daily

```text
daily 09:00 [tz=Area/City]
```

Example:

```text
daily 09:00 [tz=Africa/Lagos]
```

This represents a daily run at 9:00 a.m. in the specified time zone.

Time-zone handling matters because the same clock time corresponds to different moments in different locations. The scheduler should consistently handle daylight-saving changes where relevant.

#### Run weekly

```text
weekly mon 09:00
```

This represents a weekly run on Monday at 9:00 a.m. Confirm how the implementation handles time zones for this form.

#### Run at a fixed interval

```text
every 30m
```

This represents a repeated interval of 30 minutes.

The accepted interval units and minimum or maximum intervals depend on the scheduler.

#### Run once at a specified date and time

```text
once 2026-10-20T09:00
```

This represents a one-time run at the specified local or configured time. Because the timestamp contains no time-zone offset, the implementation must define which time zone applies.

### `/tasks`

**Purpose:** List tasks known to Butler.

```text
/tasks
```

A task listing might include:

- Task ID.
- Goal or description.
- Current status.
- Schedule.
- Dependencies.
- Creation time.
- Last execution time.
- Error or result summary.

Illustrative output:

```text
ID: 12
Goal: Research AI engineering news
Status: Completed

ID: 13
Goal: Prepare a report
Status: Waiting for task 12

ID: 14
Goal: Daily inbox summary
Status: Scheduled
```

The actual statuses depend on Butler's task model.

### `/pause`

**Purpose:** Pause work.

```text
/pause
```

The command appears to be global because it does not show a task ID. However, the exact scope must be confirmed in the implementation.

Possible behaviours include pausing all scheduled work, pausing the current task, or preventing future task runs. These are not equivalent.

### `/resume`

**Purpose:** Resume paused work.

```text
/resume
```

A robust implementation should resume existing tasks without unintentionally creating duplicate scheduled jobs.

### `/cancel`

**Purpose:** Cancel work.

```text
/cancel
```

The syntax does not show a task ID, so the target of cancellation is ambiguous without inspecting the handler.

The implementation should clearly distinguish between:

- Pausing a task temporarily.
- Cancelling a specific task.
- Cancelling all scheduled tasks.
- Stopping a task that is already running.

Cancellation should also define what happens to dependent tasks and whether an in-progress external operation can actually be stopped.

---

## 5. Graph: Dependencies and Impact Analysis

The Graph commands appear to expose Butler's graph-based representation of tasks, services, and relationships.

A graph represents entities as **nodes** and relationships as **edges**.

Possible nodes include tasks, services, resources, files, or decisions. Edges might represent that a task depends on another task, a workflow uses a service, or a service is required by a particular operation.

For example:

```text
Research competitors
        |
        v
Compare pricing
        |
        v
Prepare report
```

This structure helps Butler reason about relationships rather than treating every task as isolated.

### `/blocked`

**Purpose:** Show blocked tasks.

```text
/blocked
```

A task may be blocked because a prerequisite has not completed or a required service is unavailable.

Illustrative output:

```text
Task: Prepare market report
Blocked by: Competitor research
Reason: Required research is incomplete
```

This is useful for understanding why work cannot proceed.

### `/impact <service>`

**Purpose:** Inspect the tasks or components affected by a service.

**Example:**

```text
/impact paypal
```

If several workflows depend on PayPal, Butler might identify which of them could be affected by a PayPal outage.

An illustrative dependency graph could look like:

```text
PayPal
  ├── Check payment status
  ├── Create payment request
  └── Execute supported transfer
```

The usefulness of this command depends on whether Butler records accurate relationships between services and tasks. It should not claim that a service affects a workflow unless that dependency is represented or otherwise known.

### `/why task <id>`

**Purpose:** Explain the status or dependency chain of a task.

**Example:**

```text
/why task 15
```

An illustrative explanation might be:

```text
Task 15 is blocked because:
1. It depends on task 12.
2. Task 12 is still running.
3. Task 12 requires the search service.
```

This is useful for debugging and operational transparency.

### `/history <id>`

**Purpose:** Show history associated with a task or graph entity.

**Example:**

```text
/history 15
```

Depending on the implementation, the history may contain:

- Task creation.
- Execution start and completion.
- Step transitions.
- Tool invocations.
- Retry attempts.
- Errors.
- Final outcome.

This can help diagnose why a workflow failed or took longer than expected.

### Disabling the graph with `BUTLER_GRAPH=off`

The setting:

```text
BUTLER_GRAPH=off
```

appears to be an environment variable that disables graph functionality.

It may be configured through a deployment environment, environment file, or process configuration, depending on how Butler loads settings.

Potential reasons to disable the graph include:

- Testing graph functionality independently.
- Comparing behaviour with and without graph context.
- Diagnosing graph-related errors.
- Measuring whether graph relationships improve task planning.

Disabling the graph does **not** necessarily disable every form of memory or persistence. Conversation history, relational database records, and other memory systems may be independent.

The application should define what happens to graph-dependent commands when the graph is disabled—for example, whether they return a clear message or use a documented fallback.

---

## 6. Memory: Persistent Information and Preferences

The Memory commands manage information Butler can retain between interactions.

For example, an assistant may store a user's preferred programming language or a project's technical stack so that the user does not need to repeat it in every conversation.

### `/remember key: value [| scope]`

**Purpose:** Store a memory under a key.

**Syntax:**

```text
/remember <key>: <value> [| <scope>]
```

**Example:**

```text
/remember language: Python
```

This stores the value `Python` under the key `language`, assuming the command uses the expected key-value semantics.

Another example:

```text
/remember project: Building a Telegram AI assistant
```

The optional scope may determine where the memory applies, but the accepted scopes must be confirmed from the implementation.

Illustrative examples:

```text
/remember language: Python | global
```

```text
/remember database: PostgreSQL | project:yuciferpay
```

These scope names are examples only. Use the scope syntax that Butler actually supports.

**Why structured memory is useful:**

- Information can be retrieved by key.
- Preferences can be reused across tasks.
- Project-specific facts can be separated from general preferences.
- The assistant can avoid repeatedly asking for the same context.

### `/forget key`

**Purpose:** Remove a memory associated with a key.

**Example:**

```text
/forget language
```

The exact deletion behaviour depends on the implementation. It should define whether this removes one record, all records sharing the key, or only a record in the current scope.

Deleting a memory entry is also different from deleting the original conversation, file, or other source from which the information was obtained.

### `/prefs`

**Purpose:** Display stored preferences.

```text
/prefs
```

A response might show preferences such as response style, preferred language, or default time zone, depending on the fields supported by Butler.

This is useful for checking which preferences the assistant may apply to future interactions.

### `/why key`

**Purpose:** Explain a stored memory or preference.

**Example:**

```text
/why language
```

Depending on the implementation, Butler could show the value, source, timestamp, scope, or provenance of the memory.

For example:

```text
Key: language
Value: Python
Source: Explicit user instruction
```

Provenance information can make memory more transparent and easier to debug. The system should distinguish explicitly provided facts from inferred or uncertain information.

### `/trace`

**Purpose:** Display a trace of execution or operational events, depending on what Butler records.

An illustrative trace might look like:

```text
1. Received task
2. Retrieved relevant memory
3. Selected an AI model
4. Generated a plan
5. Called search tool
6. Produced final response
```

This can be useful for debugging the sequence of actions taken by an agent.

A trace is not necessarily the same as a model's private reasoning. A safe, useful trace should focus on operational events, tool calls, decisions, errors, and state transitions rather than exposing hidden chain-of-thought or sensitive credentials.

### `/contact`

**Purpose:** The exact behaviour is unclear from the command name alone.

It might display or manage contact information, but this should not be assumed without inspecting the command handler.

### `/contacted`

**Purpose:** The exact behaviour is also unclear from the command name alone.

It might retrieve or manage records of contacts who have already been contacted. For example, an outreach workflow could use such records to avoid sending duplicate messages.

The implementation should clearly distinguish contact details from contact history.

---

## 7. Payments: Payment Operations and Status

Payment commands can connect Butler to operations involving real money. They require more careful authorization, validation, and failure handling than ordinary text or research tasks.

### `/pay 20 USD name@example.com note`

**Purpose:** Initiate a payment-related operation using an amount, currency, recipient, and note.

**Syntax shown:**

```text
/pay <amount> <currency> <recipient> <note>
```

**Example:**

```text
/pay 20 USD name@example.com Invoice payment
```

The fields appear to mean:

| Field | Example | Meaning |
|---|---|---|
| Amount | `20` | Amount to pay |
| Currency | `USD` | Currency |
| Recipient | `name@example.com` | Recipient identifier |
| Note | `Invoice payment` | Payment description |

The actual recipient format and operation depend on the payment integration. An email address may identify a PayPal account in some workflows, but the specific API and supported transfer method determine what is possible.

**Does `/pay` definitely transfer money?**

Not necessarily. The command name suggests a payment action, but the implementation might initiate a transfer, create a payment request, or start another payment workflow. Inspect the handler and integration to confirm its actual effect.

### Recommended safeguards for payment operations

Before enabling real payments, consider these protections:

1. **Explicit confirmation:** Show the recipient, amount, and currency before committing the transaction.
2. **Recipient validation:** Validate the recipient against the selected payment method.
3. **Spending limits:** Enforce per-transaction and cumulative limits.
4. **Idempotency:** Prevent retries from accidentally creating duplicate transactions.
5. **Transaction records:** Store provider references and status updates.
6. **Authorization checks:** Ensure one user cannot spend from another user's account.
7. **Credential protection:** Keep API keys and tokens out of chat messages, model prompts, and logs.
8. **Auditability:** Record who requested an operation, what was authorized, and the final provider result.
9. **Timeout handling:** Treat an uncertain result as unknown until checked rather than immediately retrying the payment.

### `/status id`

**Purpose:** Check the status of a payment or payment-related operation.

**Syntax:**

```text
/status <id>
```

**Example:**

```text
/status PAY-123
```

Here, `PAY-123` is an illustrative transaction identifier.

Depending on the integration, a status response might show the amount, currency, transaction reference, and current status. Possible statuses include pending, processing, completed, failed, or cancelled, but the actual values depend on the provider.

### Why status checks matter

Suppose Butler submits a payment request and the API call times out. A timeout does not necessarily mean the provider failed to process the transaction.

The provider might have completed the payment even though Butler did not receive the response.

A safer workflow is:

1. Save a unique internal transaction record.
2. Submit the operation with an idempotency key if the provider supports it.
3. If the result is uncertain, check the existing transaction's status.
4. Retry only according to the provider's documented rules.
5. Update the internal record when the final outcome is known.

This reduces the risk of duplicate transfers.

---

## 8. How the Components Work Together

The command groups become more useful when they work together.

Consider the goal:

> Every weekday morning, research new developments in AI infrastructure, summarize the most important ones, and email me a report.

A possible workflow is:

```text
Scheduled Task
      |
      v
Search for AI Infrastructure News
      |
      v
Summarize Important Developments
      |
      v
Prepare Email
      |
      v
Send Email
      |
      v
Record Task Outcome
```

The components contribute as follows:

| Component | Responsibility |
|---|---|
| Models | Select an AI model for planning, research synthesis, or writing |
| Spend | Track usage and enforce the configured budget |
| Tools | Search the web and access the email service |
| Work | Schedule and execute the workflow |
| Graph | Represent dependencies between research, summarization, and delivery |
| Memory | Apply relevant preferences and stored context |
| Payments | Not needed for this example |

If the search service fails, the workflow may be unable to continue to summarization. If the budget is exhausted, Butler may need to stop further model requests. If email delivery fails, the task history should record the failure so it can be investigated or retried according to policy.

The key idea is that each component has a distinct responsibility, while the task runner coordinates them.

---

## 9. Suggested Testing Sequence

Test the system in stages, starting with configuration and progressing to tasks and sensitive operations.

### Stage 1: AI configuration

```text
/addkey openai
/models
/modelorder chat openai
```

Verify that:

- The provider key is stored securely.
- The configured models are listed correctly.
- The selected model is actually usable.
- Invalid model names produce clear errors.
- Provider failures are handled correctly.

### Stage 2: Usage and budget controls

```text
/usage
/budget tokens 100000
/budget cost 5
```

Verify that:

- Input and output usage are recorded.
- Cost estimates use the correct price units.
- The correct user or task receives the usage record.
- Budgets are enforced at the intended scope.
- Concurrent tasks cannot silently bypass the same budget.
- The reset period is clear.

Use safe test limits during development.

### Stage 3: External tools

```text
/connect search
/tools
/disconnect search
```

Verify that:

- Authentication succeeds or fails clearly.
- Connected tools are distinguished from merely implemented tools.
- Tool calls work as expected.
- Disconnecting a tool changes its availability.
- Errors from the external service are surfaced appropriately.

### Stage 4: One-off task execution

```text
/do Find three useful articles about backend system design and summarize them.
```

Verify that Butler can:

- Understand the goal.
- Create or select an appropriate plan.
- Choose a usable model.
- Invoke the required tools.
- Handle tool failures.
- Return a useful result.
- Record the task outcome.

### Stage 5: Scheduled tasks

Illustrative example:

```text
/task once 2026-10-20T09:00 | Search for backend engineering news | Summarize the results
```

Verify that:

- The schedule is parsed correctly.
- The time zone is well-defined.
- The job persists across application restarts.
- The job executes at the expected time.
- A task is not accidentally scheduled more than once.
- Failed jobs can be inspected and handled.

### Stage 6: Graph functionality

Use actual service names and task IDs from your environment:

```text
/blocked
/impact search
/why task 1
/history 1
```

Verify that:

- Task dependencies are represented accurately.
- Blocked tasks have understandable explanations.
- Impact analysis follows real dependencies.
- History matches actual execution events.
- Graph-dependent commands behave clearly when graph functionality is disabled.

### Stage 7: Memory

```text
/remember language: Python
/prefs
/why language
/forget language
```

Verify that:

- Memories persist as intended.
- Scope is respected.
- Provenance is recorded if supported.
- Deletion affects the intended record.
- Sensitive information is not stored unnecessarily.
- Memory retrieval does not expose one user's data to another.

### Stage 8: Payments

Start with a sandbox or test environment if the provider offers one.

Verify:

- Authorization and account isolation.
- Recipient and amount validation.
- Confirmation before committing sensitive operations.
- Idempotency and retry handling.
- Transaction record creation.
- Status checks after timeouts.
- Correct handling of pending, failed, and completed operations.
- Audit logs that do not contain credentials or other secrets.

Do not begin with a real-money transfer simply to check whether the command works.

---

## 10. Implementation and Security Considerations

The command list defines an interface, but each command needs correct underlying behaviour.

### Task reliability

For scheduled and multi-step work, consider:

- Persistent task storage.
- Clear task states, such as scheduled, running, completed, failed, paused, and cancelled.
- Dependency validation.
- Bounded retries and timeouts.
- Recovery after process restarts.
- Duplicate-job prevention.
- Clear handling of failed prerequisites.
- Useful execution history.

### Cost control

For AI usage and budgets, consider:

- Recording actual provider usage when available.
- Keeping model prices up to date.
- Defining the scope and reset period of each budget.
- Checking budgets before making requests.
- Accounting for concurrent tasks.
- Distinguishing estimated costs from provider billing.

### Security and permissions

For credentials and external integrations, consider:

- Secure credential storage.
- Least-privilege provider permissions.
- Per-user authorization checks.
- Clear separation between read-only and write operations.
- Protection against secrets leaking into logs or model prompts.
- Safe disconnection and credential-revocation procedures.

### Payments and other consequential actions

For actions that affect money or external accounts, consider:

- Explicit confirmation for sensitive operations.
- Recipient and amount validation.
- Idempotency keys.
- Transaction status reconciliation.
- Audit records.
- Rate limits and spending limits.
- Safe handling of ambiguous timeouts.

### Memory and privacy

For persistent memory, consider:

- Clear memory scope.
- Provenance and timestamps.
- User-level data isolation.
- A way to inspect stored information.
- Predictable deletion behaviour.
- Avoiding unnecessary storage of sensitive information.

### Graph functionality

For graph-based task analysis, consider:

- Accurate relationships between tasks and services.
- Validation of dependency cycles.
- Clear behaviour when prerequisites fail.
- Graceful operation when graph functionality is disabled.
- Tests comparing behaviour with the graph enabled and disabled.

---

## Final Summary

Butler's command system covers seven main areas:

1. **Models:** Configure AI providers, model preferences, and pricing.
2. **Spend:** Track usage and enforce token or cost budgets.
3. **Tools:** Connect external services such as PayPal, email, and search.
4. **Work:** Plan, schedule, execute, pause, resume, and inspect tasks.
5. **Graph:** Investigate blocked tasks, dependencies, impact, and execution history.
6. **Memory:** Store, inspect, explain, and remove persistent information.
7. **Payments:** Initiate supported payment operations and check their status.

The most important distinction is that **a command's presence does not prove that its intended behaviour is fully implemented**. Each command needs a handler, validation, permissions, persistence where required, error handling, and tests.

For Butler, three priorities deserve particular attention:

- **Reliability:** Tasks should survive restarts, recover from failures, and avoid duplicate execution.
- **Cost control:** Usage and spending should be measured accurately, with budgets enforced before further requests.
- **Security:** Credentials must be protected, users must only access their own resources, and sensitive operations must have appropriate authorization.

When these foundations are solid, the command system can support a more capable, dependable, and auditable AI assistant.
