"""Privacy-preserving semantic identity for durable Agent runs.

Durable recovery must reject accidental reuse of a run id for a semantically
different request. It does not need prompt text, uploaded content, authorization
headers, endpoint credentials, workspace paths, or endpoint paths in plaintext
to do so.

The complete request is projected and hashed in memory. Only the digest plus
bounded non-secret diagnostics are persisted by Runtime V3.
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
    """Project request values to deterministic JSON without deepcopy."""
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


def _parse_endpoint(url: str | None):
    if not url:
        return None
    try:
        parsed = urlparse(str(url))
        if not parsed.scheme or not parsed.hostname:
            return None
        return parsed
    except Exception:
        return None


def redacted_endpoint(url: str | None) -> str | None:
    """Persist only endpoint origin; path/userinfo/query/fragment stay private."""
    parsed = _parse_endpoint(url)
    if parsed is None:
        return None
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = host if port is None else f"{host}:{port}"
    return urlunparse((parsed.scheme.lower(), netloc, "", "", "", ""))


def endpoint_path_fingerprint(url: str | None) -> str | None:
    """Fingerprint endpoint path for diagnostics without retaining its text."""
    parsed = _parse_endpoint(url)
    if parsed is None or not parsed.path:
        return None
    return _sha256_bytes(parsed.path.encode("utf-8", errors="surrogatepass"))


def _fingerprint_optional(value: Any) -> str | None:
    if value in (None, "", (), [], {}):
        return None
    return _sha256_bytes(_canonical(_stable(value)).encode("utf-8"))


def safe_request_snapshot(request) -> dict[str, Any]:
    """Return bounded durable diagnostics plus the semantic request digest."""
    contexts = getattr(request, "contexts", None)
    model_options = getattr(request, "model_options", None)
    policy = getattr(request, "policy", None)
    execution_context = getattr(request, "execution_context", None)
    authority = getattr(execution_context, "authority_grant", None)
    root = getattr(execution_context, "execution_root", None)
    endpoint_url = getattr(request, "endpoint_url", None)

    headers = getattr(model_options, "headers", None) or {}
    fallbacks = getattr(model_options, "fallbacks", None) or ()
    uploaded_files = getattr(contexts, "uploaded_files", None) or ()
    workspace = getattr(contexts, "workspace", None)

    return {
        "semantic_request_sha256": semantic_request_digest(request),
        "session_id": getattr(request, "session_id", None),
        "owner": getattr(request, "owner", None),
        "model": getattr(request, "model", None),
        "endpoint_origin": redacted_endpoint(endpoint_url),
        "endpoint_path_sha256": endpoint_path_fingerprint(endpoint_url),
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
        "tool_catalog_revision": getattr(execution_context, "tool_catalog_revision", None),
    }


__all__ = [
    "endpoint_path_fingerprint",
    "redacted_endpoint",
    "safe_request_snapshot",
    "semantic_request_digest",
]
