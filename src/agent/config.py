"""Validated, side-effect-free settings snapshot for one agent run."""

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Mapping


def _bounded_int(
    value: Any,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(parsed, maximum))


def _bounded_float(
    value: Any,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(parsed, maximum))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass(frozen=True)
class AgentSettingsSnapshot:
    """All mutable settings read once at the start of an agent request."""

    stream_timeout_seconds: int = 300
    input_token_budget: int = 6000
    input_token_hard_max: int = 200_000
    verifier_enabled: bool = False
    max_tool_calls: int = 0
    skill_max_injected: int = 3
    skill_autosave_min_confidence: float = 0.85

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "AgentSettingsSnapshot":
        return cls(
            stream_timeout_seconds=_bounded_int(
                values.get("agent_stream_timeout_seconds", 300),
                300,
                minimum=1,
                maximum=3600,
            ),
            input_token_budget=_bounded_int(
                values.get("agent_input_token_budget", 6000),
                6000,
                minimum=0,
                maximum=1_000_000,
            ),
            input_token_hard_max=_bounded_int(
                values.get("agent_input_token_hard_max", 200_000),
                200_000,
                minimum=1,
                maximum=1_000_000,
            ),
            verifier_enabled=_as_bool(
                values.get("agent_verifier_subagent", False),
            ),
            max_tool_calls=_bounded_int(
                values.get("agent_max_tool_calls", 0),
                0,
                minimum=0,
                maximum=10_000,
            ),
            skill_max_injected=_bounded_int(
                values.get("skill_max_injected", 3),
                3,
                minimum=0,
                maximum=12,
            ),
            skill_autosave_min_confidence=_bounded_float(
                values.get("skill_autosave_min_confidence", 0.85),
                0.85,
                minimum=0.0,
                maximum=1.0,
            ),
        )

    @classmethod
    def capture(
        cls,
        getter: Callable[[str, Any], Any],
    ) -> "AgentSettingsSnapshot":
        """Read each mutable runtime setting once, falling back independently."""

        defaults = {
            "agent_stream_timeout_seconds": 300,
            "agent_input_token_budget": 6000,
            "agent_input_token_hard_max": 200_000,
            "agent_verifier_subagent": False,
            "agent_max_tool_calls": 0,
            "skill_max_injected": 3,
            "skill_autosave_min_confidence": 0.85,
        }
        values = {}
        for key, default in defaults.items():
            try:
                values[key] = getter(key, default)
            except Exception:
                values[key] = default
        return cls.from_mapping(values)
