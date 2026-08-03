from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse, urlunparse

from src.model_context import estimate_tokens, get_context_length, is_local_endpoint

from .context import ContextBudgetExceeded, ProjectedContext, project_context


class FallbackPolicy(str, Enum):
    DISABLED = "disabled"
    SAME_ENDPOINT = "same_endpoint"
    SAME_TRUST_DOMAIN = "same_trust_domain"
    EXPLICIT_CROSS_PROVIDER = "explicit_cross_provider"


@dataclass(frozen=True)
class TrustDomain:
    locality: str
    provider_family: str
    host: str
    endpoint_key: str

    def public_dict(self) -> dict[str, str]:
        # Never expose paths, query strings, headers, or credentials in routing
        # diagnostics. Host is intentionally hashed into a stable category key.
        import hashlib

        return {
            "locality": self.locality,
            "provider_family": self.provider_family,
            "host_fingerprint": hashlib.sha256(self.host.encode("utf-8")).hexdigest()[:16],
        }


@dataclass(frozen=True)
class RejectedCandidate:
    index: int
    model: str
    reason: str
    trust_domain: Mapping[str, str]


@dataclass(frozen=True)
class FallbackPlan:
    accepted: tuple[tuple[str, str, Mapping[str, str] | None], ...]
    rejected: tuple[RejectedCandidate, ...]
    policy: FallbackPolicy


@dataclass(frozen=True)
class CandidateProjection:
    messages: tuple[dict, ...]
    context_window: int
    input_budget: int
    estimated_tokens: int
    dropped_messages: int


_PROVIDER_HOSTS = {
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
    "api.mistral.ai": "mistral",
    "api.groq.com": "groq",
    "openrouter.ai": "openrouter",
    "api.deepseek.com": "deepseek",
    "api.x.ai": "xai",
    "generativelanguage.googleapis.com": "google",
    "api.moonshot.ai": "moonshot",
}


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_fallback_policy() -> FallbackPolicy:
    if _truthy("ODYSSEUS_AGENT_ALLOW_CROSS_PROVIDER_FALLBACK", False):
        return FallbackPolicy.EXPLICIT_CROSS_PROVIDER
    raw = os.getenv("ODYSSEUS_AGENT_FALLBACK_POLICY", FallbackPolicy.SAME_ENDPOINT.value)
    try:
        policy = FallbackPolicy(str(raw).strip().lower())
    except ValueError:
        policy = FallbackPolicy.SAME_ENDPOINT
    if policy is FallbackPolicy.EXPLICIT_CROSS_PROVIDER:
        # The permissive policy requires the dedicated, conspicuous opt-in.
        return FallbackPolicy.SAME_ENDPOINT
    return policy


def _normalize_endpoint_key(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host if not port or default_port else f"{host}:{port}"
    path = (parsed.path or "").rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/responses", "/v1/messages"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return urlunparse((scheme, netloc, path, "", "", ""))


def _provider_family(url: str) -> str:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    if host in _PROVIDER_HOSTS:
        return _PROVIDER_HOSTS[host]
    if host.endswith(".openai.azure.com"):
        return "azure_openai"
    path = (parsed.path or "").lower()
    if "anthropic" in host or path.endswith("/v1/messages"):
        return "anthropic"
    if "ollama" in host or parsed.port == 11434 or path.endswith("/api/chat"):
        return "ollama"
    if is_local_endpoint(url):
        return "local_openai_compatible"
    return host or "unknown_remote"


def trust_domain(url: str) -> TrustDomain:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    return TrustDomain(
        locality="local" if is_local_endpoint(url) else "remote",
        provider_family=_provider_family(url),
        host=host,
        endpoint_key=_normalize_endpoint_key(url),
    )


def _allowed(primary: TrustDomain, candidate: TrustDomain, policy: FallbackPolicy) -> tuple[bool, str]:
    if policy is FallbackPolicy.DISABLED:
        return False, "fallback_disabled"
    if policy is FallbackPolicy.EXPLICIT_CROSS_PROVIDER:
        return True, "explicit_cross_provider_opt_in"
    if policy is FallbackPolicy.SAME_ENDPOINT:
        if candidate.endpoint_key == primary.endpoint_key:
            return True, "same_endpoint"
        return False, "different_endpoint"
    if primary.locality != candidate.locality:
        return False, "locality_boundary"
    if primary.provider_family != candidate.provider_family:
        return False, "provider_boundary"
    if primary.locality == "remote" and primary.host != candidate.host:
        return False, "remote_host_boundary"
    return True, "same_trust_domain"


def filter_fallback_candidates(
    candidates: Iterable[tuple[str, str, Mapping[str, str] | None]],
    *,
    policy: FallbackPolicy | None = None,
) -> FallbackPlan:
    source = list(candidates or ())
    selected_policy = policy or load_fallback_policy()
    if not source:
        return FallbackPlan((), (), selected_policy)

    primary = source[0]
    primary_domain = trust_domain(primary[0])
    accepted: list[tuple[str, str, Mapping[str, str] | None]] = [primary]
    rejected: list[RejectedCandidate] = []
    seen = {(primary_domain.endpoint_key, str(primary[1]))}

    for index, candidate in enumerate(source[1:], start=1):
        url, model, headers = candidate
        domain = trust_domain(url)
        identity = (domain.endpoint_key, str(model))
        if identity in seen:
            rejected.append(
                RejectedCandidate(index, str(model), "duplicate_candidate", domain.public_dict())
            )
            continue
        seen.add(identity)
        allowed, reason = _allowed(primary_domain, domain, selected_policy)
        if allowed:
            accepted.append((url, model, headers))
        else:
            rejected.append(RejectedCandidate(index, str(model), reason, domain.public_dict()))
    return FallbackPlan(tuple(accepted), tuple(rejected), selected_policy)


def _tool_schema_tokens(tools: Sequence[Mapping[str, Any]] | None) -> int:
    if not tools:
        return 0
    raw = json.dumps(list(tools), sort_keys=True, separators=(",", ":"), default=str)
    return int(len(raw) * 0.3) + (8 * len(tools))


def project_messages_for_candidate(
    messages: Sequence[Mapping[str, Any]],
    *,
    endpoint_url: str,
    model: str,
    max_output_tokens: int = 0,
    tools: Sequence[Mapping[str, Any]] | None = None,
    minimum_input_tokens: int = 512,
    context_length_fn: Callable[[str, str], int] = get_context_length,
    estimate_tokens_fn: Callable[[list[dict]], int] = estimate_tokens,
) -> CandidateProjection:
    context_window = max(1024, int(context_length_fn(endpoint_url, model)))
    requested_output = int(max_output_tokens or 0)
    if requested_output <= 0:
        requested_output = min(32768, max(1024, context_window // 4))
    requested_output = min(requested_output, max(256, context_window // 2))
    schema_tokens = _tool_schema_tokens(tools)
    protocol_reserve = max(512, min(4096, context_window // 32))
    input_budget = context_window - requested_output - schema_tokens - protocol_reserve
    if input_budget < minimum_input_tokens:
        raise ContextBudgetExceeded(
            f"candidate {model!r} leaves only {input_budget} input tokens after output/schema reserves"
        )
    projected: ProjectedContext = project_context(
        [dict(message) for message in messages],
        budget_tokens=input_budget,
        estimate_tokens=estimate_tokens_fn,
    )
    return CandidateProjection(
        messages=projected.messages,
        context_window=context_window,
        input_budget=input_budget,
        estimated_tokens=projected.estimated_tokens,
        dropped_messages=projected.dropped_messages,
    )
