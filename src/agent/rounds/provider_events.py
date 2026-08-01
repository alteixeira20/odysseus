"""Stateful projection of decoded provider events for one agent round."""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from src.agent.events import AgentEvent
from src.agent.providers.finish_reason import (
    ProviderFinishReason,
    normalize_finish_reason,
)
from src.agent.rounds.document_stream import (
    DocumentStreamProjector,
    normalize_odysseus_qwen_text,
    strip_document_model_artifacts,
)


@dataclass(frozen=True)
class ProviderEventProjection:
    substantive: bool = False
    forward_data: Optional[Mapping[str, Any]] = None
    forward_raw: bool = False
    document_events: tuple[AgentEvent, ...] = ()
    visible_delta: str = ""
    reasoning_delta: str = ""
    usage_input: int = 0
    usage_output: int = 0
    usage_is_real: bool = False
    first_visible: bool = False
    first_reasoning: bool = False
    first_tool_calls: bool = False
    stream_error_text: str = ""
    delta_handled: bool = False


@dataclass
class ProviderRoundAccumulator:
    requested_model: str
    actual_model: str
    round_number: int
    odysseus_qwen_finetune: bool = False
    document_stream: DocumentStreamProjector = field(
        default_factory=DocumentStreamProjector
    )
    text: str = ""
    reasoning: str = ""
    native_tool_calls: list = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    last_input_tokens: int = 0
    has_real_usage: bool = False
    backend_gen_tps: float = 0
    backend_prefill_tps: float = 0
    visible_chars: int = 0
    raw_finish_reason: Optional[str] = None
    normalized_finish_reason: ProviderFinishReason = ProviderFinishReason.UNKNOWN
    finish_event_seen: bool = False
    # Whether src/llm_core.py observed the provider's own terminal signal
    # (explicit [DONE], Ollama "done": true, Anthropic message_stop,
    # response.completed, ...) before emitting this finish event, as
    # opposed to synthesizing one after its read loop simply ran out of
    # lines. Defaults True: a source that omits the tag is assumed to have
    # completed normally, so only branches that explicitly tag it False
    # (a raw transport EOF) can trigger interruption classification.
    protocol_terminal_seen: bool = True
    candidate_id: Optional[str] = None
    candidate_index: Optional[int] = None

    def consume(
        self,
        data: Mapping[str, Any],
        *,
        tool_call_blocked: bool = False,
        create_document_blocked: bool = False,
    ) -> ProviderEventProjection:
        event_type = data.get("type")
        substantive = bool(
            data.get("delta")
            or event_type in ("tool_call_delta", "tool_calls")
        )
        document_events: tuple[AgentEvent, ...] = ()

        if event_type == "candidate_selected":
            candidate = str(data.get("candidate_id") or "")
            if not candidate:
                return ProviderEventProjection()
            if self.candidate_id is not None and self.candidate_id != candidate:
                raise ValueError("provider round attempted to select multiple candidates")
            self.candidate_id = candidate
            try:
                self.candidate_index = int(data.get("candidate_index"))
            except (TypeError, ValueError):
                self.candidate_index = None
            self.actual_model = str(data.get("model") or self.actual_model)
            return ProviderEventProjection()

        if event_type == "tool_call_delta":
            if tool_call_blocked:
                return ProviderEventProjection(substantive=substantive)
            document_events = (
                self.document_stream.consume_native_argument_delta(
                    data.get("arg_delta", "")
                )
            )

        # Preserve the legacy event-chain behavior: once native document
        # projection opens, later provider event kinds are swallowed until the
        # round ends. This is intentionally characterized before repair.
        if self.document_stream.opened:
            return ProviderEventProjection(
                substantive=substantive,
                document_events=document_events,
            )

        if event_type == "tool_calls":
            first_tool_calls = not self.native_tool_calls
            self.native_tool_calls = list(data.get("calls", []))
            return ProviderEventProjection(
                substantive=substantive,
                first_tool_calls=bool(
                    first_tool_calls and self.native_tool_calls
                ),
            )

        if event_type == "finish":
            self.finish_event_seen = True
            self.raw_finish_reason = data.get("reason")
            self.normalized_finish_reason = normalize_finish_reason(
                self.raw_finish_reason
            )
            self.protocol_terminal_seen = bool(
                data.get("protocol_terminal_seen", True)
            )
            return ProviderEventProjection(substantive=substantive)

        if event_type == "usage":
            usage = data.get("data", {}) or {}
            self.actual_model = usage.get("model") or self.actual_model
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.last_input_tokens = input_tokens
            self.has_real_usage = True
            if usage.get("gen_tps"):
                self.backend_gen_tps = usage["gen_tps"]
            if usage.get("prefill_tps"):
                self.backend_prefill_tps = usage["prefill_tps"]
            return ProviderEventProjection(
                substantive=substantive,
                usage_input=input_tokens,
                usage_output=output_tokens,
                usage_is_real=True,
            )

        if event_type == "fallback":
            self.actual_model = (
                data.get("answered_by") or self.actual_model
            )
            return ProviderEventProjection(
                substantive=substantive,
                forward_raw=True,
            )

        if event_type == "model_actual":
            self.actual_model = data.get("model") or self.actual_model
            forwarded = dict(data)
            forwarded["requested_model"] = self.requested_model
            return ProviderEventProjection(
                substantive=substantive,
                forward_data=forwarded,
            )

        if "delta" in data:
            delta = data.get("delta")
            if not isinstance(delta, str):
                return ProviderEventProjection(substantive=substantive)
            forwarded = dict(data)
            if data.get("thinking"):
                first_reasoning = not self.reasoning
                self.reasoning += delta
                return ProviderEventProjection(
                    substantive=substantive,
                    forward_data=forwarded,
                    reasoning_delta=delta,
                    first_reasoning=first_reasoning,
                    delta_handled=True,
                )

            first_visible = not self.text
            visible_delta = delta
            if self.odysseus_qwen_finetune:
                visible_delta = normalize_odysseus_qwen_text(
                    strip_document_model_artifacts(visible_delta)
                )
            self.text += visible_delta
            self.visible_chars += len(visible_delta)
            forwarded["delta"] = visible_delta
            document_events = self.document_stream.consume_visible_text(
                self.text,
                round_number=self.round_number,
                create_document_blocked=create_document_blocked,
            )
            return ProviderEventProjection(
                substantive=substantive,
                forward_data=(
                    None
                    if self.odysseus_qwen_finetune
                    else forwarded
                ),
                document_events=document_events,
                visible_delta=visible_delta,
                first_visible=first_visible,
                delta_handled=True,
            )

        if data.get("error"):
            return ProviderEventProjection(
                substantive=substantive,
                stream_error_text=(
                    "\n\n*[Stream error: "
                    + str(data.get("error", "unknown"))
                    + "]*"
                ),
            )

        return ProviderEventProjection(substantive=substantive)


@dataclass
class DirectResponseAccumulator:
    requested_model: str
    actual_model: str
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw_finish_reason: Optional[str] = None
    normalized_finish_reason: ProviderFinishReason = ProviderFinishReason.UNKNOWN
    finish_event_seen: bool = False
    protocol_terminal_seen: bool = True
    candidate_id: Optional[str] = None

    def consume(
        self,
        data: Mapping[str, Any],
    ) -> ProviderEventProjection:
        event_type = data.get("type")
        if event_type == "candidate_selected":
            self.candidate_id = str(data.get("candidate_id") or "") or None
            self.actual_model = str(data.get("model") or self.actual_model)
            return ProviderEventProjection()
        if event_type == "finish":
            self.finish_event_seen = True
            self.raw_finish_reason = data.get("reason")
            self.normalized_finish_reason = normalize_finish_reason(
                self.raw_finish_reason
            )
            self.protocol_terminal_seen = bool(
                data.get("protocol_terminal_seen", True)
            )
            return ProviderEventProjection()
        if event_type == "usage":
            usage = data.get("data", {}) or {}
            self.actual_model = usage.get("model") or self.actual_model
            input_tokens = usage.get("input_tokens", 0) or 0
            output_tokens = usage.get("output_tokens", 0) or 0
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            return ProviderEventProjection(
                usage_input=input_tokens,
                usage_output=output_tokens,
                usage_is_real=True,
            )
        if event_type == "model_actual":
            self.actual_model = data.get("model") or self.actual_model
            forwarded = dict(data)
            forwarded["requested_model"] = self.requested_model
            return ProviderEventProjection(forward_data=forwarded)
        if event_type == "fallback":
            self.actual_model = (
                data.get("answered_by") or self.actual_model
            )
            return ProviderEventProjection(forward_raw=True)
        if "delta" in data:
            delta = data.get("delta", "")
            visible_delta = "" if data.get("thinking") else delta
            if visible_delta:
                self.text += visible_delta
            return ProviderEventProjection(
                forward_raw=True,
                visible_delta=visible_delta,
                delta_handled=True,
            )
        return ProviderEventProjection(forward_raw=True)
