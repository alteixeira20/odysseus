"""Provider attempt and retry lifecycle for one agent round."""

import asyncio
from dataclasses import dataclass
import json
import logging
import random
import time
from typing import Any, AsyncIterator, Callable, Mapping, Optional

from src.agent.events import encode_legacy_sse, run_status_event
from src.agent.providers.errors import (
    is_transient_error,
    stream_error_details,
)
from src.agent.rounds.provider_events import ProviderRoundAccumulator
from src.agent.rounds.stream_consumer import stream_with_idle_status


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderAttemptRequest:
    round_number: int
    session_id: Optional[str]
    timeout_seconds: float
    total_started_at: float
    round_started_at: float
    max_attempts: int = 3
    first_provider_byte_seen: bool = False
    first_reasoning_seen: bool = False
    first_visible_seen: bool = False
    first_tool_call_seen: bool = False
    create_document_blocked: bool = False


@dataclass(frozen=True)
class ProviderAttemptOutcome:
    attempts: int
    substantive: bool
    fatal: bool
    terminal_error_chunk: Optional[str]
    first_provider_byte_elapsed: Optional[float]
    provider_ttfb: Optional[float]
    first_reasoning_elapsed: Optional[float]
    first_visible_elapsed: Optional[float]
    first_tool_call_elapsed: Optional[float]
    provider_capacity_wait: float
    first_event_seen: bool
    first_delta_seen: bool


class ProviderAttemptRunner:
    """Stream one provider round, retrying only safe pre-content failures."""

    def __init__(
        self,
        request: ProviderAttemptRequest,
        accumulator: ProviderRoundAccumulator,
        stream_factory: Callable[[], Any],
        *,
        tool_call_blocked: Optional[
            Callable[[Mapping[str, Any]], bool]
        ] = None,
        idle_stream: Callable[..., AsyncIterator[str]] = (
            stream_with_idle_status
        ),
        transient_error: Callable[[str], bool] = is_transient_error,
        error_details: Callable[[str], Mapping[str, Any]] = (
            stream_error_details
        ),
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], Any] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.request = request
        self.accumulator = accumulator
        self.stream_factory = stream_factory
        self.tool_call_blocked = tool_call_blocked or (lambda data: False)
        self.idle_stream = idle_stream
        self.transient_error = transient_error
        self.error_details = error_details
        self.clock = clock
        self.sleeper = sleeper
        self.uniform = uniform
        self.outcome: Optional[ProviderAttemptOutcome] = None

    async def stream(self) -> AsyncIterator[str]:
        request = self.request
        deadline = request.round_started_at + max(
            request.timeout_seconds * 4,
            1200,
        )
        attempt = 0
        substantive = False
        fatal = False
        terminal_error_chunk: Optional[str] = None
        first_event_seen = False
        first_delta_seen = False
        first_provider_byte_elapsed: Optional[float] = None
        provider_ttfb: Optional[float] = None
        first_reasoning_elapsed: Optional[float] = None
        first_visible_elapsed: Optional[float] = None
        first_tool_call_elapsed: Optional[float] = None
        provider_capacity_wait = 0.0

        while (
            attempt < request.max_attempts
            and not substantive
            and not fatal
        ):
            attempt += 1
            attempt_error_chunk: Optional[str] = None
            yield run_status_event(
                "contacting_provider",
                "Contacting provider",
                attempt=attempt,
                max_attempts=request.max_attempts,
            )
            provider_stream = self.stream_factory()
            async for chunk in self.idle_stream(provider_stream):
                if not first_event_seen:
                    first_event_seen = True
                    logger.info(
                        "[agent-timing] first_event round=%s attempt=%s "
                        "elapsed=%.3fs kind=%s",
                        request.round_number,
                        attempt,
                        self.clock() - request.round_started_at,
                        (
                            "error"
                            if chunk.startswith("event: error")
                            else "data"
                        ),
                    )
                if self.clock() > deadline:
                    logger.warning(
                        "[agent-timing] round_deadline round=%s "
                        "elapsed=%.3fs deadline_s=%s",
                        request.round_number,
                        self.clock() - request.round_started_at,
                        max(request.timeout_seconds * 4, 1200),
                    )
                    break
                if chunk.startswith("event: error"):
                    attempt_error_chunk = chunk
                    terminal_error_chunk = chunk
                    if (
                        not substantive
                        and self.transient_error(chunk)
                        and attempt < request.max_attempts
                    ):
                        logger.warning(
                            "[agent-timing] transient_error round=%s "
                            "attempt=%s/%s elapsed=%.3fs chunk=%r; retrying",
                            request.round_number,
                            attempt,
                            request.max_attempts,
                            self.clock() - request.round_started_at,
                            chunk[:300],
                        )
                        break
                    fatal = True
                    logger.warning(
                        "[agent-timing] stream_error round=%s "
                        "elapsed=%.3fs chunk=%r",
                        request.round_number,
                        self.clock() - request.round_started_at,
                        chunk[:500],
                    )
                    break
                if (
                    chunk.startswith("data: ")
                    and not chunk.startswith("data: [DONE]")
                ):
                    try:
                        data = json.loads(chunk[6:])
                        if data.get("type") == "run_status":
                            try:
                                provider_capacity_wait += (
                                    float(
                                        data.get("capacity_wait_ms") or 0
                                    )
                                    / 1000
                                )
                            except (TypeError, ValueError):
                                pass
                            yield chunk
                            continue
                        if data.get("type") == "provider_timing":
                            if (
                                data.get("phase") == "first_byte"
                                and not request.first_provider_byte_seen
                                and first_provider_byte_elapsed is None
                            ):
                                first_provider_byte_elapsed = (
                                    self.clock() - request.total_started_at
                                )
                                try:
                                    provider_ttfb = (
                                        float(data.get("duration_ms"))
                                        / 1000
                                    )
                                except (TypeError, ValueError):
                                    provider_ttfb = None
                            yield chunk
                            continue
                        projection = self.accumulator.consume(
                            data,
                            tool_call_blocked=bool(
                                data.get("type") == "tool_call_delta"
                                and self.tool_call_blocked(data)
                            ),
                            create_document_blocked=(
                                request.create_document_blocked
                            ),
                        )
                        if projection.substantive:
                            substantive = True
                        if projection.delta_handled and not first_delta_seen:
                            first_delta_seen = True
                            logger.info(
                                "[agent-timing] first_provider_delta "
                                "round=%s elapsed=%.3fs total_elapsed=%.3fs "
                                "thinking=%s",
                                request.round_number,
                                self.clock() - request.round_started_at,
                                self.clock() - request.total_started_at,
                                bool(data.get("thinking")),
                            )
                        if (
                            projection.first_reasoning
                            and not request.first_reasoning_seen
                            and first_reasoning_elapsed is None
                        ):
                            first_reasoning_elapsed = (
                                self.clock() - request.total_started_at
                            )
                            logger.info(
                                "[agent-timing] phase=first_reasoning "
                                "session=%s round=%s elapsed_s=%.3f",
                                request.session_id,
                                request.round_number,
                                first_reasoning_elapsed,
                            )
                            yield run_status_event(
                                "provider_reasoning",
                                "Provider is reasoning",
                            )
                        if (
                            projection.first_visible
                            and not request.first_visible_seen
                            and first_visible_elapsed is None
                        ):
                            first_visible_elapsed = (
                                self.clock() - request.total_started_at
                            )
                            logger.info(
                                "[agent-timing] phase=first_visible_text "
                                "session=%s round=%s elapsed_s=%.3f",
                                request.session_id,
                                request.round_number,
                                first_visible_elapsed,
                            )
                        if (
                            projection.first_tool_calls
                            and not request.first_tool_call_seen
                            and first_tool_call_elapsed is None
                        ):
                            first_tool_call_elapsed = (
                                self.clock() - request.total_started_at
                            )
                            logger.info(
                                "Agent round %s: received %s native "
                                "tool call(s)",
                                request.round_number,
                                len(self.accumulator.native_tool_calls),
                            )
                        if data.get("type") == "fallback":
                            logger.warning(
                                "[agent] round %s fell back: %s -> %s",
                                request.round_number,
                                data.get("selected_model"),
                                data.get("answered_by"),
                            )
                        if projection.forward_raw:
                            yield chunk
                        elif projection.forward_data is not None:
                            yield (
                                "data: "
                                + json.dumps(
                                    dict(projection.forward_data)
                                )
                                + "\n\n"
                            )
                        for event in projection.document_events:
                            if event.kind == "doc_stream_open":
                                logger.info(
                                    "Doc streaming: open title=%r lang=%r",
                                    event.payload.get("title"),
                                    event.payload.get("language"),
                                )
                            yield encode_legacy_sse(event)
                        if projection.stream_error_text:
                            logger.error(
                                "Agent round %s: stream error: %s",
                                request.round_number,
                                data.get("error", "unknown"),
                            )
                            yield (
                                "data: "
                                + json.dumps(
                                    {
                                        "delta": (
                                            projection.stream_error_text
                                        )
                                    }
                                )
                                + "\n\n"
                            )
                    except json.JSONDecodeError:
                        if request.round_number == 1:
                            yield chunk
                elif chunk.startswith("event: "):
                    yield chunk

            if (
                not substantive
                and attempt_error_chunk
                and not fatal
            ):
                details = self.error_details(attempt_error_chunk)
                try:
                    retry_after = float(details.get("retry_after"))
                except (TypeError, ValueError):
                    retry_after = None
                if (
                    retry_after is not None
                    and not 0 <= retry_after <= 30
                ):
                    retry_after = None
                base_backoff = min(0.5 * (2 ** (attempt - 1)), 4.0)
                backoff = (
                    retry_after
                    if retry_after is not None
                    else min(
                        base_backoff + self.uniform(0.0, 0.25),
                        4.0,
                    )
                )
                yield run_status_event(
                    "retrying",
                    (
                        "Retrying provider request "
                        f"({attempt + 1}/{request.max_attempts})"
                    ),
                    attempt=attempt + 1,
                    max_attempts=request.max_attempts,
                    delay_s=round(backoff, 3),
                )
                logger.info(
                    "[agent-timing] retry session=%s round=%s "
                    "attempt=%s delay_s=%.3f status=%s",
                    request.session_id,
                    request.round_number,
                    attempt + 1,
                    backoff,
                    details.get("status"),
                )
                await self.sleeper(backoff)

        self.outcome = ProviderAttemptOutcome(
            attempts=attempt,
            substantive=substantive,
            fatal=fatal,
            terminal_error_chunk=terminal_error_chunk,
            first_provider_byte_elapsed=first_provider_byte_elapsed,
            provider_ttfb=provider_ttfb,
            first_reasoning_elapsed=first_reasoning_elapsed,
            first_visible_elapsed=first_visible_elapsed,
            first_tool_call_elapsed=first_tool_call_elapsed,
            provider_capacity_wait=provider_capacity_wait,
            first_event_seen=first_event_seen,
            first_delta_seen=first_delta_seen,
        )
