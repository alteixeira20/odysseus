from __future__ import annotations

from dataclasses import dataclass
import math
import os


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class RuntimeV3Limits:
    max_rounds: int = 50
    max_tool_calls: int = 256
    max_provider_calls: int = 128
    wall_clock_seconds: float = 3600.0
    idle_seconds: float = 300.0
    max_event_bytes: int = 1_048_576
    max_replay_events: int = 8192
    max_replay_bytes: int = 64 * 1024 * 1024
    max_mcp_output_bytes: int = 1_048_576
    mcp_call_timeout_seconds: float = 120.0

    def normalize_request(self, *, max_rounds: int, max_tool_calls: int) -> "RuntimeV3Limits":
        rounds = max(1, min(self.max_rounds, int(max_rounds or self.max_rounds)))
        requested_tools = int(max_tool_calls or self.max_tool_calls)
        tools = max(1, min(self.max_tool_calls, requested_tools))
        providers = max(1, min(self.max_provider_calls, rounds * 3))
        return RuntimeV3Limits(
            max_rounds=rounds,
            max_tool_calls=tools,
            max_provider_calls=providers,
            wall_clock_seconds=self.wall_clock_seconds,
            idle_seconds=self.idle_seconds,
            max_event_bytes=self.max_event_bytes,
            max_replay_events=self.max_replay_events,
            max_replay_bytes=self.max_replay_bytes,
            max_mcp_output_bytes=self.max_mcp_output_bytes,
            mcp_call_timeout_seconds=self.mcp_call_timeout_seconds,
        )


def load_runtime_v3_limits() -> RuntimeV3Limits:
    return RuntimeV3Limits(
        max_rounds=_bounded_int("ODYSSEUS_AGENT_MAX_ROUNDS", 200, 1, 1000),
        max_tool_calls=_bounded_int("ODYSSEUS_AGENT_MAX_TOOL_CALLS", 256, 1, 1000),
        max_provider_calls=_bounded_int("ODYSSEUS_AGENT_MAX_PROVIDER_CALLS", 512, 1, 2000),
        wall_clock_seconds=_bounded_float("ODYSSEUS_AGENT_MAX_RUN_SECONDS", 3600.0, 1.0, 86400.0),
        idle_seconds=_bounded_float("ODYSSEUS_AGENT_RUN_IDLE_SECONDS", 300.0, 1.0, 3600.0),
        max_event_bytes=_bounded_int("ODYSSEUS_AGENT_MAX_EVENT_BYTES", 1_048_576, 4096, 16_777_216),
        max_replay_events=_bounded_int("ODYSSEUS_AGENT_MAX_REPLAY_EVENTS", 8192, 128, 100_000),
        max_replay_bytes=_bounded_int(
            "ODYSSEUS_AGENT_MAX_REPLAY_BYTES",
            64 * 1024 * 1024,
            1_048_576,
            2 * 1024 * 1024 * 1024,
        ),
        max_mcp_output_bytes=_bounded_int("ODYSSEUS_MCP_MAX_OUTPUT_BYTES", 1_048_576, 4096, 16_777_216),
        mcp_call_timeout_seconds=_bounded_float("ODYSSEUS_MCP_CALL_TIMEOUT_SECONDS", 120.0, 1.0, 3600.0),
    )
