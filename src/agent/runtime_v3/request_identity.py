"""Privacy-preserving semantic identity for durable Agent runs.

Runtime recovery needs to detect accidental reuse of a run id for a different
request, but the durable ledger does not need a copy of prompt text, uploaded
content, authorization headers, or endpoint query credentials to do that. This
module hashes the complete semantic request in memory and persists only the
resulting digest plus bounded/redacted diagnostics.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _stable(value: Any, *, _seen: set[int] | None = None) -> Any:
    """Project arbitrary request values into deterministic JSON material.

    This projection is used only as hash input. It is deliberately broader than
    the persisted snapshot so semantic inputs can affect run identity without
    becoming recoverable plaintext in SQLite. Dataclasses are traversed field by
    field instead of using ``asdict`` so nested runtime objects are never deep-
    copied just to compute identity.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"$bytes_sha256": _sha256_bytes(value), "size": len(value)}
    if isinstance(value, Enum):
        return _stable(value.value, _seen=_seen)

    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return {"$cycle": f"{type(value).__module__}.{type(value).__qualname__}"}
    seen.add(identity)
    try:
        if isinstance(value, Mapping):
            return {
                str(key): _stable(item, _seen=seen)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [_stable(item, _seen=seen) for item in value]
        if isinstance(value, (set, frozenset)):
            items = [_stable(item, _seen=seen) for item in value]
            return sorted(items, key=_canonical)
        if is_dataclass(value):
            return {
                field.name: _stable(getattr(value, field.name), _seen=seen)
                for field in fields(value)
                if not field.name.startswith("_")
            }
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, Mapping):
            public = {
                str(key): item
                for key, item in attributes.items()
                if not str(key).startswith("_") and not callable(item)
            }
            return {
                "$type": f"{type(value).__module__}.{type(value).__qualname__}",
                "fields": _stable(public, _seen=seen),
            }
        return {"$type": f"{type(value).__module__}.{type(value).__qualname__}"}
    finally:
        seen.discard(identity)


def _execution_contract(request) -> dict[str, Any] | None:
    context = getattr(request, "execution_context", None)
    if context is None:
        return None
    root = getattr(context, "execution_root", None)
    authority = getattr(context, "authority_grant", None)
    return {
        "owner_id": getattr(context, "owner_id", None),
        "session_id": getattr(context, "session_id", None),
        "conversation_id": getattr(context, "conversation_id", None),
        "turn_id": getattr(context, "turn_id", None),
        "candidate_id": getattr(context, "candidate_id", None),
        "execution_mode": getattr(getattr(context, "execution_mode", None), "value", None),
        "execution_root": getattr(root, "path", None),
        "workspace_revision": getattr(root, "workspace_revision", None),
        "authority_revision": getattr(authority, "revision", None),
        "tool_catalog_revision": getattr(context, "tool_catalog_revision", None),
        "budgets": _stable(getattr(context, "budgets", None)),
    }


def semantic_request_digest(request) -> str:
    """Hash every material AgentRunRequest input without persisting plaintext."""
    material = {
        "endpoint_url": getattr(request, "endpoint_url", None),
        "model": getattr(request, "model", None),
        "messages": _stable(getattr(request, "messages", None)),
        "session_id": getattr(request, "session_id", None),
        "owner": getattr(request, "owner", None),
        "limits": _stable(getattr(request, "limits", None)),
        "contexts": _stable(getattr(request, "contexts", None)),
        "policy": _stable(getattr(request, "policy", None)),
        "model_options": _stable(getattr(request, "model_options", None)),
        "plan_mode": bool(getattr(request, "plan_mode", False)),
        "approved_plan": _stable(getattr(request, "approved_plan", None)),
        "workload": getattr(request, "workload", None),
        "is_teacher_run": bool(getattr(request, "is_teacher_run", False)),
        "execution_contract": _execution_contract(request),
    }
    return _sha256_bytes(_canonical(material).encode("utf-8"))


def redacted_endpoint(url: str | None) -> str | None:
    """Retain endpoint topology while dropping userinfo, query, and fragment."""
    if not url:
        return None
    try:
        parsed = urlparse(str(url))
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = host if port is None else f"{host}:{port}"
        path = parsed.path or ""
        return urlunparse((parsed.scheme.lower(), netloc, path, "", "", ""))
    except Exception:
        # Never fall back to persisting an unparsed URL that may contain a
        # credential. The semantic digest still binds the original value.
        return None


def _fingerprint_optional(value: Any) -> str | None:
    if value in (None, "", (), [], {}):
        return None
    return _sha256_bytes(_canonical(_stable(value)).encode("utf-8"))


def safe_request_snapshot(request) -> dict[str, Any]:
    """Return the durable, bounded, non-secret request diagnostic snapshot."""
    contexts = getattr(request, "contexts", None)
    model_options = getattr(request, "model_options", None)
    policy = getattr(request, "policy", None)
    execution_context = getattr(request, "execution_context", None)
    authority = getattr(execution_context, "authority_grant", None)
    root = getattr(execution_context, "execution_root", None)

    headers = getattr(model_options, "headers", None) or {}
    fallbacks = getattr(model_options, "fallbacks", None) or ()
    uploaded_files = getattr(contexts, "uploaded_files", None) or ()
    workspace = getattr(contexts, "workspace", None)

    return {
        "semantic_request_sha256": semantic_request_digest(request),
        "session_id": getattr(request, "session_id", None),
        "owner": getattr(request, "owner", None),
        "model": getattr(request, "model", None),
        "endpoint": redacted_endpoint(getattr(request, "endpoint_url", None)),
        "workload": getattr(request, "workload", None),
        "plan_mode": bool(getattr(request, "plan_mode", False)),
        "message_count": len(getattr(request, "messages", None) or ()),
        "header_names": sorted(str(name) for name in headers.keys()),
        "fallback_count": len(fallbacks),
        "workspace_sha256": _fingerprint_optional(workspace),
        "uploaded_file_count": len(uploaded_files),
        "active_document": getattr(contexts, "active_document", None) is not None,
        "active_email": getattr(contexts, "active_email", None) is not None,
        "disabled_tools": sorted(getattr(policy, "disabled_tools", None) or ()),
        "relevant_tools": sorted(getattr(policy, "relevant_tools", None) or ()),
        "forced_tools": sorted(getattr(policy, "forced_tools", None) or ()),
        "execution_mode": getattr(policy, "execution_mode", None),
        "authority_revision": getattr(authority, "revision", None),
        "workspace_revision": getattr(root, "workspace_revision", None),
    }


__all__ = [
    "redacted_endpoint",
    "safe_request_snapshot",
    "semantic_request_digest",
]
