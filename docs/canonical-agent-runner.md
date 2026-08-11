# Canonical AgentRunner migration

`agentic-lab` is converging on one public Agent control plane:

```text
AgentRunRequest
      |
      v
  AgentRunner
      |
      +-- authority preparation (Runtime V2 contract)
      +-- durable lifecycle (Runtime V3 ledger)
      +-- backend execution
```

The current backend is `AgentLoopCompatibilityBackend`. It is a temporary strangler seam around `src.agent_loop.stream_agent_loop`, not a second public runtime.

## Ownership rules

- `src.agent.api` owns only API compatibility and typed request construction.
- `AgentRunner` owns request preparation, execution authority, durable lifecycle, and backend invocation order.
- `AgentLoopCompatibilityBackend` is the only typed-to-legacy keyword projection point.
- Runtime callers use `src.agent.api`; they do not call `src.agent_loop.stream_agent_loop` directly.
- An already server-prepared `AgentExecutionContext` is preserved by identity and never prepared a second time.
- A compatibility/direct request without an execution context is prepared by `AgentRunner` before durable execution begins.

## Remaining migration seam

`src.agent_loop.stream_agent_loop` still contains the compatibility orchestration body. Subsequent slices should move phase ownership inward from this seam rather than wrapping another runtime around it:

1. server/HTTP authority preparation through an `AgentRunner.prepare` request contract;
2. typed context/working-state preparation;
3. provider round orchestration;
4. supervisor decisions and verification;
5. typed tool-call/effect execution through the canonical registry;
6. typed terminal events so the durable lifecycle no longer needs to infer semantics from SSE text.

The legacy function signature stays frozen until callers and characterization tests no longer require it. It is an adapter surface, not the target architecture.
