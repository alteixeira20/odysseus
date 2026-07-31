"""Typed context-budget contract over the existing deterministic trimmer.

Root cause context: ``src/agent_loop.py`` already computes an adaptive
input-token budget (``src/context_budget.py``) and deterministically trims
messages to fit it (``src/context_compactor.trim_for_context`` — protects
the leading system prompt, any ``_protected`` message such as an active
document, and the tail of the conversation including the current user
turn) — but it only ever did this **once**, immediately before round 1 of
the agent loop. A run's ``messages`` list keeps growing every round after
that (assistant turns, tool calls, tool results), for up to
``AgentLimits.max_rounds`` (default 50) rounds, with no budget check
before any of rounds 2..N's provider calls. A long multi-step session —
exactly the "recovering from long-context... failures" scenario this
foundation pass targets — could silently exceed the model's context
window mid-run despite the budget machinery existing.

``ContextBudgetManager`` is a thin typed wrapper around the existing,
already-tested trim/budget functions (deliberately not a reimplementation
— see specs/agent-runtime-v2-progress.md for why deterministic compaction
reuses this code rather than introducing a second compactor). The
production fix this module enables is calling ``.apply()`` from
``src/agent_loop.py`` at the top of *every* round, not only once before
the loop.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextBudgetReport:
    """Counts/estimates only — never message content (see module docstring
    of src/agent/context/budget.py for why: this is safe to log verbatim).
    """

    estimated_before: int
    estimated_after: int
    reserved_output: int
    effective_budget: int
    compacted_messages: int
    retained_messages: int
    over_budget: bool


class ContextBudgetManager:
    """Applies the adaptive input-token budget to one round's messages."""

    def apply(
        self,
        messages: list[dict],
        *,
        endpoint_url: str,
        model: str,
        context_length: int,
        soft_budget: int,
        hard_max: int,
        max_output_tokens: int,
    ) -> tuple[list[dict], ContextBudgetReport]:
        """Trim ``messages`` to the effective budget; return (trimmed, report).

        ``soft_budget <= 0`` disables budgeting entirely (matches the
        existing settings contract — 0 means "don't soft-trim"), returning
        the input unchanged with an over_budget=False report.
        """
        from src.context_budget import budget_is_explicit, compute_input_token_budget
        from src.context_compactor import trim_for_context
        from src.model_context import budget_context_for_model, estimate_tokens

        before = estimate_tokens(messages)

        if soft_budget <= 0:
            return messages, ContextBudgetReport(
                estimated_before=before,
                estimated_after=before,
                reserved_output=0,
                effective_budget=0,
                compacted_messages=0,
                retained_messages=len(messages),
                over_budget=False,
            )

        reserve_tokens = min(max(max_output_tokens or 1024, 512), 2048)
        explicit = budget_is_explicit(soft_budget)
        # Scale only off a window actually discovered for this model
        # (falls back to the passed-in context_length when undiscovered),
        # matching the pre-existing single-shot call this replaces.
        ctx_for_budget = budget_context_for_model(
            endpoint_url, model, fallback=context_length
        )
        effective_budget = compute_input_token_budget(
            soft_budget, ctx_for_budget, explicit, hard_max=hard_max
        )

        trimmed = trim_for_context(messages, effective_budget, reserve_tokens=reserve_tokens)
        after = estimate_tokens(trimmed)

        report = ContextBudgetReport(
            estimated_before=before,
            estimated_after=after,
            reserved_output=reserve_tokens,
            effective_budget=effective_budget,
            compacted_messages=max(0, len(messages) - len(trimmed)),
            retained_messages=len(trimmed),
            over_budget=before > effective_budget,
        )
        return trimmed, report
