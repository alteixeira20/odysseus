"""Structured loop-safety primitives and per-run stall state."""

from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Optional

from src.agent.contracts import SupervisorAction, SupervisorDecision


def detect_runaway_call(
    call_frequency: Mapping[str, int],
    threshold: int = 15,
) -> Optional[str]:
    """Return the tool name for an identical call repeated past the threshold.

    Frequencies are keyed by ``"{tool_name}:{argument_signature}"``. Distinct
    calls to the same tool are therefore not mistaken for a runaway batch.
    """

    signature = next(
        (
            signature
            for signature, count in call_frequency.items()
            if count >= threshold
        ),
        None,
    )
    return signature.split(":", 1)[0] if signature else None


@dataclass
class StallSupervisor:
    recent_signatures: deque[str] = field(
        default_factory=lambda: deque(maxlen=6)
    )
    call_frequency: Counter[str] = field(default_factory=Counter)
    stuck_rounds: int = 0
    stuck_threshold: int = 4
    runaway_threshold: int = 15

    def observe(
        self,
        tool_blocks,
        *,
        real_text: str,
    ) -> Optional[SupervisorDecision]:
        signature = "|".join(
            sorted(
                f"{block.tool_type}:"
                f"{(block.content or '').strip()[:120]}"
                for block in tool_blocks
            )
        )
        repeated = signature in self.recent_signatures
        self.recent_signatures.append(signature)
        for block in tool_blocks:
            call_signature = (
                f"{block.tool_type}:"
                f"{(block.content or '').strip()[:120]}"
            )
            self.call_frequency[call_signature] += 1
        if repeated and not str(real_text or "").strip():
            self.stuck_rounds += 1
        else:
            self.stuck_rounds = 0
        runaway = detect_runaway_call(
            self.call_frequency,
            threshold=self.runaway_threshold,
        )
        if self.stuck_rounds < self.stuck_threshold and not runaway:
            return None
        detail = (
            f"calling {runaway} with identical arguments over and over"
            if runaway
            else "repeating the same tool calls without new progress"
        )
        return SupervisorDecision(
            action=SupervisorAction.FORCE_ANSWER,
            reason="loop_breaker_stall",
            metadata={
                "detail": detail,
                "signature": signature,
                "runaway_tool": runaway,
                "stuck_rounds": self.stuck_rounds,
            },
        )
