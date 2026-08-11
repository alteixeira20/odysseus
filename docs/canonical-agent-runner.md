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

Odysseus currently has two typed event families that can produce terminal run state before SSE serialization:

```text
AgentEvent(run_state) -----------+
                                 |
RuntimeEvent v2(run_state) ------+--> DurableRunLifecycle
                                 |          |
                                 |          +--> durable terminal state
                                 v
                         existing SSE encoders
                                 |
                                 v
                         frontend wire unchanged
```

`DurableRunLifecycle` installs task-local observers only while advancing the execution backend, captures either typed terminal family, journals the exact resulting wire event, and only then applies the durable terminal transition. The observer contexts are reset before transport/UI code receives each chunk.

SSE JSON terminal parsing is retained only for genuinely legacy/literal wire producers that bypass both typed event encoders. It is no longer canonical terminal truth for modern Agent paths.

## Completed migration boundaries

1. **Canonical execution entry point** — `AgentRunRequest -> AgentRunner` is the public execution path; Runtime V3 and the legacy loop are no longer invoked by `src.agent.api` directly.
2. **Server/HTTP authority preparation** — `chat_routes.py` sends intent, workspace grants, host token and prepared turn lease through `AgentAuthorityRequest`; Runtime V2 execution-context construction is owned by `AgentRunner`.
3. **Typed durable lifecycle** — `AgentRunner` owns `DurableRunLifecycle`; both AgentEvent and Runtime V2 terminal events are typed-first and the old Runtime V3 orchestrator is only a compatibility façade.

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
