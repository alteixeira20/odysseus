# Canonical AgentRunner migration

`agentic-lab` now has one public Agent execution control plane:

```text
application / legacy caller
          |
          v
     src.agent.api
          |
          v
      AgentRunner
          |
          +-- authority preparation (Runtime V2 contract)
          +-- durable lifecycle (Runtime V3 ledger service)
          +-- backend execution
          |
          v
AgentLoopCompatibilityBackend
          |
          v
_legacy_stream_agent_kernel
```

`src.agent_loop.stream_agent_loop` remains only as a frozen compatibility façade. It re-enters `src.agent.api.stream_agent_loop`; it no longer owns orchestration. `AgentLoopCompatibilityBackend` deliberately reaches the internal `_legacy_stream_agent_kernel` directly so the canonical runner cannot recurse through the public façade.

## Ownership rules

- `src.agent.api` is the stable application boundary for typed execution and authority preparation.
- `AgentRunner` owns request preparation, execution authority, one `DurableRunLifecycle` instance per run, and backend invocation order.
- Runtime V3 is a durability/ledger service. `runtime_v3.orchestrator.stream_with_durable_runtime()` remains only as a compatibility façade.
- HTTP routes provide `AgentAuthorityRequest`; they do not construct Runtime V2 budgets, catalogue revisions, capabilities, roots, or execution-context grants directly.
- `PreparedAuthority.event_payload()` owns the transport-neutral projection of prepared authority used by the current SSE UI.
- `AgentLoopCompatibilityBackend` is the only typed-to-legacy keyword projection point and calls only the internal kernel.
- `src.agent_loop.stream_agent_loop` preserves the historical signature but delegates through the stable API/runner.
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

1. **Canonical execution entry point** — `AgentRunRequest -> AgentRunner` is the public typed execution path.
2. **Server/HTTP authority preparation** — `chat_routes.py` sends intent, workspace grants, host token and prepared turn lease through `AgentAuthorityRequest`; Runtime V2 execution-context construction is owned by `AgentRunner`.
3. **Typed durable lifecycle** — `AgentRunner` owns `DurableRunLifecycle`; both AgentEvent and Runtime V2 terminal events are typed-first and the old Runtime V3 orchestrator is only a compatibility façade.
4. **Public legacy-loop inversion** — the historical `src.agent_loop.stream_agent_loop` signature now delegates into the canonical API/runner; the characterized implementation is explicitly internal as `_legacy_stream_agent_kernel`.

## Remaining internal kernel debt

The internal kernel still composes the already-extracted context, provider, supervision and tool-execution components. That is now an implementation debt inside one control plane rather than a competing public runtime.

The next migration should be driven by behavioral evidence rather than moving the ~1000+ line kernel wholesale. The high-value remaining targets are:

1. typed working/run state instead of many orchestration locals;
2. provider-round composition owned by a typed kernel service;
3. supervisor/verification decisions expressed as typed outcomes end-to-end;
4. tool-call/effect execution flowing exclusively through canonical `ToolDefinition` metadata;
5. eventual removal of `AgentLoopCompatibilityBackend` once the internal kernel no longer depends on legacy globals.

Separate deferred work remains intentionally outside this migration: stream-event batching/performance should be driven by measured SQLite/latency data, and the one-run host-token issuance endpoint is a distinct authority-store API rather than Agent-run preparation.

The historical function signature stays frozen while compatibility is needed. It is now an adapter surface, not an orchestrator.
