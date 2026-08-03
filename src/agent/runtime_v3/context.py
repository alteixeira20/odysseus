from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable


class ContextBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectedContext:
    messages: tuple[dict, ...]
    estimated_tokens: int
    dropped_messages: int


def _tool_call_ids(message: dict) -> tuple[str, ...]:
    calls = message.get("tool_calls") or []
    ids = []
    for call in calls:
        if isinstance(call, dict) and call.get("id"):
            ids.append(str(call["id"]))
    return tuple(ids)


def project_context(messages: Iterable[dict], *, budget_tokens: int,
                    estimate_tokens: Callable[[list[dict]], int]) -> ProjectedContext:
    """Project context without truncating system or latest-user semantics.

    Tool call/result pairs are indivisible. If critical messages alone exceed
    the budget, the function fails instead of silently mutating instructions.
    """
    source = [dict(m) for m in messages]
    if not source:
        return ProjectedContext((), 0, 0)
    critical_indexes = {i for i, m in enumerate(source) if m.get("role") == "system"}
    latest_user = next((i for i in range(len(source) - 1, -1, -1)
                        if source[i].get("role") == "user"), None)
    if latest_user is not None:
        critical_indexes.add(latest_user)

    groups: list[list[int]] = []
    consumed: set[int] = set()
    for i, message in enumerate(source):
        if i in consumed:
            continue
        call_ids = set(_tool_call_ids(message))
        if call_ids:
            group = [i]
            for j in range(i + 1, len(source)):
                candidate = source[j]
                if candidate.get("role") == "tool" and str(candidate.get("tool_call_id") or "") in call_ids:
                    group.append(j)
                    consumed.add(j)
            groups.append(group)
        else:
            groups.append([i])
        consumed.add(i)

    selected = set(critical_indexes)
    for group in groups:
        if selected.intersection(group):
            selected.update(group)
    critical = [source[i] for i in sorted(selected)]
    critical_tokens = estimate_tokens(critical)
    if critical_tokens > budget_tokens:
        raise ContextBudgetExceeded(
            f"critical context requires {critical_tokens} tokens but budget is {budget_tokens}"
        )

    for group in reversed(groups):
        if selected.intersection(group):
            continue
        trial = selected.union(group)
        candidate = [source[i] for i in sorted(trial)]
        if estimate_tokens(candidate) <= budget_tokens:
            selected = trial

    projected = [source[i] for i in sorted(selected)]
    return ProjectedContext(
        messages=tuple(projected),
        estimated_tokens=estimate_tokens(projected),
        dropped_messages=len(source) - len(projected),
    )
