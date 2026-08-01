"""Coverage for ContextBudgetManager (src/agent/context/budget.py) — the
typed wrapper over the existing trim_for_context/compute_input_token_budget
functions, and the production fix that re-applies it every round instead
of only once before round 1.
"""

import json

from src.agent.context.budget import ContextBudgetManager, ContextBudgetReport


def _big_user_message(n_chars=40_000):
    return {"role": "user", "content": "x" * n_chars}


def test_soft_budget_disabled_returns_input_unchanged():
    mgr = ContextBudgetManager()
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    trimmed, report = mgr.apply(
        messages,
        endpoint_url="http://x",
        model="m",
        context_length=8192,
        soft_budget=0,
        hard_max=100_000,
        max_output_tokens=1024,
    )
    assert trimmed == messages
    assert report.over_budget is False
    assert report.compacted_messages == 0


def test_under_budget_messages_pass_through_unchanged():
    mgr = ContextBudgetManager()
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    trimmed, report = mgr.apply(
        messages,
        endpoint_url="http://x",
        model="m",
        context_length=8192,
        soft_budget=6000,
        hard_max=100_000,
        max_output_tokens=1024,
    )
    assert trimmed == messages
    assert report.over_budget is False
    assert report.retained_messages == len(messages)


def test_over_budget_messages_get_compacted_with_report():
    mgr = ContextBudgetManager()
    messages = (
        [{"role": "system", "content": "sys"}]
        + [{"role": "user", "content": f"turn {i}"} for i in range(200)]
        + [_big_user_message()]
    )
    trimmed, report = mgr.apply(
        messages,
        endpoint_url="http://x",
        model="m",
        context_length=0,  # unknown window -> conservative default budget
        soft_budget=500,  # small explicit budget forces trimming
        hard_max=100_000,
        max_output_tokens=256,
    )
    assert report.over_budget is True
    assert report.estimated_after <= report.estimated_before
    assert len(trimmed) <= len(messages)
    # The current (last) user turn must always survive.
    assert trimmed[-1]["role"] == "user"


def test_protected_message_survives_aggressive_trim():
    mgr = ContextBudgetManager()
    protected = {"role": "system", "content": "ACTIVE DOCUMENT: important", "_protected": True}
    messages = (
        [{"role": "system", "content": "sys"}, protected]
        + [{"role": "user", "content": f"turn {i}" * 50} for i in range(100)]
        + [{"role": "user", "content": "final question"}]
    )
    trimmed, report = mgr.apply(
        messages,
        endpoint_url="http://x",
        model="m",
        context_length=0,
        soft_budget=800,
        hard_max=100_000,
        max_output_tokens=256,
    )
    assert protected in trimmed
    assert trimmed[-1]["content"] == "final question"


def test_report_is_frozen_and_shaped():
    report = ContextBudgetReport(
        estimated_before=100,
        estimated_after=50,
        reserved_output=512,
        effective_budget=1000,
        compacted_messages=3,
        retained_messages=7,
        over_budget=True,
    )
    assert report.over_budget is True
    try:
        report.over_budget = False  # type: ignore[misc]
        assert False, "ContextBudgetReport must be immutable"
    except AttributeError:
        pass


# ── Production integration: re-applied every round, not just once ────────

def _collect(gen):
    import asyncio

    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def test_budget_is_reapplied_every_round_not_only_once(monkeypatch):
    import src.agent_loop as agent_loop

    calls = {"n": 0}
    apply_calls = []

    async def fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            # A tool-free "length" finish forces the truncation-continuation
            # supervisor to advance to another round, so this test reliably
            # exercises multiple rounds.
            yield f'data: {json.dumps({"delta": "partial " + str(calls["n"])})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "reason": "length"})}\n\n'
            yield "data: [DONE]\n\n"
        else:
            yield f'data: {json.dumps({"delta": "done"})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "reason": "stop"})}\n\n'
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)

    real_apply = agent_loop._CONTEXT_BUDGET_MANAGER.apply

    def spying_apply(messages, **kwargs):
        apply_calls.append(len(messages))
        return real_apply(messages, **kwargs)

    monkeypatch.setattr(agent_loop._CONTEXT_BUDGET_MANAGER, "apply", spying_apply)

    _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "Keep going."}],
            relevant_tools={"bash"},
            _is_teacher_run=True,
        )
    )

    # Truncation-continuation is capped at 4, so rounds 1-4 each hit
    # "length" (calls 1-3 do; call 4 finally streams "stop"), all bounded
    # by _MAX_TRUNCATION_CONTINUATIONS. The budget must have been checked
    # more than once — once at prep time is not enough for a multi-round run.
    assert calls["n"] >= 3
    assert len(apply_calls) >= 3


def test_protected_context_remains_canonical_but_is_sanitized_for_every_provider_round(
    monkeypatch,
):
    import src.agent_loop as agent_loop

    protected = {
        "role": "system",
        "content": "ACTIVE DOCUMENT: retain me across rounds",
        "_protected": True,
        "_runtime_source": "active_document",
    }
    provider_requests = []
    budget_protection = []
    calls = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        provider_requests.append([dict(message) for message in messages])
        yield f'data: {json.dumps({"delta": "partial" if calls["n"] == 1 else "done"})}\n\n'
        reason = "length" if calls["n"] == 1 else "stop"
        yield f'data: {json.dumps({"type": "finish", "reason": reason})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(
        agent_loop,
        "_build_system_prompt",
        lambda messages, *args, **kwargs: ([dict(protected), *messages], []),
    )

    real_apply = agent_loop._CONTEXT_BUDGET_MANAGER.apply

    def spying_apply(messages, **kwargs):
        budget_protection.append(
            any(
                message.get("content") == protected["content"]
                and message.get("_protected") is True
                for message in messages
            )
        )
        return real_apply(messages, **kwargs)

    monkeypatch.setattr(agent_loop._CONTEXT_BUDGET_MANAGER, "apply", spying_apply)

    _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "Inspect and complete the active work."}],
            relevant_tools={"read_file"},
            _is_teacher_run=True,
        )
    )

    assert calls["n"] == 2
    assert len(budget_protection) >= 2
    assert all(budget_protection)
    for request_messages in provider_requests:
        retained = [
            message for message in request_messages
            if message.get("content") == protected["content"]
        ]
        assert len(retained) == 1
        assert all(not key.startswith("_") for key in retained[0])
