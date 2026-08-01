"""Server-bound one-run authority and exact execution-root preparation."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional

from src.agent.execution.observation_ledger import ObservationLedger
from src.execution_policy import ExecutionMode, normalize_execution_mode

from .contracts import (
    AgentExecutionContext,
    AuthorityGrant,
    Capability,
    CancellationToken,
    ExecutionRoot,
    ExecutionRootSource,
    RunBudgets,
)
from .events import RuntimeEventFactory
from .workspace_service import WORKSPACE_SERVICE, WorkspacePathError


@dataclass(frozen=True)
class HostAuthorization:
    token: str
    authorization_id: str
    expires_at: float


@dataclass(frozen=True)
class _HostAuthorizationRecord:
    authorization_id: str
    owner_id: str
    session_id: str
    expires_at: float


class HostAuthorizationStore:
    """One-use, process-local grants; raw tokens are never retained or logged."""

    def __init__(self, *, ttl_seconds: float = 120.0) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self._records: dict[str, _HostAuthorizationRecord] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        for digest, record in list(self._records.items()):
            if record.expires_at <= now:
                self._records.pop(digest, None)

    def issue(self, *, owner_id: str, session_id: str) -> HostAuthorization:
        owner = str(owner_id or "")
        session = str(session_id or "")
        if not owner or not session:
            raise ValueError("host authorization requires owner and session")
        token = secrets.token_urlsafe(32)
        authorization_id = secrets.token_urlsafe(18)
        expires_at = time.time() + self.ttl_seconds
        record = _HostAuthorizationRecord(
            authorization_id=authorization_id,
            owner_id=owner,
            session_id=session,
            expires_at=expires_at,
        )
        with self._lock:
            self._prune(time.time())
            self._records[self._digest(token)] = record
        return HostAuthorization(token, authorization_id, expires_at)

    def consume(
        self,
        token: Optional[str],
        *,
        owner_id: str,
        session_id: str,
    ) -> Optional[str]:
        if not token:
            return None
        digest = self._digest(str(token))
        now = time.time()
        with self._lock:
            self._prune(now)
            record = self._records.get(digest)
            if record is None:
                return None
            if (
                record.owner_id != str(owner_id or "")
                or record.session_id != str(session_id or "")
            ):
                return None
            self._records.pop(digest, None)
        return record.authorization_id

    def pending_count(self) -> int:
        with self._lock:
            self._prune(time.time())
            return len(self._records)


HOST_AUTHORIZATIONS = HostAuthorizationStore()


def _configured_root(raw: Optional[str], label: str) -> Optional[str]:
    if not raw:
        return None
    canonical = os.path.realpath(os.path.expanduser(str(raw)))
    if not os.path.isabs(canonical) or not os.path.isdir(canonical):
        raise WorkspacePathError(f"configured {label} is not a directory")
    if os.path.dirname(canonical) == canonical:
        raise WorkspacePathError(f"configured {label} cannot be a filesystem root")
    if WORKSPACE_SERVICE.is_sensitive_path(canonical):
        raise WorkspacePathError(f"configured {label} is a sensitive or excluded path")
    return canonical


def resolve_execution_root(
    *,
    execution_mode: ExecutionMode,
    selected_workspace: Optional[str],
    sandbox_default: Optional[str] = None,
    host_default: Optional[str] = None,
) -> tuple[ExecutionMode, ExecutionRoot, str]:
    """Resolve a canonical root once; process CWD is never consulted."""

    selected = _configured_root(selected_workspace, "selected workspace")
    sandbox_configured = _configured_root(sandbox_default, "sandbox default")
    host_configured = _configured_root(host_default, "host default")
    mode = normalize_execution_mode(execution_mode)
    reason = "requested"
    if selected:
        root_path = selected
        source = ExecutionRootSource.SELECTED_WORKSPACE
    elif mode is ExecutionMode.HOST and host_configured:
        root_path = host_configured
        source = ExecutionRootSource.HOST_DEFAULT
    elif mode is ExecutionMode.HOST:
        # A host grant without an explicit root is not silently rebound to the
        # service working directory. Keep the run usable for non-process tools
        # in an isolated root, but remove host process authority.
        mode = ExecutionMode.DISABLED
        reason = "host_execution_requires_selected_or_configured_root"
        root_path = tempfile.mkdtemp(prefix="odysseus-agent-workspace-")
        source = ExecutionRootSource.EPHEMERAL_WORKSPACE
    elif sandbox_configured:
        root_path = sandbox_configured
        source = ExecutionRootSource.CONFIGURED_DEFAULT
    else:
        root_path = tempfile.mkdtemp(prefix="odysseus-agent-workspace-")
        source = ExecutionRootSource.EPHEMERAL_WORKSPACE
    canonical = os.path.realpath(root_path)
    writable = os.access(canonical, os.W_OK)
    root = ExecutionRoot(
        path=canonical,
        source=source,
        writable=writable,
        workspace_revision=WORKSPACE_SERVICE.revision(canonical),
    )
    return mode, root, reason


def _authority_capabilities(
    *,
    mode: ExecutionMode,
    root: ExecutionRoot,
    plan_mode: bool,
) -> frozenset[Capability]:
    capabilities = {Capability.WORKSPACE_READ, Capability.VCS_READ}
    if root.writable and not plan_mode:
        capabilities.update({Capability.WORKSPACE_WRITE, Capability.VCS_WRITE})
    if mode is ExecutionMode.SANDBOXED and not plan_mode:
        capabilities.add(Capability.PROCESS_SANDBOX)
    elif mode is ExecutionMode.HOST and not plan_mode:
        capabilities.update(
            {
                Capability.PROCESS_HOST,
                Capability.NETWORK_PUBLIC,
                Capability.NETWORK_PRIVATE,
                Capability.EXTERNAL_READ,
                Capability.EXTERNAL_WRITE,
                Capability.CREDENTIAL_ACCESS,
            }
        )
    return frozenset(capabilities)


def prepare_execution_context(
    *,
    owner_id: Optional[str],
    session_id: str,
    requested_mode: ExecutionMode | str,
    selected_workspace: Optional[str],
    budgets: RunBudgets,
    tool_catalog_revision: str,
    plan_mode: bool = False,
    host_authorization_token: Optional[str] = None,
    sandbox_default: Optional[str] = None,
    host_default: Optional[str] = None,
    run_id: Optional[str] = None,
    disabled_tools: frozenset[str] = frozenset(),
) -> tuple[AgentExecutionContext, str]:
    owner = str(owner_id or "")
    session = str(session_id)
    mode = normalize_execution_mode(requested_mode)
    host_authorization_id = None
    reason = "requested"
    if mode is ExecutionMode.HOST:
        host_authorization_id = HOST_AUTHORIZATIONS.consume(
            host_authorization_token,
            owner_id=owner,
            session_id=session,
        )
        if host_authorization_id is None:
            mode = ExecutionMode.DISABLED
            reason = "host_authorization_missing_or_consumed"
    elif host_authorization_token:
        # Explicit shell denial wins and also burns a supplied one-run token;
        # it cannot be replayed on a later request within the TTL.
        HOST_AUTHORIZATIONS.consume(
            host_authorization_token,
            owner_id=owner,
            session_id=session,
        )
    mode, execution_root, root_reason = resolve_execution_root(
        execution_mode=mode,
        selected_workspace=selected_workspace,
        sandbox_default=sandbox_default,
        host_default=host_default,
    )
    if root_reason != "requested":
        reason = root_reason
    capabilities = _authority_capabilities(
        mode=mode,
        root=execution_root,
        plan_mode=plan_mode,
    )
    revision_material = json.dumps(
        {
            "capabilities": sorted(item.value for item in capabilities),
            "host_authorization_id": host_authorization_id,
            "execution_mode": mode.value,
            "root": execution_root.path,
            "workspace_revision": execution_root.workspace_revision,
            "disabled_tools": sorted(str(item) for item in disabled_tools),
            "plan_mode": bool(plan_mode),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    authority = AuthorityGrant(
        capabilities=capabilities,
        revision=hashlib.sha256(revision_material).hexdigest(),
        host_authorization_id=host_authorization_id,
        disabled_tools=frozenset(disabled_tools),
    )
    actual_run_id = str(run_id or secrets.token_urlsafe(18))
    context = AgentExecutionContext(
        run_id=actual_run_id,
        owner_id=owner,
        session_id=session,
        execution_mode=mode,
        execution_root=execution_root,
        authority_grant=authority,
        budgets=budgets,
        cancellation_token=CancellationToken(secrets.token_urlsafe(18)),
        tool_catalog_revision=str(tool_catalog_revision),
        observation_ledger=ObservationLedger(),
        event_factory=RuntimeEventFactory(actual_run_id),
    )
    return context, reason
