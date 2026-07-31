"""Regression tests for the tool-support heuristic in stream_agent_loop.

Verifies two critical cases:
  1. local Ollama endpoints must NOT enable native tool schemas by default
     (some models terminate after one token with schemas).
  2. api.deepseek.com must still be treated as tool-capable via the host
     allow-list (_API_HOSTS), so cloud deepseek users keep working.
"""
import pytest
from src.agent_loop import _API_HOSTS, _endpoint_lookup_keys, _is_ollama_openai_compat_url
from src.agent.providers.capabilities import detect_model_capabilities


def _compute_is_api_model(model: str, endpoint_url: str, endpoint_supports=None) -> bool:
    """Exercise the production capability decision without provider I/O."""
    return detect_model_capabilities(
        endpoint_url,
        model,
        endpoint_supports_tools=endpoint_supports,
    ).native_tool_calls


class TestDeepSeekToolSupport:
    # --- local Ollama cases (must NOT get native tool schemas by default) ---

    def test_deepseek_r1_7b_local_ollama_no_tools(self):
        result = _compute_is_api_model(
            "deepseek-r1:7b", "http://localhost:11434/v1"
        )
        assert result is False, (
            "deepseek-r1:7b on Ollama must not enable tool schemas "
            "(Ollama returns HTTP 400 for this model)"
        )

    def test_deepseek_r1_14b_local_no_tools(self):
        assert _compute_is_api_model("deepseek-r1:14b", "http://localhost:11434/v1") is False

    def test_deepseek_r1_70b_local_no_tools(self):
        assert _compute_is_api_model("deepseek-r1:70b", "http://127.0.0.1:11434/v1") is False

    def test_deepseek_r1_via_docker_no_tools(self):
        assert _compute_is_api_model(
            "deepseek-r1:7b", "http://host.docker.internal:11434/v1"
        ) is False

    def test_qwen_local_ollama_defaults_to_fenced_tools(self):
        assert _compute_is_api_model(
            "qwen3.5:4b", "http://localhost:11434/v1"
        ) is False

    def test_gemma_local_ollama_defaults_to_fenced_tools(self):
        assert _compute_is_api_model(
            "gemma4:e4b", "http://host.docker.internal:11434/v1"
        ) is False

    def test_gpt_oss_local_openai_compat_defaults_to_fenced_tools(self):
        assert _compute_is_api_model(
            "gpt-oss-20b", "http://localhost:8000/v1"
        ) is False

    def test_qwen_native_ollama_defaults_to_fenced_tools(self):
        assert _compute_is_api_model(
            "qwen3.5:4b", "http://localhost:11434/api/chat"
        ) is False

    # --- cloud API cases (must still get tool schemas) ---

    def test_deepseek_cloud_api_gets_tools(self):
        result = _compute_is_api_model(
            "deepseek-chat", "https://api.deepseek.com/v1"
        )
        assert result is True, (
            "api.deepseek.com must be treated as tool-capable via _API_HOSTS"
        )

    def test_deepseek_v3_cloud_gets_tools(self):
        assert _compute_is_api_model("deepseek-v3", "https://api.deepseek.com/v1") is True

    def test_deepseek_v2_cloud_gets_tools(self):
        assert _compute_is_api_model("deepseek-v2.5", "https://api.deepseek.com/v1") is True

    # --- endpoint_supports override takes priority ---

    def test_endpoint_supports_true_overrides_blocklist(self):
        """A user who explicitly sets supports_tools=True on their endpoint
        can force tool schemas even for deepseek-r1 (e.g. custom server)."""
        result = _compute_is_api_model(
            "deepseek-r1:7b", "http://localhost:11434/v1", endpoint_supports=True
        )
        assert result is True

    def test_endpoint_supports_true_overrides_ollama_default(self):
        """A user can still explicitly opt a known-good Ollama endpoint into
        native schemas."""
        result = _compute_is_api_model(
            "qwen3.5:4b", "http://localhost:11434/v1", endpoint_supports=True
        )
        assert result is True

    def test_endpoint_supports_true_overrides_native_ollama_default(self):
        result = _compute_is_api_model(
            "qwen3.5:4b", "http://localhost:11434/api/chat", endpoint_supports=True
        )
        assert result is True

    def test_endpoint_supports_true_overrides_gpt_oss_default(self):
        result = _compute_is_api_model(
            "gpt-oss-20b", "http://localhost:8000/v1", endpoint_supports=True
        )
        assert result is True

    def test_endpoint_supports_false_overrides_cloud(self):
        """supports_tools=False on an endpoint gates even cloud APIs."""
        result = _compute_is_api_model(
            "deepseek-chat", "https://api.deepseek.com/v1", endpoint_supports=False
        )
        assert result is False

    # --- other local models unaffected ---

    def test_qwen_local_non_ollama_still_gets_tools(self):
        assert _compute_is_api_model("qwen2.5:14b", "http://localhost:8000/v1") is True

    def test_llama_local_non_ollama_gets_tools_via_host(self):
        assert _compute_is_api_model("llama3.2:3b", "http://localhost:8000/v1") is True


class TestApiHostsContainsDeepSeek:
    def test_api_deepseek_com_in_api_hosts(self):
        assert "api.deepseek.com" in _API_HOSTS

    def test_deepseek_com_in_api_hosts(self):
        assert "deepseek.com" in _API_HOSTS


class TestStructuredCapabilities:
    def test_ollama_fenced_capability_is_explicit(self):
        capabilities = detect_model_capabilities(
            "http://localhost:11434/v1",
            "qwen3.5:4b",
        )
        assert capabilities.native_tool_calls is False
        assert capabilities.fenced_tool_calls is True
        assert capabilities.compact_prompt is True
        assert capabilities.provider_family == "ollama_openai_compat"

    def test_hosted_native_capability_is_explicit(self):
        capabilities = detect_model_capabilities(
            "https://api.openai.com/v1",
            "gpt-5",
        )
        assert capabilities.native_tool_calls is True
        assert capabilities.fenced_tool_calls is False
        assert capabilities.mcp_schemas is True
        assert capabilities.document_streaming_mode == "native_delta"


class TestEndpointLookupKeys:
    def test_chat_completions_url_matches_endpoint_base(self):
        keys = _endpoint_lookup_keys("http://localhost:11434/v1/chat/completions")

        assert "http://localhost:11434/v1" in keys
        assert "http://localhost:11434/v1/" in keys

    def test_native_ollama_chat_url_matches_api_base(self):
        keys = _endpoint_lookup_keys("http://host.docker.internal:11434/api/chat")

        assert "http://host.docker.internal:11434/api" in keys
