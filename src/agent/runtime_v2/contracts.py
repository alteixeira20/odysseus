"""Immutable authority, call, effect, and result contracts for Runtime V2."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

from src.agent.execution.observation_ledger import ObservationLedger
from src.execution_policy import ExecutionMode


def immutable_mapping(value: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


class ExecutionRootSource(str, Enum):
    SELECTED_WORKSPACE = "selected_workspace"
    CONFIGURED_DEFAULT = "configured_default"
    EPHEMERAL_WORKSPACE = "ephemeral_workspace"
    HOST_DEFAULT = "host_default"


@dataclass(frozen=True)
class ExecutionRoot:
    path: str
    source: ExecutionRootSource
    writable: bool
    workspace_revision: str

    def __post_init__(self) -> None:
        canonical = os.path.realpath(os.fspath(self.path))
        if not os.path.isabs(canonical):
            raise ValueError("execution root must be absolute")
        if canonical != self.path:
            raise ValueError("execution root must already be canonical")
        if not os.path.isdir(canonical):
            raise ValueError(f"execution root is not a directory: {canonical}")
        if not self.workspace_revision:
            raise ValueError("execution root requires a workspace revision")


class Capability(str, Enum):
    WORKSPACE_READ = "workspace_read"
    WORKSPACE_WRITE = "workspace_write"
    PROCESS_SANDBOX = "process_sandbox"
    PROCESS_HOST = "process_host"
    VCS_READ = "vcs_read"
    VCS_WRITE = "vcs_write"
    NETWORK_PUBLIC = "network_public"
    NETWORK_PRIVATE = "network_private"
    EXTERNAL_READ = "external_read"
    EXTERNAL_WRITE = "external_write"
    CREDENTIAL_ACCESS = "credential_access"


@dataclass(frozen=True)
class AuthorityGrant:
    capabilities: frozenset[Capability]
    revision: str
    host_authorization_id: Optional[str] = None
    approved_effects: frozenset[str] = frozenset()
    disabled_tools: frozenset[str] = frozenset()

    def allows(self, capability: Capability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class RunBudgets:
    max_rounds: int = 50
    max_tool_calls: int = 256
    max_provider_requests: int = 128
    wall_clock_seconds: float = 3600.0
    idle_seconds: float = 300.0
    max_output_chars: int = 80_000

    def __post_init__(self) -> None:
        if min(self.max_rounds, self.max_tool_calls, self.max_provider_requests) < 1:
            raise ValueError("run count budgets must be positive")
        if min(self.wall_clock_seconds, self.idle_seconds, self.max_output_chars) <= 0:
            raise ValueError("run time/output budgets must be positive")


class CancellationToken:
    """A run-owned cancellation identity with no authority-bearing globals."""

    def __init__(self, identity: str):
        self._identity = str(identity)
        self._event = asyncio.Event()

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError


@dataclass(frozen=True)
class AgentExecutionContext:
    run_id: str
    owner_id: str
    session_id: str
    conversation_id: str
    turn_id: str
    candidate_id: str
    execution_mode: ExecutionMode
    execution_root: ExecutionRoot
    authority_grant: AuthorityGrant
    budgets: RunBudgets
    cancellation_token: CancellationToken
    tool_catalog_revision: str
    observation_ledger: ObservationLedger
    event_factory: Any = field(compare=False, repr=False)

    def __post_init__(self) -> None:
        if not all((self.run_id, self.session_id, self.conversation_id, self.turn_id)):
            raise ValueError(
                "execution context requires run, session, conversation, and turn identities"
            )
        if not isinstance(self.execution_mode, ExecutionMode):
            raise TypeError("execution_mode must be an ExecutionMode")


@dataclass(frozen=True)
class Effect:
    kind: str
    target: str
    capability: Capability
    consequential: bool = False
    destructive: bool = False
    opaque: bool = False
    metadata: Mapping[str, Any] = field(default_factory=immutable_mapping)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.target}"


class ApprovalDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class ApprovalOutcome:
    decision: ApprovalDecision
    reason: str
    effects: tuple[Effect, ...] = ()


@dataclass(frozen=True)
class NormalizedToolCall:
    call_id: str
    canonical_name: str
    arguments: Mapping[str, Any]
    provider_name: str
    raw_name: str
    run_id: str = "legacy"
    conversation_id: str = "legacy"
    turn_id: str = "legacy"
    candidate_id: str = "legacy"
    provider_round: int = 0
    authority_revision: str = "legacy"
    workspace_revision: str = "legacy"
    tool_contract_revision: str = "legacy"
    legacy_content: Optional[str] = None
    normalization_error: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.call_id or not self.canonical_name or not self.raw_name:
            raise ValueError("normalized tool calls require id, canonical name, and raw name")
        object.__setattr__(self, "arguments", immutable_mapping(self.arguments))

    @property
    def arguments_digest(self) -> str:
        raw = json.dumps(
            dict(self.arguments),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class ToolResultStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=immutable_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", immutable_mapping(self.details))


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    canonical_name: str
    status: ToolResultStatus
    data: Mapping[str, Any] = field(default_factory=immutable_mapping)
    error: Optional[ToolError] = None
    committed_effects: tuple[Effect, ...] = ()
    artifacts: tuple[Mapping[str, Any], ...] = ()
    truncation: Optional[Mapping[str, Any]] = None
    continuation: Optional[Mapping[str, Any]] = None
    backend: str = "runtime_v2"
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", immutable_mapping(self.data))
        object.__setattr__(
            self,
            "artifacts",
            tuple(immutable_mapping(item) for item in self.artifacts),
        )
        if self.truncation is not None:
            object.__setattr__(self, "truncation", immutable_mapping(self.truncation))
        if self.continuation is not None:
            object.__setattr__(self, "continuation", immutable_mapping(self.continuation))
        if self.status is ToolResultStatus.SUCCESS and self.error is not None:
            raise ValueError("successful ToolResult cannot contain an error")
        if self.status is not ToolResultStatus.SUCCESS and self.error is None:
            raise ValueError(f"{self.status.value} ToolResult requires a typed error")

    def bind_call(self, call: NormalizedToolCall) -> "ToolResult":
        return replace(
            self,
            call_id=call.call_id,
            canonical_name=call.canonical_name,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "canonical_name": self.canonical_name,
            "status": self.status.value,
            "data": dict(self.data),
            "error": (
                {
                    "code": self.error.code,
                    "message": self.error.message,
                    "details": dict(self.error.details),
                }
                if self.error
                else None
            ),
            "committed_effects": [
                {
                    "kind": effect.kind,
                    "target": effect.target,
                    "key": effect.key,
                    "capability": effect.capability.value,
                    "consequential": effect.consequential,
                    "destructive": effect.destructive,
                    "opaque": effect.opaque,
                    "metadata": dict(effect.metadata),
                }
                for effect in self.committed_effects
            ],
            "artifacts": [dict(item) for item in self.artifacts],
            "truncation": dict(self.truncation) if self.truncation else None,
            "continuation": dict(self.continuation) if self.continuation else None,
            "backend": self.backend,
            "duration_ms": self.duration_ms,
        }

    def model_text(self) -> str:
        if self.error is not None:
            return self.error.message
        for key in ("text", "summary", "stdout"):
            value = self.data.get(key)
            if value not in (None, ""):
                return str(value)
        return "(no output)"

    def legacy_projection(self) -> dict[str, Any]:
        """One bounded compatibility projection for unchanged non-V2 UI code."""

        projected = {
            "tool_result": self.as_dict(),
            "completion_state": self.status.value,
            "backend": self.backend,
            "invocation_id": self.call_id,
            **dict(self.data),
        }
        if self.error is not None:
            projected.update(
                {
                    "error": self.error.message,
                    "error_type": self.error.code,
                    "exit_code": projected.get("exit_code", 1),
                }
            )
        else:
            projected.setdefault("output", self.model_text())
            projected.setdefault("exit_code", 0)
        if self.truncation:
            projected["truncation"] = dict(self.truncation)
        if self.continuation:
            projected["continuation"] = dict(self.continuation)
        return projected


def error_result(
    *,
    call: NormalizedToolCall,
    status: ToolResultStatus,
    code: str,
    message: str,
    effects: Sequence[Effect] = (),
    backend: str = "runtime_v2",
    details: Optional[Mapping[str, Any]] = None,
) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        canonical_name=call.canonical_name,
        status=status,
        error=ToolError(code, message, immutable_mapping(details)),
        committed_effects=tuple(effects),
        backend=backend,
    )
