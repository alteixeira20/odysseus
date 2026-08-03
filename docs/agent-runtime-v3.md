# Agent Runtime V3 foundation

Runtime V3 introduces a durable execution boundary without deleting the legacy
compatibility facade in one unsafe migration.

## Invariants

1. A run has one durable identity and an explicit terminal reason.
2. Every emitted runtime event receives a monotonic sequence in SQLite.
3. An effect moves through `started` to exactly one of `committed`, `failed`, or
   `unknown`. Process interruption converts unresolved effects to `unknown`.
4. Unknown effects are never automatically retried.
5. Idempotency keys are scoped to a run and cannot be reused with different
   arguments.
6. Context projection never truncates system instructions or the latest user
   request. Native tool call/result groups are indivisible.
7. MCP subprocesses receive a minimal environment, not the Odysseus process
   environment. MCP calls have a deadline and a total result-size budget.
8. Model prose and ordinary code fences never become an implicit document or
   external effect.

## Storage

The default ledger is `data/agent-runtime-v3.sqlite3`. Set
`ODYSSEUS_RUNTIME_V3_DB` to override it. SQLite uses WAL and `synchronous=FULL`.
The database is additive and may be deleted only when no run history or effect
reconciliation is required.

## Recovery

Startup classifies runs owned by a prior process as `interrupted`, marks every
started effect `unknown`, and retains sequenced events for inspection. Automatic
resume is deliberately not enabled until every tool has an explicit effect and
idempotency contract.

## Configuration

- `ODYSSEUS_AGENT_MAX_ROUNDS`
- `ODYSSEUS_AGENT_MAX_TOOL_CALLS`
- `ODYSSEUS_AGENT_MAX_PROVIDER_CALLS`
- `ODYSSEUS_AGENT_MAX_RUN_SECONDS`
- `ODYSSEUS_AGENT_RUN_IDLE_SECONDS`
- `ODYSSEUS_AGENT_MAX_EVENT_BYTES`
- `ODYSSEUS_AGENT_MAX_REPLAY_EVENTS`
- `ODYSSEUS_MCP_CALL_TIMEOUT_SECONDS`
- `ODYSSEUS_MCP_MAX_OUTPUT_BYTES`
