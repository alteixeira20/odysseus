# Canonical AgentRunner migration

`agentic-lab` is converging on one public Agent control plane:

```text
AgentRunRequest / AgentAuthorityRequest
              |
              v
          AgentRunner
              |
              +-- authority preparation (Runtime V2 contract)
              +-- durable lifecycle (Runtime V3 ledger service)
              +-- backend execution
```

The current execution backend is `AgentLoopCompatibilityBackend`. It is a temporary strangler seam around `src.agent_loop.stream_agent_loop`, not a second public runtime.

## Ownership rules

- `src.agent.api` is the stable application boundary for typed execution and authority preparation.
- `AgentRunner` owns request preparation, execution authority, one `DurableRunLifecycle` instance per run, and backend invocation order.
- Runtime V3 is a durability/ledger service. `runtime_v3.orchestrator.stream_with_durable_runtime()` remains only as a compatibility façade.
- HTTP routes provide `AgentAuthorityRequest`; they do not construct Runtime V2 budgets, catalogue revisions, capabilities, roots, or execution-context grants directly.
- `PreparedAuthority.event_payload()` owns the transport-neutral projection of prepared authority used by the current SSE UI.
- `AgentLoopCompatibilityBackend` is the only typed-to-legacy keyword projection point.
- Runtime callers use `src.agent.api`; they do not call `src.agent_loop.stream_agent_loop` directly.
- An already prepared `AgentExecutionContext` is preserved by identity and never prepared a second time.
- A compatibility/direct request without an execution context is prepared by `AgentRunner` before durable execution begins.

## Typed lifecycle boundary

`run_state_event()` already creates an immutable `AgentEvent` before projecting it to the legacy SSE wire. The canonical lifecycle observes that typed event in the current task context and uses it as terminal truth.

```text
AgentEvent(run_state)
        |
        +----> DurableRunLifecycle terminal semantics
        |
        v
encode_legacy_sse()
        |
        v
current frontend wire (unchanged)
```

The durable replay ledger still stores the current wire event for compatibility. For terminal semantics, SSE JSON parsing is consulted only when a genuinely legacy producer bypasses the typed `AgentEvent` boundary. The typed terminal is applied after its exact wire event is journaled, preserving the existing replay-before-terminal ordering.

## Completed migration boundaries

1. **Canonical execution entry point** — `AgentRunRequest -> AgentRunner` is the public execution path; Runtime V3 and the legacy loop are no longer invoked by `src.agent.api` directly.
2. **Server/HTTP authority preparation** — `chat_routes.py` sends intent, workspace grants, host token and prepared turn lease through `AgentAuthorityRequest`; Runtime V2 execution-context construction is owned by `AgentRunner`.
3. **Typed durable lifecycle** — `AgentRunner` owns `DurableRunLifecycle`; terminal state is typed-first and the old Runtime V3 orchestrator is only a compatibility façade.

## Remaining migration seam

`src.agent_loop.stream_agent_loop` still contains the compatibility orchestration body. Subsequent slices should move phase ownership inward from this seam rather than wrapping another runtime around it:

1. typed context and working-state preparation;
2. provider round orchestration;
3. supervisor decisions and verification;
4. typed tool-call/effect execution through the canonical registry;
5. replace `AgentLoopCompatibilityBackend` with the canonical execution kernel;
6. invert `src.agent_loop.stream_agent_loop` into a compatibility facade over `AgentRunner`, then retire it when external compatibility no longer requires the signature.

Separate deferred work remains intentionally outside this migration: stream-event batching/performance should be driven by measured SQLite/latency data, and the one-run host-token issuance endpoint is a distinct authority-store API rather than Agent-run preparation.

The legacy function signature stays frozen during the migration. It is an adapter surface, not the target architecture.
