from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one anchor, found {count}: {old[:160]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_in_fallback(old: str, new: str) -> None:
    target = Path("src/llm_core.py")
    text = target.read_text(encoding="utf-8")
    marker = "async def stream_llm_with_fallback("
    if text.count(marker) != 1:
        raise RuntimeError("stream_llm_with_fallback definition changed")
    prefix, body = text.split(marker, 1)
    count = body.count(old)
    if count != 1:
        raise RuntimeError(f"fallback body: expected one anchor, found {count}: {old[:160]!r}")
    target.write_text(prefix + marker + body.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/llm_core.py",
    "from src.model_context import get_context_length, DEFAULT_CONTEXT, is_local_endpoint\n",
    "from src.model_context import get_context_length, DEFAULT_CONTEXT, is_local_endpoint\n"
    "from src.agent.runtime_v3.context import ContextBudgetExceeded\n"
    "from src.agent.runtime_v3.routing import (\n"
    "    filter_fallback_candidates,\n"
    "    project_messages_for_candidate,\n"
    ")\n",
)
replace_in_fallback(
    '''    cands = _dedupe_candidates(candidates)
    if not cands:
''',
    '''    fallback_plan = filter_fallback_candidates(_dedupe_candidates(candidates))
    cands = list(fallback_plan.accepted)
    for rejected in fallback_plan.rejected:
        logger.warning(
            "[fallback-policy] rejected candidate index=%s model=%s reason=%s policy=%s trust=%s",
            rejected.index,
            rejected.model,
            rejected.reason,
            fallback_plan.policy.value,
            dict(rejected.trust_domain),
        )
    if not cands:
''',
)
replace_in_fallback(
    '''        pending_metadata = []
        candidate_id = secrets.token_urlsafe(18)
        async for chunk in stream_llm(url, model, messages, headers=headers, **kwargs):
''',
    '''        pending_metadata = []
        candidate_id = secrets.token_urlsafe(18)
        try:
            projection = project_messages_for_candidate(
                messages,
                endpoint_url=url,
                model=model,
                max_output_tokens=int(kwargs.get("max_tokens") or 0),
                tools=kwargs.get("tools"),
            )
            candidate_messages = list(projection.messages)
            logger.info(
                "[fallback-context] candidate=%s index=%s context=%s input_budget=%s estimated=%s dropped=%s",
                model,
                i,
                projection.context_window,
                projection.input_budget,
                projection.estimated_tokens,
                projection.dropped_messages,
            )
        except ContextBudgetExceeded as exc:
            context_error = (
                "event: error\\n"
                + "data: "
                + json.dumps(
                    {
                        "error": "Candidate context cannot preserve critical instructions",
                        "status": 413,
                        "error_kind": "context_budget",
                        "model": model,
                    }
                )
                + "\\n\\n"
            )
            logger.warning(
                "[fallback-context] rejected candidate index=%s model=%s: %s",
                i,
                model,
                exc,
            )
            if not is_last:
                last_error = context_error
                continue
            yield context_error
            return
        async for chunk in stream_llm(url, model, candidate_messages, headers=headers, **kwargs):
''',
)

env = Path(".env.example")
text = env.read_text(encoding="utf-8")
block = '''
# Model fallback trust boundary. Default: only another model on the exact same
# normalized endpoint may receive the prompt. Cross-provider fallback requires
# the separate explicit opt-in below.
ODYSSEUS_AGENT_FALLBACK_POLICY=same_endpoint
ODYSSEUS_AGENT_ALLOW_CROSS_PROVIDER_FALLBACK=0
'''
if "ODYSSEUS_AGENT_FALLBACK_POLICY" not in text:
    env.write_text(text.rstrip() + "\n" + block, encoding="utf-8")

print("Runtime V3 context/routing integration applied")
