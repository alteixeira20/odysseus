"""Pure final-metrics projection for an agent run."""

from typing import Dict, List, Optional

from src.model_context import estimate_tokens


def compute_final_metrics(
    messages: List[Dict],
    full_response: str,
    total_duration: float,
    time_to_first_token,
    context_length: int,
    real_input_tokens: int,
    real_output_tokens: int,
    has_real_usage: bool,
    tool_events: list,
    round_texts: list,
    model: str = "",
    last_round_input_tokens: int = 0,
    request_context_tokens: int = 0,
    prep_timings: Optional[Dict[str, float]] = None,
    backend_gen_tps: float = 0,
    backend_prefill_tps: float = 0,
) -> dict:
    """Compute token counts, throughput, context use, and preparation metrics."""

    if has_real_usage:
        input_tokens = real_input_tokens
        output_tokens = real_output_tokens
    else:
        input_content = ""
        for msg in messages:
            if isinstance(msg.get("content"), str):
                input_content += msg["content"] + "\n"
        input_tokens = len(input_content) // 4
        output_tokens = len(full_response) // 4

    if backend_gen_tps and backend_gen_tps > 0:
        tps = backend_gen_tps
    else:
        tps = output_tokens / total_duration if total_duration > 0 else 0

    if request_context_tokens:
        ctx_tokens = request_context_tokens
    elif last_round_input_tokens:
        ctx_tokens = last_round_input_tokens
    elif has_real_usage:
        ctx_tokens = real_input_tokens
    else:
        ctx_tokens = estimate_tokens(messages)
    ctx_pct = (
        min(round((ctx_tokens / context_length) * 100, 1), 100.0)
        if context_length
        else 0
    )

    metrics = {
        "response_time": round(total_duration, 2),
        "time_to_first_token": (
            round(time_to_first_token, 2) if time_to_first_token else 0
        ),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens_per_second": round(tps, 2),
        "tps_source": (
            "backend"
            if (backend_gen_tps and backend_gen_tps > 0)
            else "computed"
        ),
        "total_tokens": input_tokens + output_tokens,
        "request_context_tokens": ctx_tokens,
        "context_length": context_length,
        "context_percent": ctx_pct,
        "usage_source": "real" if has_real_usage else "estimated",
        "model": model,
    }
    if backend_prefill_tps and backend_prefill_tps > 0:
        metrics["prefill_tps"] = round(backend_prefill_tps, 2)
    if prep_timings:
        prep_total = round(sum(prep_timings.values()), 3)
        metrics["agent_prep_time"] = prep_total
        metrics["agent_model_wait_time"] = round(time_to_first_token or 0, 3)
        metrics["agent_prep_breakdown"] = {
            key: round(value, 3) for key, value in prep_timings.items()
        }
    if tool_events:
        metrics["tool_events"] = tool_events
        metrics["round_texts"] = round_texts
    return metrics
