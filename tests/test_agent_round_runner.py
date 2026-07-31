import asyncio
import json

import pytest

from src.agent.rounds.provider_events import ProviderRoundAccumulator
from src.agent.rounds.runner import (
    ProviderAttemptRequest,
    ProviderAttemptRunner,
)


def _error_chunk(**payload):
    return "event: error\ndata: " + json.dumps(payload) + "\n\n"


def _data_chunk(payload):
    return "data: " + json.dumps(payload) + "\n\n"


def _request(round_number=1, **kwargs):
    return ProviderAttemptRequest(
        round_number=round_number,
        session_id="session",
        timeout_seconds=30,
        total_started_at=0,
        round_started_at=0,
        **kwargs,
    )


def _stream_factory(attempt_chunks):
    calls = {"count": 0}

    def factory():
        chunks = attempt_chunks[calls["count"]]
        calls["count"] += 1

        async def stream():
            for chunk in chunks:
                yield chunk

        return stream()

    return factory, calls


async def _no_sleep(delay):
    return None


@pytest.mark.asyncio
async def test_transient_pre_content_error_retries_then_streams_answer():
    factory, calls = _stream_factory(
        [
            [_error_chunk(status=502, text="Bad gateway")],
            [_data_chunk({"delta": "answer"}), "data: [DONE]\n\n"],
        ]
    )
    accumulator = ProviderRoundAccumulator(
        requested_model="model",
        actual_model="model",
        round_number=1,
    )
    runner = ProviderAttemptRunner(
        _request(),
        accumulator,
        factory,
        sleeper=_no_sleep,
        uniform=lambda low, high: 0,
        clock=lambda: 1,
    )

    chunks = [chunk async for chunk in runner.stream()]
    phases = [
        json.loads(chunk[6:]).get("phase")
        for chunk in chunks
        if chunk.startswith("data: ")
    ]

    assert phases == [
        "contacting_provider",
        "retrying",
        "contacting_provider",
        None,
    ]
    assert calls["count"] == 2
    assert accumulator.text == "answer"
    assert runner.outcome is not None
    assert runner.outcome.attempts == 2
    assert runner.outcome.substantive is True
    assert runner.outcome.fatal is False


@pytest.mark.asyncio
async def test_three_transient_failures_produce_one_terminal_outcome():
    error = _error_chunk(status=503, retry_after=0)
    factory, calls = _stream_factory([[error], [error], [error]])
    accumulator = ProviderRoundAccumulator(
        requested_model="model",
        actual_model="model",
        round_number=1,
    )
    runner = ProviderAttemptRunner(
        _request(),
        accumulator,
        factory,
        sleeper=_no_sleep,
        clock=lambda: 1,
    )

    chunks = [chunk async for chunk in runner.stream()]

    assert calls["count"] == 3
    assert error not in chunks
    assert runner.outcome is not None
    assert runner.outcome.fatal is True
    assert runner.outcome.terminal_error_chunk == error
    assert runner.outcome.attempts == 3


@pytest.mark.asyncio
async def test_visible_content_prevents_retry_after_provider_error():
    error = _error_chunk(status=502)
    factory, calls = _stream_factory(
        [[_data_chunk({"delta": "partial"}), error]]
    )
    accumulator = ProviderRoundAccumulator(
        requested_model="model",
        actual_model="model",
        round_number=1,
    )
    runner = ProviderAttemptRunner(
        _request(),
        accumulator,
        factory,
        sleeper=_no_sleep,
        clock=lambda: 1,
    )

    chunks = [chunk async for chunk in runner.stream()]

    assert calls["count"] == 1
    assert accumulator.text == "partial"
    assert sum('"delta": "partial"' in chunk for chunk in chunks) == 1
    assert runner.outcome is not None
    assert runner.outcome.substantive is True
    assert runner.outcome.fatal is True


@pytest.mark.asyncio
async def test_capacity_and_first_byte_timings_are_aggregated():
    factory, _ = _stream_factory(
        [
            [
                _data_chunk(
                    {
                        "type": "run_status",
                        "capacity_wait_ms": 250,
                    }
                ),
                _data_chunk(
                    {
                        "type": "provider_timing",
                        "phase": "first_byte",
                        "duration_ms": 125,
                    }
                ),
                _data_chunk({"delta": "think", "thinking": True}),
                _data_chunk({"delta": "visible"}),
            ]
        ]
    )
    accumulator = ProviderRoundAccumulator(
        requested_model="model",
        actual_model="model",
        round_number=1,
    )
    ticks = iter((1, 2, 3, 4, 5, 6, 7, 8, 9, 10))
    runner = ProviderAttemptRunner(
        _request(),
        accumulator,
        factory,
        sleeper=_no_sleep,
        clock=lambda: next(ticks),
    )

    chunks = [chunk async for chunk in runner.stream()]

    assert any("provider_reasoning" in chunk for chunk in chunks)
    assert runner.outcome is not None
    assert runner.outcome.provider_capacity_wait == 0.25
    assert runner.outcome.provider_ttfb == 0.125
    assert runner.outcome.first_provider_byte_elapsed is not None
    assert runner.outcome.first_reasoning_elapsed is not None
    assert runner.outcome.first_visible_elapsed is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("round_number", "forwarded"),
    ((1, True), (2, False)),
)
async def test_malformed_provider_data_preserves_round_one_compatibility(
    round_number,
    forwarded,
):
    malformed = "data: {not-json}\n\n"
    factory, _ = _stream_factory(
        [[malformed], [malformed], [malformed]]
    )
    accumulator = ProviderRoundAccumulator(
        requested_model="model",
        actual_model="model",
        round_number=round_number,
    )
    runner = ProviderAttemptRunner(
        _request(round_number=round_number),
        accumulator,
        factory,
        sleeper=_no_sleep,
        clock=lambda: 1,
    )

    chunks = [chunk async for chunk in runner.stream()]

    assert (malformed in chunks) is forwarded
    assert runner.outcome is not None
    assert runner.outcome.attempts == 3


@pytest.mark.asyncio
async def test_cancellation_during_retry_backoff_is_not_converted_to_error():
    error = _error_chunk(status=429)
    factory, calls = _stream_factory([[error]])
    accumulator = ProviderRoundAccumulator(
        requested_model="model",
        actual_model="model",
        round_number=1,
    )

    async def cancelled_sleep(delay):
        raise asyncio.CancelledError

    runner = ProviderAttemptRunner(
        _request(),
        accumulator,
        factory,
        sleeper=cancelled_sleep,
        clock=lambda: 1,
    )

    with pytest.raises(asyncio.CancelledError):
        _ = [chunk async for chunk in runner.stream()]

    assert calls["count"] == 1
    assert runner.outcome is None
