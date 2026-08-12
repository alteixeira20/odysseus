# Agent Lab testing and upstream extraction guide

This document is the validation contract for `agentic-lab` before agentic work is split into upstream pull requests.

The branch is a laboratory integration branch, not an upstream PR as a whole. A green deterministic suite is necessary but not sufficient: real-model testing must also demonstrate that the runtime completes the requested work without false success, repeated effects, authority drift, workspace leakage, or unreaped processes.

## 1. Local readiness commands

From the repository root:

```bash
python scripts/agent_lab_readiness.py
```

This runs the high-signal quick contract: compile checks, durable effect/replay tests, request-identity/privacy tests, completion/truncation/unknown-tool regressions, verifier truthfulness, approval dependability, process identity, cancellation, and the JavaScript runtime contracts.

Before a release candidate or upstream extraction session, run the full contract:

```bash
python scripts/agent_lab_readiness.py --full
```

`--full` expands the same Python test categories used by `.github/workflows/agent-runtime-gate.yml`, plus the same JavaScript syntax and contract tests.

Useful diagnostic modes:

```bash
python scripts/agent_lab_readiness.py --dry-run
python scripts/agent_lab_readiness.py --full --no-js
```

Do not use `--skip-sandbox-probe` as evidence that the runtime is ready. It exists only for diagnosing non-Linux or restricted environments.

### Linux prerequisites

The process-integrity contract expects `bubblewrap` and `tmux`. On Ubuntu 24.04, AppArmor may restrict unprivileged user namespaces even when Bubblewrap is installed. The harness probes this boundary before the test suite and fails with an actionable diagnostic rather than silently testing a weaker environment.

The GitHub Agent Runtime Gate changes namespace sysctls only inside its ephemeral hosted runner. Do not copy those changes blindly to a persistent workstation; inspect the local security policy first.

The broad repository CI also provisions and probes the same Linux sandbox prerequisites. Its Python suite is deterministically partitioned across 16 exhaustive module shards; every discovered test module is assigned exactly once and the shards are blocking.

## 2. Hard blocker criteria

Stop testing and treat the branch as not ready if any scenario produces one of these outcomes:

- a run reports `completed` when the requested effect or artifact was not actually produced;
- an external or workspace mutation is executed twice after retry, replay, approval, verifier repair, reconnect, or provider interruption;
- a one-use exact approval can authorize a second execution;
- a started or ambiguous effect is retried without reconciliation;
- a cancelled/superseded process remains alive after the run reaches its terminal state;
- one concurrent run observes or mutates another run's workspace/authority/tool scope;
- a sandboxed process reads secret material or writes outside the authorized root;
- endpoint credentials, authorization header values, prompts, uploaded contents, active document/email contents, plan text, or raw workspace paths appear in the Runtime V3 durable request record;
- a verifier timeout/malformed response/failure is treated as successful completion when verification is enabled;
- a provider truncation or interrupted native tool call is silently accepted as a complete answer;
- a cross-provider fallback occurs outside the configured trust policy.

These are correctness failures, not UX defects.

## 3. Real-model behavioral matrix

Run the matrix with at least one strong remote model and one local OpenAI-compatible model. If practical, add a smaller local model because schema discipline, tool repair, and continuation behavior often fail there before they fail on frontier models.

For each case record: model/provider, final disposition, tool calls, approvals, rounds, elapsed time, repeated effects, verifier result (if enabled), and a one-line pass/fail note.

| Case | Prompt/task shape | Required outcome |
| --- | --- | --- |
| Plain answer | Ask a factual/explanatory question requiring no tools. | No tool call; one terminal completion; no intent nudge loop. |
| Workspace read | Ask for a concrete fact from a selected workspace. | Search/read only; correct root; no mutation; concise evidence-backed answer. |
| Workspace patch | Request one small file edit. | Exactly one committed patch; result reflects actual file state; no duplicate replay. |
| Multi-file patch | Request related edits in two files. | All-new state on success; crash/recovery simulation must never leave an accepted half-commit. |
| Sandbox command | Request a harmless command/test in sandbox mode. | Runs in authorized root; cannot read common secrets or write outside root. |
| Host approval | Request a host command that requires exact approval. | Approval request binds exact command/context; one execution after approval; reuse denied. |
| Cancel process | Start a long command and cancel it during spawn/execution. | Typed cancellation; process/group reaped before terminal run state. |
| Concurrent workspaces | Start independent operations in two workspaces. | No authority, path, tool, result, or lifecycle leakage between runs. |
| Unknown tool recovery | Exercise a stale/misspelled native tool call with a fixture or model that produces one. | Structured model-visible error and retry opportunity; not tool-free false completion. |
| Output truncation | Use a provider/fixture that terminates for output length. | Bounded continuation; no duplicate tool execution; clean stop after complete response. |
| Interrupted tool call | Interrupt a partial native/fenced call. | Incomplete call is never dispatched; continuation/recovery is bounded. |
| Fallback | Configure two candidates inside one allowed trust boundary. | Candidate-specific context budget; fallback obeys policy; no unauthorized cross-provider hop. |
| Verifier PASS | Enable verifier after a successful effectful task. | Explicit PASS permits completion. |
| Verifier FAIL | Make evidence intentionally insufficient. | Fresh repair required; prose-only completion cannot clear failure; no repeated committed effect. |
| Verifier UNKNOWN | Simulate verifier timeout or malformed protocol. | Run becomes `INCOMPLETE`; never false success. |
| Durable run collision | Reuse a run id with changed semantic request in a fixture/integration test. | Collision rejected before semantic state can be confused. |
| Recovery/reconciliation | Interrupt after an effect may have started. | Durable effect becomes `unknown`; automatic retry blocked; manual reconciliation required. |

## 4. What to measure

A useful agent benchmark should eventually automate these metrics. During manual testing, record enough data to establish a baseline:

- task completion rate;
- false-success rate (target: zero);
- correct-tool recall and wrong-tool rate;
- malformed/unknown tool-call repair rate;
- duplicate/repeated effect count (target: zero);
- unknown-effect count and reconciliation outcome;
- rounds and provider calls per task;
- wall-clock completion time and time to first visible output;
- input/output tokens when the provider reports them;
- approvals per task;
- verifier PASS/FAIL/UNKNOWN distribution;
- fallback count and candidate used;
- context projection failures or dropped non-critical context.

Do not optimize token count or latency at the expense of correctness. In particular, measure local-model tool quality separately from strong remote models.

## 5. Integrated Agent Lab architecture

The public execution path has crossed the canonical-runner boundary:

```text
application / legacy caller
          |
          v
     src.agent.api
          |
          v
      AgentRunner
          |
          +-- Runtime V2 authority preparation
          +-- Runtime V3 durable lifecycle
          +-- backend execution
          |
          v
AgentLoopCompatibilityBackend
          |
          v
_legacy_stream_agent_kernel
```

`AgentRunner` owns request preparation, authority preparation, one durable run lifecycle, backend invocation, and delegated-stream cleanup. The legacy kernel consumes a prepared `AgentExecutionContext` and fails closed if that authority context is absent. The public façade, stable API, runner, durable lifecycle, and compatibility backend propagate async-generator close/cancellation so client disconnects synchronously reach in-flight tool cleanup.

The remaining size of `_legacy_stream_agent_kernel` is internal kernel debt under a single authoritative runner, not evidence that another top-level runtime or `AgentRunner` should be introduced. Further large decomposition should wait for behavioral evidence from real models.

## 6. Stabilization slices already integrated

The current lab stabilization work was intentionally merged into the laboratory branch as reviewable internal slices rather than one branch-wide rewrite. It includes:

- durable execution and unknown-tool recovery, including fail-high effect classification, exact approval/replay coherence, cancellation ownership, direct-call authority isolation, and rollback regressions;
- typed verifier `PASS | FAIL | UNKNOWN` semantics with fail-closed completion behavior;
- semantic durable request identity with secret-minimal diagnostics, workspace fingerprinting, endpoint-origin-only persistence, and path fingerprinting;
- canonical `src.agent.api` / `AgentRunner` control-plane inversion while preserving the legacy kernel as a compatibility backend;
- deterministic local Browser MCP deployment expectations and background process lifecycle hardening;
- the Agent Runtime Gate plus deterministic broad repository CI with real Linux sandbox prerequisites.

## 7. Upstream extraction strategy

Do not upstream `agentic-lab` wholesale. Start **every section branch from the then-current official `odysseus-dev/odysseus:dev`**, reconcile current upstream work first, and copy only the behavior/capability that is still missing or materially stronger.

Use one umbrella tracking issue for the complete Agent runtime reliability effort. Prefer approximately six coherent section PRs rather than micro-PRs. The first section can be ready for review; later sections should initially be drafts. Use child/stacked PRs only when a section has a real dependency boundary or is too large to review safely as one unit.

Recommended section order:

1. **Provider protocol and completion correctness.** Finish/termination normalization, bounded truncation continuation, interrupted/incomplete native tool-call handling, and candidate-specific context budgeting. Reconcile fallback behavior with whatever routing/fallback work is current upstream; do not duplicate an active fallback implementation.
2. **Canonical Agent execution boundary and lifecycle ownership.** Stable `src.agent.api`, typed `AgentRunner`, prepared execution context, compatibility inversion, one durable lifecycle per run, and deterministic stream-close/cancellation ownership.
3. **Durable execution integrity and authority reconciliation.** Runtime V3 run/effect semantics, fail-high unmodelled effects, unknown-effect reconciliation, idempotency, exact approval/replay coherence, process cleanup, and workspace transaction correctness. Reconcile with upstream sandbox/approval/security work instead of introducing a competing authority stack.
4. **Completion truthfulness plus durable identity/privacy.** Verifier `PASS | FAIL | UNKNOWN`, fail-closed finalization/repair semantics, semantic request collision identity, and secret-minimal durable metadata/fingerprints.
5. **MCP and background-runtime reliability.** Deterministic lockfile-installed Browser MCP binary resolution, no runtime NPX install path, deployment guards, background task ownership, and process cleanup.
6. **Qualification and CI contract.** Reproducible readiness runner, real Bubblewrap namespace probe, deterministic exhaustive broad CI, cancellation regressions, and behavioral-evaluation guidance.

The section boundary is the review unit. A section may contain several ordinary commits organized by behavior, tests, and integration, but upstream should not receive dozens of tiny PRs solely to reproduce the fork's development history.

## 8. Known deferred work

The branch is ready for behavioral testing when the deterministic gates are green, but these architectural debts remain and should not be hidden by a successful test session:

- Runtime V3 still journals raw wire events and can perform synchronous SQLite durability work at streaming frequency. Measure latency, DB growth, and retained content before changing batching/crash semantics.
- Operations code still has storage-coupling that should move behind a repository API before upstreaming durable operations/inspection.
- Context management is still primarily transcript projection rather than typed working state/evidence references.
- Routing/domain/prompt/tool metadata is duplicated across several registries and regex maps.
- The semantic verifier is LLM-based and opt-in; deterministic postconditions should eventually be the primary verifier for effects/artifacts.
- Automatic resume remains deliberately disabled for tools without a complete effect/idempotency contract.

The immediate testing objective is therefore precise: prove the current integrated runtime is safe, truthful, stable, and effective across representative models before further architectural consolidation or performance optimization.
