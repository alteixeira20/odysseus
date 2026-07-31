"""Pure classification of provider SSE errors."""

import json
from typing import Any, Dict


def stream_error_details(chunk: str) -> Dict[str, Any]:
    """Parse safe retry fields from a legacy SSE error event."""

    if not isinstance(chunk, str) or not chunk.startswith("event: error"):
        return {}
    try:
        lines = chunk.split("\n")
        data_line = next(
            (line for line in lines if line.startswith("data: ")),
            "",
        )
        if data_line:
            data = json.loads(data_line[6:])
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def is_transient_error(chunk: str) -> bool:
    """Return true for retryable pre-content transport/provider failures."""

    data = stream_error_details(chunk)
    status = data.get("status")
    if (
        status in (429, 500, 502, 503, 504)
        or data.get("negotiation_retry") is True
    ):
        return True
    raw_text = " ".join(
        value
        for value in (
            data.get("text"),
            data.get("raw"),
            (
                data.get("error")
                if isinstance(data.get("error"), str)
                else ""
            ),
        )
        if isinstance(value, str)
    ).lower()
    return any(
        term in raw_text
        for term in (
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway timeout",
            "500 internal",
            "rate limit",
            "connection reset",
            "connection refused",
            "read timeout",
            "connecterror",
            "bad gateway",
        )
    )
