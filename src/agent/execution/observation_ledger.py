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

The live loop constructs one ledger for each run and threads it through the
typed tool-batch request.  Results are never suppressed: an exact repeat gets
a compact note in the model-visible result, while changed content is treated
as a fresh observation.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional


OBSERVED_READ_TOOLS = frozenset({"read_file", "grep", "glob", "ls"})


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

    def note_tool_result(
        self,
        *,
        tool: str,
        content: str,
        result: dict[str, Any],
    ) -> Optional[str]:
        """Record one successful repository observation from live dispatch.

        Canonical request arguments define the observation key and page.  A
        digest of the returned structured result is the identity, so a file or
        search result that changed between calls is never labelled a repeat.
        """

        if tool not in OBSERVED_READ_TOOLS or result.get("error"):
            return None
        args = _tool_arguments(content)
        key, range_ = _observation_key(tool, args, content)
        if not key:
            return None
        identity_payload = {
            key: value
            for key, value in result.items()
            if key not in {"observation_notice", "repeated_observation"}
        }
        encoded = json.dumps(
            identity_payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        ).encode("utf-8")
        identity = hashlib.sha256(encoded).hexdigest()
        return self.note(
            tool=tool,
            key=key,
            range_=range_,
            identity=identity,
        )

    def __len__(self) -> int:
        return len(self._seen)


def _tool_arguments(content: str) -> dict[str, Any]:
    raw = str(content or "").strip()
    if not raw.startswith("{"):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _observation_key(
    tool: str,
    args: dict[str, Any],
    content: str,
) -> tuple[str, Any]:
    if tool == "read_file":
        key = str(args.get("path") or content or "").strip()
        return key, (args.get("offset") or 0, args.get("limit") or 0)
    if tool == "grep":
        key = " | ".join(
            str(args.get(name) or "")
            for name in ("pattern", "path", "glob", "ignore_case")
        ).strip(" |")
        return key or str(content or "").strip(), (
            args.get("offset") or 0,
            args.get("max_results") or 200,
        )
    if tool == "glob":
        key = " | ".join(
            str(args.get(name) or "") for name in ("pattern", "path")
        ).strip(" |")
        return key or str(content or "").strip(), (
            args.get("offset") or 0,
            args.get("max_results") or 200,
        )
    key = str(args.get("path") or content or ".").strip()
    return key, (args.get("offset") or 0, args.get("max_results") or 200)
