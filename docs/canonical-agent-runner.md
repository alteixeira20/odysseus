# Canonical AgentRunner migration

`agentic-lab` is converging on one public Agent control plane:

```text
AgentRunRequest / AgentAuthorityRequest
              |
              v
          AgentRunner
              |
              +-- authority preparation (Runtime V2 contract)
              +-- durable lifecycle (Runtime V3 ledger)
              +-- backend execution
```

The current execution backend is `AgentLoopCompatibilityBackend`. It is a temporary strangler seam around `src.agent_loop.stream_agent_loop`, not a second public runtime.

## Ownership rules

- `src.agent.api` is the stable application boundary for typed execution and authority preparation.
- `AgentRunner` owns request preparation, execution authority, durable lifecycle, and backend invocation order.
- HTTP routes provide `AgentAuthorityRequest`; they do not construct Runtime V2 budgets, catalogue revisions, capabilities, roots, or authority grants directly.
- `PreparedAuthority.event_payload()` owns the transport-neutral projection of prepared authority used by the current SSE UI.
- `AgentLoopCompatibilityBackend` is the only typed-to-legacy keyword projection point.
- Runtime callers use `src.agent.api`; they do not call `src.agent_loop.stream_agent_loop` directly.
- An already prepared `AgentExecutionContext` is preserved by identity and never prepared a second time.
- A compatibility/direct request without an execution context is prepared by `AgentRunner` before durable execution begins.

## Completed migration boundaries

1. **Canonical execution entry point** — `AgentRunRequest -> AgentRunner` is the public execution path; Runtime V3 and the legacy loop are no longer invoked by `src.agent.api` directly.
2. **Server/HTTP authority preparation** — `chat_routes.py` sends intent, workspace grants, host token and prepared turn lease through `AgentAuthorityRequest`; Runtime V2 authority construction is owned by `AgentRunner`.

## Remaining migration seam

`src.agent_loop.stream_agent_loop` still contains the compatibility orchestration body. Subsequent slices should move phase ownership inward from this seam rather than wrapping another runtime around it:

1. typed durable lifecycle/terminal events so ledger semantics no longer depend on parsing SSE text;
2. typed context and working-state preparation;
3. provider round orchestration;
4. supervisor decisions and verification;
5. typed tool-call/effect execution through the canonical registry;
6. replace `AgentLoopCompatibilityBackend` with the canonical execution kernel;
7. invert `src.agent_loop.stream_agent_loop` into a compatibility facade over `AgentRunner`, then retire it when external compatibility no longer requires the signature.

The legacy function signature stays frozen during the migration. It is an adapter surface, not the target architecture.
