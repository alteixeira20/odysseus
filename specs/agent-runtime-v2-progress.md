# Agent runtime v2 progress

This is a strangler refactor of the current `agentic-lab` working tree. The
working tree, including pre-existing uncommitted changes, is the source of
truth. No changes are staged or committed by this work.

Baseline:

- Branch: `agentic-lab`
- HEAD: `53ce95049db686588544776191c83ea5469232ad`
- Safety snapshot: `/tmp/odysseus-bash-lifecycle-repair`
- Compatibility facade: `src/agent_loop.py`
- Public wire contract: legacy SSE strings consumed by `static/js/chat.js`

## Extraction ledger

| Slice | Source | Destination | Compatibility | Tests | Behavior |
|---|---|---|---|---|---|
| Foreground Bash lifecycle | `src.agent_tools.subprocess_tools`, tool execution, agent stream, chat UI | Owned process-group runner plus `agentToolLifecycle.js` | Existing `bash` tool name and result fields retained; lifecycle fields are additive | `test_bash_lifecycle.py`, `agentToolLifecycle.test.mjs`, cancellation/runtime suites | Explicitly changed: bounded timeout, owned tree termination, partial output, shell recovery, terminal UI states |
| Runtime contracts | Legacy stream arguments and raw event dictionaries | `src/agent/contracts.py`, `events.py`, `config.py` | `stream_agent_loop` signature frozen; production SSE still encoded by legacy code | `test_agent_architecture.py`, runtime contracts | None; additive types and encoder |
| Conversation assembly | Four pure helpers in `agent_loop.py` | `src/agent/conversation.py` | Legacy private names remain aliases in `src.agent_loop` | `test_agent_conversation.py`, agent/prompt contract suites | None |
| Final metrics | `_compute_final_metrics` | `src/agent/telemetry/metrics.py` | `_compute_final_metrics` remains an identity-preserving alias | agent-loop and chat-metrics suites | None |
| Runaway-call detection | `_detect_runaway_call` | `src/agent/supervision/loop_breaker.py` | `_detect_runaway_call` remains an identity-preserving alias | loop-breaker and round-exhaustion suites | None |
| Preference dependency inversion | Agent imports from `routes.prefs_routes` | `src/user_preferences.py`; route compatibility wrappers | Route-private load/save names remain callable | preference, skill prompt, architecture suites | None |
| Per-run settings snapshot | Repeated numeric settings reads in `agent_loop.py` | `AgentSettingsSnapshot.capture()` | Existing defaults retained; stream signature unchanged | architecture, context-budget, runtime-contract suites | Explicitly changed: malformed numeric settings fall back safely instead of aborting the stream |
| Stable runtime API | Direct production imports of the legacy module | `src/agent/api.py` | Legacy signature remains in `src.agent_loop`; typed requests project onto it | architecture and route caller suites | None |
| Provider capabilities and errors | Scattered endpoint/model checks and stream-error parsing | `src/agent/providers/capabilities.py`, `errors.py` | Legacy private names remain aliases where imported by tests | provider, support-heuristic, fallback suites | None |
| Tool-call normalization | Native/fenced conversion in the loop | `src/agent/rounds/tool_calls.py` | `_resolve_tool_blocks` remains an identity-preserving alias | native threading, parsing, policy suites | None |
| Qwen memory-call policy | Inline finetune-specific call filtering between parsing and execution | `filter_odysseus_qwen_calls()` in `src/agent/rounds/tool_calls.py` | Converted/native call alignment and implicit-memory retry behavior retained | direct filter, Qwen, native-threading, replay, runtime suites | Explicitly changed after parity: an explicit request to browse saved memories now retains the valid read call instead of discarding it |
| Tool-result threading | Provider-specific assistant/tool message construction | `src/agent/execution/message_threading.py` | `_append_tool_results` remains an identity-preserving alias | native tool result threading and runtime suites | None |
| Provider idle stream pump | Cancellation-safe idle status loop | `src/agent/rounds/stream_consumer.py` | `_stream_with_idle_status` remains an identity-preserving alias | stream cancellation and runtime suites | None |
| Deterministic replay safety net | Live provider chunks and unstable prompt output | replay fixtures plus prompt hashes | Replays call the legacy facade and normalize only volatile fields | `test_agent_replay_golden.py` | None |
| Document normalization and projection | Inline native-argument and fenced-document mutable state | `src/agent/rounds/document_stream.py` | Legacy normalizer aliases remain; SSE is encoded with the legacy encoder | projector, document normalization, policy, replay suites | None |
| Fenced document pre-execution preview | Inline round-one ownership and later-round create/update preview parsing | `DocumentStreamProjector.preview_fenced_tool_blocks()` | Round-one frontend fence ownership and later duplicate previews retained | projector, replay, runtime, policy suites | None |
| Schema preparation | Inline provider schema filtering and byte accounting | `src/agent/rounds/schema_preparation.py` | Provider still receives the same ordered list or `None` | schema, routing, policy, runtime suites | None |
| Provider round accumulation | Mutable round text/reasoning/native-call/usage/model fields | `src/agent/rounds/provider_events.py` | Legacy wire forwarding and the characterized native-document quirk remain | provider event, replay, retry, document suites | None |
| Provider attempt lifecycle | Inline provider retries, backoff, stream projection, timing, and terminal-error handling | `src/agent/rounds/runner.py` | Legacy stream call remains patchable through `src.agent_loop`; exact SSE order retained | round runner, reliability, runtime, replay, policy, workspace, cancellation suites | None |
| Foreground tool task ownership | Inline task, progress queue, disconnect cleanup | `src/agent/execution/executor.py` | Existing progress/result ordering remains | executor, disconnect, Bash lifecycle suites | None |
| Tool result projection | Repeated result-to-UI/model rendering chains | `src/agent/execution/result_adapters.py` | Existing event fields and ordering remain | result adapter, ask-user, Bash, replay suites | None |
| Tool result projection registry | Inline web/document/UI/ask/plan/image/research/note/persistence event construction | `project_tool_result()` in `src/agent/execution/result_adapters.py` | Legacy duplicate document projections and exact pre/post-Qwen event order retained | result adapter, ask-user, replay, Qwen, native-threading suites | None |
| Tool batch lifecycle | Inline budget, policy, sequential execution, progress, cancellation, result projection, persistence, threading, and specialized completion | `src/agent/execution/batch_runner.py` | Facade injects legacy execution/format/threading/token seams; explicit nested-generator close preserves disconnect cancellation | direct batch, agent loop, policy, workspace, unknown-tool, native, Bash, disconnect suites | None |
| Persisted tool event resolution | `_resolved_tool_event_name` in `agent_loop.py` | `src/agent/execution/result_adapters.py` | Legacy private name remains an identity-preserving alias | result adapter, replay, runtime suites | None |
| Typed routing | Inline deterministic domain/continuation classifier | `src/agent/routing/classifier.py`, `context_targets.py`, `tool_domains.py` | Legacy private classifier/maps remain identity aliases | routing, ReDoS, domain, runtime suites | None; decision reasons are additive logs |
| Prompt contexts | Upload, workspace, local-machine, email-draft, and approved-plan helpers | `src/agent/prompting/contexts/*`, `plan_context.py` | Legacy helper imports remain identity aliases | prompt context, workspace, email, plan, prompt hash suites | None |
| Prompt assembly | Inline compact/full prompt assembly and shadowed obsolete rule definitions | `src/agent/prompting/builder.py` plus one effective legacy data catalog | `_assemble_prompt`, `_section_text`, and override patch seams remain callable | prompt snapshots, runtime, tool-policy, user-time suites | None; dead shadowed definitions removed after snapshot parity |
| Prompt message sequencing | Inline trusted-system insertion/merge and ordered request-context placement | `assemble_prompt_messages()` in `src/agent/prompting/builder.py` | Protected-message boundaries and exact document/email/integration/MCP/skill/time order retained | direct sequencing, injection audit, skill, time, replay, policy, workspace suites | None |
| Static base prompt | Trusted preamble and provider-specific base rules in the facade | `src/agent/prompting/base.py` | Legacy private constants remain aliases; runtime override resolution stays in the facade | direct base, prompt snapshot, runtime-contract, policy suites | None |
| Skill-index prompt context | Inline service loading, category grouping, and untrusted skill catalogue formatting | `src/agent/prompting/contexts/skills.py` | `_build_base_prompt` remains patchable and returns the same separate untrusted block | direct formatter, owner/toolset gating, prompt injection suites | None |
| Qwen minimal prompt packs | Inline document, notes, general, saved-memory, and recent-tool prompt constructors | `src/agent/prompting/odysseus_qwen.py` | All five legacy private names remain identity-preserving aliases | exact prompt-pack snapshots, Qwen adapter, document, replay, runtime suites | None |
| Odysseus-Qwen behavior adapter | Inline Qwen turn guards, deterministic summaries, and false-success predicates | `src/agent/providers/adapters/odysseus_qwen.py` | Legacy private helpers and regex constants remain aliases | Qwen adapter, result adapter, provider event, replay, runtime suites | None |
| Ordered supervisors | Inline intent-nudge/stall/verifier state | `src/agent/supervision/*` | Existing SSE decisions and prompt instructions retained | intent, stall, verifier, exhaustion suites | None |
| Deterministic final tool summaries | Final notes/calendar/tasks/email conditional chain | `select_deterministic_tool_summary()` in `src/agent/supervision/finalizer.py` | Latest matching-result precedence and empty-result stop behavior retained | direct finalizer, result adapter, batch, replay, agent-loop suites | None |
| Unknown native tool recovery | Unknown native call silently collapsed into an empty/finished round | `src/agent/rounds/tool_calls.py`, structured execution error | Legacy tuple resolver still drops unknown calls for compatibility; runtime uses the recoverable resolver | unknown-tool, native threading, continuation suites | Explicitly changed: returns a retryable result with close-name suggestions and continues |
| Per-task document/model execution context | Module-global active document and model pointers | `ContextVar` bridge in `src/agent_tools/document_tools.py` | Existing setter/getter/clear APIs retained and exported | concurrency isolation, active-document clear/route, owner-scope suites | Explicitly changed: concurrent agent tasks cannot overwrite each other's document/model context |
| Normalized provider finish-reason contract | `src/llm_core.py` never read the provider's per-choice `finish_reason` (OpenAI-compatible) or `done_reason` (Ollama native); root-caused a reported incident where a turn stopped silently after announcement text with no tool call | `src/agent/providers/finish_reason.py` (`ProviderFinishReason`, `ProviderFinished`, `normalize_finish_reason`, `classify_truncation`); wire-level `{"type": "finish", ...}` event in `src/llm_core.py`; consumed by `ProviderRoundAccumulator`/`DirectResponseAccumulator` (`src/agent/rounds/provider_events.py`) and surfaced on `ProviderAttemptOutcome` (`src/agent/rounds/runner.py`) | New internal wire event type, never forwarded to SSE (`forward_raw=False`/`forward_data=None` for `type: finish`); no existing event shape changed | `test_provider_finish_reason.py` (normalizer, classifier, accumulator wiring, wire-level capture for OpenAI-compatible + Ollama native) | Additive; no prior behavior relied on this field being absent |
| Incomplete native tool-call detection | Native call whose argument JSON failed to parse was silently dropped (`logger.error` only, no signal reaching the round loop) | `incomplete_native_calls` on `ResolvedToolCalls` (`src/agent/rounds/tool_calls.py`), populated by a pre-parse check ahead of the existing `function_call_to_tool_block` conversion | Conversion/drop behavior unchanged; this only adds an observable diagnostic alongside it | `test_provider_finish_reason.py::test_incomplete_native_call_is_truncated_even_without_length`, `resolve_round_tool_calls` batch | None; the call was already never executed on parse failure — this makes that fact visible to the truncation classifier |
| Bounded safe continuation after truncation | `src/agent_loop.py`'s `if not tool_blocks: ... break # no tools — done` accepted a truncated (output-limit or incomplete-native-call) round as a deliberate final answer | Truncation check at the top of the `if not tool_blocks:` branch in `stream_agent_loop`, gated by `classify_truncation()` and a run-scoped `_truncation_continuation_count` (cap `_MAX_TRUNCATION_CONTINUATIONS = 4`) | New `truncation_continuation` SSE event (additive); existing completion-verifier/intent-nudge/loop-breaker/`break` paths run unchanged once the cap is hit or the round wasn't truncated | `test_agent_truncation_continuation.py` (resumes on `length`, no continuation on clean `stop`, bounded at 4 continuations) | Explicitly changed: a tool-free round truncated by the provider's output limit no longer ends the turn; the agent now sees a compact continuation instruction and keeps going, bounded and side-effect-free (no tool ran this round, so nothing can be duplicated) |
| Finish-event ordering (OpenAI-compatible) | `src/llm_core.py` yielded the internal `{"type": "finish", ...}` event as soon as a chunk's `finish_reason` was read, before that same chunk's own text/tool-call deltas were processed — a provider that puts `finish_reason` on its final content/tool-call chunk raced the terminal event ahead of the content it terminates | `_captured_finish_reason`/`_emit_finish_event()` in `src/llm_core.py`: the raw reason is captured immediately but only ever emitted at the stream's two exit points ([DONE], end-of-stream), after all trailing deltas/tool-call assembly for that attempt have already been yielded | No behavior change for the common case (finish_reason on its own trailing chunk); only changes ordering when finish_reason and content/tool-calls share one chunk | `test_provider_finish_reason.py::test_openai_compatible_finish_ordered_after_same_chunk_content`, `..._tool_call` | Bug fix: guarantees "all deltas from a chunk → accumulated tool-call state → terminal metadata → DONE" |
| Finish-reason contract extended to Anthropic-native + ChatGPT Subscription | Only the OpenAI-compatible and Ollama-native branches in `src/llm_core.py` populated the `finish` event; Anthropic's `message_delta.delta.stop_reason` and the Responses API's `response.status`/`incomplete_details.reason` were read for other purposes but never surfaced as a normalized finish reason | Anthropic branch captures `stop_reason` at `message_delta`, emits `finish` at `message_stop` (after tool_calls/usage, before DONE); ChatGPT Subscription branch derives `stop`/`incomplete_details.reason` at `response.completed`, emits `finish` before DONE; both emit `reason=None` if the stream ends without a terminal event; `"max_output_tokens"`/`"incomplete"` added to `normalize_finish_reason`'s map | Purely additive wire event, same `forward_raw=False` non-forwarding contract as the other two branches | `test_provider_finish_reason.py::test_anthropic_native_*`, `test_chatgpt_subscription_*` (6 tests) | Closes the gap noted in the prior pass's "Next slices" — these two providers now participate in truncation-continuation on equal footing with OpenAI-compatible/Ollama |
| Fenced tool-call truncation detection | `classify_truncation()` only recognized truncation via `finish_reason=length` or an incomplete *native* call; a local/non-native model truncated mid a fenced ```` ```bash ```` block produced zero tool_blocks (safe — `parse_tool_blocks()` only returns complete blocks) but no signal reached the continuation policy, so it could still be accepted as a deliberate empty answer | `_has_unclosed_tool_fence()` in `src/agent/rounds/tool_calls.py`: odd count of `` ``` `` markers whose last (unmatched) opener is tagged with a name from `TOOL_TAGS`; surfaced as `ResolvedToolCalls.fenced_call_unclosed` → `ProviderFinished.had_unclosed_fenced_call` → new OR-branch in `classify_truncation()` | Purely additive signal; `parse_tool_blocks()` itself is untouched, so fenced-call execution semantics are unchanged | `test_provider_finish_reason.py::test_unclosed_bash_fence_*`, `test_closed_bash_fence_*`, `test_unclosed_non_tool_fence_is_not_flagged`, `test_unclosed_fence_not_flagged_when_fenced_parsing_is_skipped` | Explicitly changed: a fenced tool call cut off mid-stream (no native tool-calling model in play) now triggers the same bounded, side-effect-free continuation as a native truncation |
| Truncation-continuation policy extraction | The truncation classify/cap/instruction-text logic lived inline in `stream_agent_loop`'s `if not tool_blocks:` branch (~65 lines) | `evaluate_truncation_continuation()` in `src/agent/supervision/continuation.py`, returning a `SupervisorDecision` (same contract as `evaluate_intent_without_action`); `agent_loop.py` calls it and only projects the decision onto the wire (SSE event, appended message, `continue`) | `stream_agent_loop`'s SSE contract (`truncation_continuation` event shape, cap behavior) is unchanged | `test_supervision_continuation.py` (6 unit tests) plus the existing `test_agent_truncation_continuation.py` end-to-end suite (unchanged, still passing through the new call site) | None; policy relocated, not changed |
| Frontend truncation-continuation status | The `truncation_continuation` SSE event was never handled in `static/js/chat.js` — harmless (falls through the type-dispatch chain unmatched) but gave the user no indication a round was resumed after an output-limit cutoff; the following `agent_step` just showed the generic "Generating response" spinner | `_pendingTruncationContinuation` flag set by the `truncation_continuation` handler, consumed and cleared by the very next `agent_step` (its guaranteed successor on the wire) to label that round's spinner "Continuing response" instead of "Generating response" | No new DOM states; reuses the existing per-round bubble/spinner lifecycle, so there is no new way for a card to get stuck | `node --check`, full JS suite (126 passed, no regressions); no dedicated unit test — `handleChatSubmit`'s SSE loop is a large stateful closure not covered by the existing JS unit-test harness, see Deferred work | Cosmetic only: never surfaces the raw provider reason to the user, matches the existing owner/admin-vs-user diagnostic split |

## Current invariants

- `src/agent/**` never imports `routes/**`.
- `src/agent_loop.py` no longer imports an HTTP route module.
- Production callers import the stable `src.agent.api`; the API lazily delegates
  to the legacy facade during the strangler transition.
- Existing private helper imports remain valid while consumers migrate.
- Conversation helpers, metrics, and loop detection have one implementation.
- Runtime settings used by orchestration are read and normalized once per run.
- No event ordering or existing SSE field has changed in the structural slices.

## Validation checkpoints

- Bash lifecycle: 21 tests passed.
- Initial affected Python batches: 240 tests passed.
- Initial full Python suite after Bash repair: 4815 passed, 3 skipped.
- Full JavaScript suite after Bash repair: 127 passed.
- Contracts/conversation/prompt batch: 158 tests passed.
- Metrics batch: 93 tests passed.
- Loop supervision batch: 25 tests passed.
- Settings, preference, context, and runtime batches: 111 tests passed.
- Document projector integration batch: 35 tests passed.
- First post-extraction full Python gate: 4879 passed, 3 skipped.
- Persisted event resolver and Qwen adapter focused gate: 50 tests passed.
- Provider attempt runner focused/affected gates: 144 tests passed.
- Post-runner full Python gate: 4907 passed, 3 skipped.
- Qwen prompt-pack and prompt-builder focused gates: 105 tests passed.
- Final full Python parity gate: 4909 passed, 3 skipped.
- Final full JavaScript/streaming gate: 127 passed.
- Tool projection/batch-runner focused and affected gates: 269 tests passed.
- Post-batch full Python parity gate: 4917 passed, 3 skipped.
- Task-local document/model context focused gate: 19 tests passed.
- Post-context full Python parity gate: 4918 passed, 3 skipped.
- Fenced document preview focused gate: 58 tests passed.
- Static prompt-base focused gate: 50 tests passed.
- Skill-index context focused gate: 58 tests passed.
- Deterministic finalizer focused/affected gate: 92 tests passed.
- Qwen tool-call policy parity gate: 73 tests passed.
- Explicit memory-browse reliability gate: 89 tests passed.
- Post-prompt/Qwen full Python parity gate: 4932 passed, 3 skipped.
- Prompt message sequencing focused/affected gate: 83 tests passed.
- Post-sequencing full Python parity gate: 4935 passed, 3 skipped.
- `py_compile` and `git diff --check` pass after each completed slice.
- Finish-reason/truncation-continuation focused gate: 19 tests passed
  (`test_provider_finish_reason.py`, `test_agent_truncation_continuation.py`).
- Broader affected batch (round runner, provider events, tool calls, agent
  loop, replay, intent/loop-breaker/verifier, ask-user, document stream,
  Qwen): 128 + 57 tests passed.
- Post-truncation-fix full Python parity gate: 4954 passed, 3 skipped.
- Full JavaScript/streaming gate: 126 passed.
- Finish-ordering/provider-extension/fenced-truncation/continuation-extraction
  focused gate: 44 + 6 tests passed (`test_provider_finish_reason.py`,
  `test_agent_truncation_continuation.py`, `test_agent_unknown_tool_recovery.py`,
  `test_agent_reliability_repair.py`, `test_supervision_continuation.py`).
- Post-this-session full Python parity gate: 4973 passed, 3 skipped.
- Post-this-session full JavaScript/streaming gate: 126 passed (no new JS
  unit coverage for the chat.js spinner-label change — see Deferred work).
- `py_compile` and `git diff --check` pass after each completed slice.

## Next slices

1. Extract the remaining document/email/skill/local context assembly from
   `_build_system_prompt` without changing prompt wording or message order.
2. Move round-specific Qwen tool-call filtering and force-answer policy behind
   typed round decisions.
3. Compose the provider and tool-batch seams after the intervening supervisor
   policy is explicit.
4. Reduce the outer orchestrator to routing, round iteration, supervision,
   metrics, and finalization.
5. Canonicalize the tool catalog only after the runtime split is stable.
6. Repair the characterized native document/tool-call assignment quirk as a
   separate behavior change.
7. Build the canonical `ToolRegistry` (one typed source of truth for
   schemas/handlers/aliases/risk/idempotency), unify `todowrite`/`update_plan`
   behind one `PlanService`, and add the concise shell-literacy/
   action-commitment prompt guidance (find/sed/rg/jq/xargs/Git) — none of
   these were started in this pass; deliberately deferred (large, separate
   surfaces from the truncation/finish-reason repair, each warranting its own
   slice with its own validation gate).
8. Add a production-backed context-budget report and bounded/resumable
   foundational tool results (read_file/grep/glob/ls/Bash output truncation
   metadata) — not started; see the session report.
9. Typed per-run execution context (workspace identity, cancellation,
   continuation state) to replace the loose dict/closure-variable state
   `stream_agent_loop` currently carries — not started.

Behavioral improvements after the relevant parity gate:

- first-class unknown-tool recovery;
- sequence-aware protected context groups;
- structured supervisor decisions and progress signals;
- explicit per-run tool execution context;
- canonical tool catalog validation;
- a tool-free round truncated by the provider's output limit (or an
  incomplete native tool call) no longer ends the turn silently — it is
  classified via normalized provider metadata and resumed with a bounded,
  side-effect-free continuation (`_MAX_TRUNCATION_CONTINUATIONS = 4`);
- the provider's terminal metadata event can no longer race ahead of that
  same chunk's own content/tool-call deltas;
- Anthropic-native and ChatGPT Subscription streams now participate in
  truncation classification on equal footing with OpenAI-compatible/Ollama;
- a fenced (non-native) tool call truncated mid-stream is now recognized as
  truncation, not a deliberate empty answer;
- the truncation-continuation policy is a named, independently testable
  component instead of an inline conditional block in the facade;
- the user sees a distinct "Continuing response" status instead of a
  generic spinner when a round resumes after an output-limit cutoff.
