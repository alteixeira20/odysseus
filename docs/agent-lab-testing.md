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

## 5. Stabilization slices already integrated

The current lab stabilization work was intentionally merged as reviewable slices rather than one branch-wide refactor:

- **PR #17 — durable execution/unknown-tool baseline:** fail-high effect classification, exact approval/replay coherence, cancellation ownership, unknown-tool recovery, direct one-shot authority isolation, and regression hardening.
- **PR #18 — completion truthfulness:** typed `PASS | FAIL | UNKNOWN` verifier semantics and fail-closed completion behavior.
- **PR #19 — request identity/privacy:** semantic request collision identity with bounded durable diagnostics, workspace fingerprinting, endpoint-origin-only persistence, and endpoint path fingerprinting.
- **Agent Runtime Gate:** blocking Linux sandbox-aware focused correctness suite plus JavaScript runtime contracts.

## 6. Upstream extraction strategy

Do not upstream `agentic-lab` wholesale. Start each upstream contribution from the current official `dev` and keep it independently reviewable.

Recommended extraction order:

1. Provider finish/termination and bounded truncation continuation, if not already present upstream.
2. Behavior-preserving orchestration/component extraction in ordinary source commits. Do **not** use PR #8's compressed hidden-patch workflow as an upstream review shape.
3. Trust-bound fallback and candidate-specific context budgeting.
4. Reconcile tool/effect metadata with the official authority/sandbox/approval stack; do not introduce a competing security registry.
5. Durable run/effect semantics: fail-high unmodelled effects, unknown reconciliation, idempotency, exact approval/replay coherence, cancellation ownership.
6. Completion verifier truthfulness as a separate, small PR.
7. Semantic request identity and durable metadata privacy as a separate, small PR.
8. Durable run operations/inspector after storage internals are encapsulated.
9. Crash-recoverable workspace transaction journal as a separate filesystem-correctness PR.
10. Planning/evidence state and an automated behavioral evaluation harness after the canonical runtime shape is settled.

## 7. Known deferred work

The branch is ready for behavioral testing when the deterministic gate is green, but these architectural debts remain and should not be hidden by a successful test session:

- Runtime V3 still wraps/interprets legacy SSE rather than owning one typed canonical `AgentRunner` control plane.
- Runtime V3 currently journals raw wire events and can perform synchronous SQLite durability work at streaming frequency. Measure latency, DB growth, and retained content during testing before changing batching/crash semantics.
- Operations code still has storage-coupling that should move behind a repository API before upstreaming.
- Context management is still primarily transcript projection rather than typed working state/evidence references.
- Routing/domain/prompt/tool metadata is duplicated across several registries and regex maps.
- The semantic verifier is LLM-based and opt-in; deterministic postconditions should eventually be the primary verifier for effects/artifacts.
- Automatic resume remains deliberately disabled for tools without a complete effect/idempotency contract.

The immediate testing objective is therefore precise: prove the current integrated runtime is safe, truthful, stable, and effective across representative models before further architectural consolidation or performance optimization.
