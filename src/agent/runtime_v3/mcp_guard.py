from __future__ import annotations

import asyncio
import base64
import os
import re
from typing import Any, Awaitable, Callable, Mapping

from .config import load_runtime_v3_limits


_SAFE_BASE_ENV = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL", "TZ", "VIRTUAL_ENV",
)
_SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|COOKIE|AUTH)", re.I)


def build_mcp_child_env(explicit: Mapping[str, str] | None = None) -> dict[str, str]:
    """Create a minimal child environment; never inherit application secrets."""
    child = {name: os.environ[name] for name in _SAFE_BASE_ENV if name in os.environ}
    for key, value in (explicit or {}).items():
        key = str(key)
        if not key or "\x00" in key or "=" in key:
            raise ValueError(f"invalid environment variable name: {key!r}")
        child[key] = str(value)
    return child


def redact_environment(env: Mapping[str, str]) -> dict[str, str]:
    return {k: ("<redacted>" if _SECRET_NAME.search(k) else str(v)) for k, v in env.items()}


def validate_pinned_npx(args: list[str]) -> None:
    for arg in args:
        if arg in {"-y", "--yes"}:
            raise ValueError("runtime package installation is forbidden for MCP servers")
        if arg.endswith("@latest") or arg == "latest":
            raise ValueError("MCP packages must be pinned to an exact version")


async def guarded_call(call: Callable[[], Awaitable[Any]], *, timeout_seconds: float | None = None) -> Any:
    timeout = timeout_seconds or load_runtime_v3_limits().mcp_call_timeout_seconds
    return await asyncio.wait_for(call(), timeout=timeout)


def bounded_mcp_result(result: Any, *, max_bytes: int | None = None) -> dict[str, Any]:
    limit = max_bytes or load_runtime_v3_limits().max_mcp_output_bytes
    remaining = limit
    output_parts: list[str] = []
    images: list[dict[str, str]] = []
    truncated = False

    def take_text(value: Any) -> None:
        nonlocal remaining, truncated
        data = str(value).encode("utf-8", errors="replace")
        if len(data) > remaining:
            data = data[:remaining]
            truncated = True
        output_parts.append(data.decode("utf-8", errors="replace"))
        remaining -= len(data)

    for content in getattr(result, "content", ()) or ():
        if remaining <= 0:
            truncated = True
            break
        if hasattr(content, "text"):
            take_text(content.text)
        elif getattr(content, "type", "") == "image" and hasattr(content, "data"):
            raw = str(content.data)
            try:
                decoded_size = len(base64.b64decode(raw, validate=False))
            except Exception:
                decoded_size = len(raw.encode("utf-8", errors="replace"))
            if decoded_size > remaining:
                truncated = True
                take_text(f"[Image omitted: {decoded_size} bytes exceeds remaining MCP output budget]")
            else:
                images.append({"data": raw, "mimeType": getattr(content, "mimeType", "image/png")})
                remaining -= decoded_size
                take_text(f"[Image captured ({decoded_size} bytes)]")
        elif hasattr(content, "data"):
            take_text(content.data)

    output = "\n".join(output_parts)
    if truncated:
        output += "\n[truncated by MCP output limit]"
    is_error = bool(getattr(result, "isError", False))
    payload: dict[str, Any] = {
        "stdout": "" if is_error else output,
        "stderr": output if is_error else "",
        "exit_code": 1 if is_error else 0,
        "truncated": truncated,
    }
    if images:
        payload["images"] = images
    return payload
