"""One structured capability decision for each model/provider pairing."""

from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

from src.llm_core import _is_ollama_native_url


API_HOSTS = frozenset(
    [
        "api.openai.com",
        "api.anthropic.com",
        "openrouter.ai",
        "api.groq.com",
        "api.mistral.ai",
        "api.cohere.com",
        "api.deepseek.com",
        "deepseek.com",
        "api.together.xyz",
        "api.fireworks.ai",
        "api.perplexity.ai",
        "api.x.ai",
        "ollama.com",
        "api.venice.ai",
        "api.kimi.com",
        "api.githubcopilot.com",
    ]
)

_NATIVE_TOOL_MODEL_MARKERS = (
    "gpt-4",
    "gpt-5",
    "gpt-o",
    "claude",
    "gemini",
    "gemma",
    "qwen3",
    "qwen2.5",
    "mixtral",
    "mistral",
    "llama-3.1",
    "llama-3.2",
    "llama-3.3",
    "llama-4",
    "llama3.1",
    "llama3.2",
    "llama3.3",
    "llama4",
    "minimax",
    "kimi",
    "yi-",
    "phi-3",
    "phi-4",
    "command-r",
    "glm-4",
    "internlm",
    "hermes",
    "deepseek-v",
    "deepseek-chat",
)
_NO_NATIVE_TOOL_MODEL_MARKERS = ("deepseek-r1", "gpt-oss")
_REASONING_MODEL_MARKERS = (
    "deepseek-r1",
    "reasoning",
    "qwen3",
    "o1",
    "o3",
    "o4",
)


@dataclass(frozen=True)
class ModelCapabilities:
    native_tool_calls: bool
    fenced_tool_calls: bool
    mcp_schemas: bool
    compact_prompt: bool
    reasoning_content: bool
    document_streaming_mode: str
    provider_family: str
    endpoint_supports_tools: Optional[bool] = None


def is_ollama_openai_compat_url(endpoint_url: str) -> bool:
    """Return true for local Ollama's OpenAI-compatible ``/v1`` surface."""

    try:
        parsed = urlparse(endpoint_url or "")
    except Exception:
        return False
    path = (parsed.path or "").rstrip("/")
    return parsed.port == 11434 and (
        path == "/v1" or path.startswith("/v1/")
    )


def is_local_openai_compat_url(endpoint_url: str) -> bool:
    try:
        parsed = urlparse(endpoint_url or "")
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    if not (path == "/v1" or path.startswith("/v1/")):
        return False
    if host in {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "host.docker.internal",
    }:
        return True
    if host.startswith("192.168.") or host.startswith("10."):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            return 16 <= second <= 31
        except Exception:
            return False
    return False


def endpoint_lookup_keys(endpoint_url: str) -> List[str]:
    """Return candidate persisted base URLs for a runtime chat URL."""

    raw = (endpoint_url or "").strip()
    keys: List[str] = []

    def add(value: str) -> None:
        value = (value or "").strip()
        if value and value not in keys:
            keys.append(value)
        trimmed = value.rstrip("/")
        if trimmed and trimmed not in keys:
            keys.append(trimmed)
        if trimmed and f"{trimmed}/" not in keys:
            keys.append(f"{trimmed}/")

    add(raw)
    try:
        from src.endpoint_resolver import normalize_base

        add(normalize_base(raw))
    except Exception:
        pass
    return keys


def detect_model_capabilities(
    endpoint_url: str,
    model: str,
    *,
    endpoint_supports_tools: Optional[bool] = None,
) -> ModelCapabilities:
    """Resolve provider behavior once from endpoint evidence and model hints."""

    endpoint_url = endpoint_url or ""
    model_lower = (model or "").lower()
    ollama_native = _is_ollama_native_url(endpoint_url)
    ollama_openai_compat = is_ollama_openai_compat_url(endpoint_url)
    model_supports_tools = any(
        marker in model_lower for marker in _NATIVE_TOOL_MODEL_MARKERS
    )
    model_rejects_tools = any(
        marker in model_lower for marker in _NO_NATIVE_TOOL_MODEL_MARKERS
    )

    if endpoint_supports_tools is True:
        native_tools = True
    elif (
        endpoint_supports_tools is False
        or model_rejects_tools
        or ollama_native
        or ollama_openai_compat
    ):
        native_tools = False
    else:
        native_tools = (
            any(host in endpoint_url for host in API_HOSTS)
            or model_supports_tools
        )

    if ollama_native:
        provider_family = "ollama_native"
    elif ollama_openai_compat:
        provider_family = "ollama_openai_compat"
    elif any(host in endpoint_url for host in API_HOSTS):
        provider_family = "hosted_openai_compatible"
    elif is_local_openai_compat_url(endpoint_url):
        provider_family = "local_openai_compatible"
    else:
        provider_family = "unknown"

    odysseus_qwen = model_lower.startswith("odysseus-qwen3")
    if odysseus_qwen:
        document_streaming_mode = "odysseus_qwen"
    elif native_tools:
        document_streaming_mode = "native_delta"
    else:
        document_streaming_mode = "fenced"

    return ModelCapabilities(
        native_tool_calls=native_tools,
        fenced_tool_calls=not native_tools,
        mcp_schemas=native_tools,
        compact_prompt=(
            native_tools or ollama_native or ollama_openai_compat
        ),
        reasoning_content=any(
            marker in model_lower for marker in _REASONING_MODEL_MARKERS
        ),
        document_streaming_mode=document_streaming_mode,
        provider_family=provider_family,
        endpoint_supports_tools=endpoint_supports_tools,
    )
