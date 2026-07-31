"""Per-run repeated-read protection.

Root cause context: nothing today notices when a model re-reads the exact
same file range, re-runs the same grep, or re-lists the same directory
twice in one run with nothing having changed on disk in between — each
call just re-executes and re-injects the same bytes into the growing
message history, competing with the context budget
(src/agent/context/budget.py) for space it didn't need to spend.

``ObservationLedger`` is the standalone primitive for detecting that
case: a per-run (never global/shared — see test coverage) record of
"canonical tool + canonical key + range" observations, keyed on stable
inputs including a file's mtime/size where cheap so a genuine re-read
after the file changed is never suppressed. It does not block a repeated
read — ``note()`` returns a short warning string pointing back at the
prior observation, which the caller decides what to do with (typically:
prepend it to the tool result rather than the caller silently doing
nothing with a legitimate re-read).

Scope of this pass: this module is standalone and independently tested.
Wiring it into the live tool-dispatch path (src/tool_execution.py's
``execute_tool_block``) is deferred to the typed per-run execution
context (``AgentExecutionContext.observation_ledger`` in the cross-cutting
requirements of specs/agent-runtime-v2-progress.md) — that's the natural
place a genuinely *per-run* instance is constructed and threaded through,
and building that plumbing prematurely here would duplicate it. See the
progress doc for status.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Observation:
    tool: str
    key: str
    range_key: str
    identity: str
    observed_at: float


def _range_key(range_: Any) -> str:
    if range_ is None:
        return ""
    if isinstance(range_, (tuple, list)):
        return "-".join(str(p) for p in range_)
    return str(range_)


class ObservationLedger:
    """Tracks (tool, key, range) observations for exactly one run.

    Construct a fresh instance per run — never reuse one across runs or
    share it between concurrent sessions (see
    tests/test_observation_ledger.py for the isolation contract this
    depends on: it is just a dict, with no ambient/global state, so
    per-run isolation is guaranteed by construction as long as callers
    don't share the instance).
    """

    def __init__(self):
        self._seen: dict[tuple[str, str, str], Observation] = {}

    def note(
        self,
        *,
        tool: str,
        key: str,
        range_: Any = None,
        identity: Optional[str] = None,
    ) -> Optional[str]:
        """Record an observation; return a warning if it exactly repeats one.

        ``identity`` should capture anything that legitimately changes the
        answer (e.g. a file's ``(mtime, size)``) — when it differs from the
        prior observation for the same (tool, key, range), this is treated
        as a genuine re-read, not a repeat, and no warning is returned.
        """
        range_k = _range_key(range_)
        identity_k = "" if identity is None else str(identity)
        cache_key = (tool, key, range_k)
        prior = self._seen.get(cache_key)
        observation = Observation(
            tool=tool,
            key=key,
            range_key=range_k,
            identity=identity_k,
            observed_at=time.time(),
        )
        self._seen[cache_key] = observation

        if prior is not None and prior.identity == identity_k:
            age_s = observation.observed_at - prior.observed_at
            range_note = f" (range {range_k})" if range_k else ""
            return (
                f"Note: this is the exact same {tool} read of {key!r}{range_note} "
                f"as {age_s:.1f}s ago, and nothing has changed since — the "
                "result is identical. Consider using what you already have "
                "instead of reading it again."
            )
        return None

    def __len__(self) -> int:
        return len(self._seen)
