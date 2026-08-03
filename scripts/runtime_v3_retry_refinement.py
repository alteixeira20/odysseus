from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one anchor, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/agent/runtime_v3/ledger.py",
    '''                if row is not None:
                    if row["request_sha256"] != request_hash or row["tool_name"] != tool_name:
                        raise RuntimeError("idempotency key reused for a different effect")
                    status = EffectStatus(row["status"])
                    cached = json.loads(row["result_json"]) if row["result_json"] else None
                    return EffectLease(row["effect_id"], False, status, cached, "duplicate")
''',
    '''                if row is not None:
                    if row["request_sha256"] != request_hash or row["tool_name"] != tool_name:
                        raise RuntimeError("idempotency key reused for a different effect")
                    status = EffectStatus(row["status"])
                    stored_policy = RetryPolicy(row["retry_policy"])
                    if (
                        status is EffectStatus.FAILED
                        and stored_policy is RetryPolicy.SAFE
                        and retry_policy is RetryPolicy.SAFE
                    ):
                        db.execute(
                            """UPDATE agent_effects SET status=?,result_json=NULL,error_json=NULL,
                               started_at=?,finished_at=NULL,attempt=attempt+1,revision=revision+1
                               WHERE effect_id=?""",
                            (EffectStatus.STARTED.value, now, row["effect_id"]),
                        )
                        return EffectLease(
                            row["effect_id"], True, EffectStatus.STARTED, None,
                            "safe_retry_after_proven_failure",
                        )
                    cached = json.loads(row["result_json"]) if row["result_json"] else None
                    return EffectLease(row["effect_id"], False, status, cached, "duplicate")
''',
)

replace_once(
    "src/agent/runtime_v3/effect_bridge.py",
    '''    *,
    ledger: DurableRunLedger | None = None,
) -> DurableEffectHandle:
''',
    '''    *,
    ledger: DurableRunLedger | None = None,
    idempotency_scope: str | None = None,
) -> DurableEffectHandle:
''',
)
replace_once(
    "src/agent/runtime_v3/effect_bridge.py",
    '''        "tool_contract_revision": call.tool_contract_revision,
    }
''',
    '''        "tool_contract_revision": call.tool_contract_revision,
        "idempotency_scope": idempotency_scope or "default",
    }
''',
)
replace_once(
    "src/agent/runtime_v3/effect_bridge.py",
    '''        retry_policy=retry_policy,
        idempotency_key=f"{call.candidate_id}:{call.call_id}",
    )
''',
    '''        retry_policy=retry_policy,
        idempotency_key=(
            f"{call.candidate_id}:{call.call_id}:{idempotency_scope}"
            if idempotency_scope
            else f"{call.candidate_id}:{call.call_id}"
        ),
    )
''',
)

replace_once(
    "src/agent/runtime_v3/executor_bridge.py",
    '''        handle = begin_tool_effect(call, context, effects)
''',
    '''        approval_scope = None
        if approval_id:
            import hashlib

            approval_scope = "approval-" + hashlib.sha256(
                str(approval_id).encode("utf-8")
            ).hexdigest()[:24]
        handle = begin_tool_effect(
            call,
            context,
            effects,
            idempotency_scope=approval_scope,
        )
''',
)

print("Runtime V3 retry and approval idempotency refinements applied")
