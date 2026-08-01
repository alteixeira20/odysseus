"""Argument-aware effect classification and capability/approval policy."""

from __future__ import annotations

import os
import re
import shlex
from enum import Enum
from typing import Any, Mapping, Sequence

from src.execution_policy import ExecutionMode

from .contracts import (
    AgentExecutionContext,
    ApprovalDecision,
    ApprovalOutcome,
    Capability,
    Effect,
)
from .workspace_service import WORKSPACE_SERVICE


class DefinitionApprovalPolicy(str, Enum):
    CAPABILITY_ONLY = "capability_only"
    SENSITIVE_EFFECTS = "sensitive_effects"
    ALWAYS_APPROVE = "always_approve"


_SAFE_HOST_PROGRAMS = frozenset(
    {
        "pwd",
        "whoami",
        "id",
        "uname",
        "date",
        "printf",
        "echo",
        "ls",
        "find",
        "rg",
        "grep",
        "head",
        "tail",
        "wc",
        "stat",
        "file",
        "du",
        "df",
        "realpath",
        "readlink",
        "git",
    }
)
_SAFE_GIT_SUBCOMMANDS = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "rev-parse",
        "ls-files",
        "ls-tree",
        "cat-file",
        "describe",
    }
)
_PACKAGE_MANAGERS = frozenset(
    {"apt", "apt-get", "dnf", "yum", "pacman", "apk", "brew", "pip", "pip3", "npm", "pnpm", "yarn"}
)
_SERVICE_PROGRAMS = frozenset({"systemctl", "service", "launchctl", "rc-service"})
_CONTAINER_PROGRAMS = frozenset({"docker", "podman", "kubectl", "nerdctl"})
_CONTAINER_READ_SUBCOMMANDS = frozenset(
    {"ps", "images", "inspect", "logs", "stats", "version", "info", "config", "get", "describe"}
)
_CREDENTIAL_PROGRAMS = frozenset(
    {"ssh", "scp", "sftp", "gpg", "pass", "security", "keyctl", "vault"}
)
_DESTRUCTIVE_PROGRAMS = frozenset(
    {"rm", "rmdir", "shred", "truncate", "dd", "mkfs", "wipefs", "unlink"}
)
_OPAQUE_SHELL = re.compile(r"(?:&&|\|\||[;|<>`]|\$\(|\n)")
_CREDENTIAL_PATH = re.compile(
    r"(?:^|[/\\])(?:\.ssh|\.gnupg|\.aws|\.kube|\.docker|\.config/gh)(?:[/\\]|$)|"
    r"(?:credentials|id_rsa|id_ed25519|private[_-]?key|\.env)(?:$|\s)",
    re.IGNORECASE,
)
_CREDENTIAL_REFERENCE = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|"
    r"AWS_[A-Z0-9_]+|SSH_[A-Z0-9_]+|GITHUB_[A-Z0-9_]+|GH_TOKEN)",
    re.IGNORECASE,
)


def _program(argv: Sequence[str]) -> str:
    return os.path.basename(argv[0]).casefold() if argv else "unknown"


def resolve_command_effects(
    arguments: Mapping[str, Any],
    context: AgentExecutionContext,
) -> tuple[Effect, ...]:
    command = str(arguments.get("command") or "").strip()
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        argv = []
    program = _program(argv)
    mode = context.execution_mode
    process_capability = (
        Capability.PROCESS_HOST
        if mode is ExecutionMode.HOST
        else Capability.PROCESS_SANDBOX
    )
    process_kind = (
        "process.execute.host"
        if mode is ExecutionMode.HOST
        else "process.execute.sandbox"
    )
    # Opaque host commands require approval. The same syntax remains bounded
    # by Bubblewrap in sandbox mode, where specifically classified sensitive
    # effects (delete/install/etc.) still apply independently.
    opaque = mode is ExecutionMode.HOST and (
        not argv or bool(_OPAQUE_SHELL.search(command))
    )
    if mode is ExecutionMode.HOST and not opaque:
        if program not in _SAFE_HOST_PROGRAMS:
            opaque = True
        elif program == "git":
            subcommand = next((item for item in argv[1:] if not item.startswith("-")), "")
            opaque = subcommand not in _SAFE_GIT_SUBCOMMANDS
        elif program == "find" and any(
            item in {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf"}
            for item in argv[1:]
        ):
            opaque = True
    effects: list[Effect] = [
        Effect(
            kind=process_kind,
            target=program,
            capability=process_capability,
            opaque=opaque,
            metadata={"execution_root": context.execution_root.path},
        )
    ]
    lowered = command.casefold()
    if program in {"sudo", "doas", "pkexec", "su"} or re.search(
        r"(?:^|\s)(?:sudo|doas|pkexec|su)(?:\s|$)", lowered
    ):
        effects.append(
            Effect(
                "privilege.escalation",
                program,
                Capability.PROCESS_HOST,
                consequential=True,
            )
        )
    if program in _PACKAGE_MANAGERS and any(
        item.casefold() in {"install", "add", "upgrade", "update", "remove", "uninstall"}
        for item in argv[1:]
    ):
        effects.append(
            Effect(
                "package.install",
                program,
                Capability.EXTERNAL_WRITE,
                consequential=True,
            )
        )
    if program in _SERVICE_PROGRAMS:
        effects.append(
            Effect(
                "service.mutate",
                " ".join(argv[1:3]) or program,
                Capability.EXTERNAL_WRITE,
                consequential=True,
            )
        )
    if program in _CONTAINER_PROGRAMS:
        subcommand = next((item.casefold() for item in argv[1:] if not item.startswith("-")), "")
        if subcommand not in _CONTAINER_READ_SUBCOMMANDS:
            effects.append(
                Effect(
                    "container.mutate",
                    subcommand or program,
                    Capability.EXTERNAL_WRITE,
                    consequential=True,
                )
            )
    if program == "git" and any(item.casefold() == "push" for item in argv[1:]):
        remote = "unknown"
        for index, item in enumerate(argv[1:], 1):
            if item.casefold() == "push" and index + 1 < len(argv):
                remote = argv[index + 1]
                break
        effects.append(
            Effect(
                "git.remote.write",
                remote,
                Capability.VCS_WRITE,
                consequential=True,
            )
        )
    if (
        program in _CREDENTIAL_PROGRAMS
        or _CREDENTIAL_PATH.search(command)
        or _CREDENTIAL_REFERENCE.search(command)
    ):
        effects.append(
            Effect(
                "credential.access",
                program,
                Capability.CREDENTIAL_ACCESS,
                consequential=True,
            )
        )
    destructive_find = program == "find" and "-delete" in argv[1:]
    destructive_git = program == "git" and any(
        item.casefold() in {"clean", "reset", "restore", "checkout"}
        for item in argv[1:]
    )
    if program in _DESTRUCTIVE_PROGRAMS or destructive_find or destructive_git:
        target = next((item for item in reversed(argv[1:]) if not item.startswith("-")), "unknown")
        effects.append(
            Effect(
                "filesystem.delete",
                target,
                Capability.WORKSPACE_WRITE,
                consequential=True,
                destructive=True,
            )
        )
    if program in {"curl", "wget", "http", "xh"}:
        host = next((item for item in argv[1:] if "://" in item), "unknown")
        mutating = bool(
            re.search(r"(?:--request|-X)\s*(?:POST|PUT|PATCH|DELETE)\b", command, re.IGNORECASE)
            or any(item in argv for item in ("--data", "--data-binary", "-d", "--upload-file", "-T"))
        )
        effects.append(
            Effect(
                "network.request.public",
                host,
                Capability.NETWORK_PUBLIC,
                consequential=mutating,
                metadata={"write": mutating},
            )
        )
        if mutating:
            effects.append(
                Effect(
                    "external.write",
                    host,
                    Capability.EXTERNAL_WRITE,
                    consequential=True,
                )
            )
    return tuple(effects)


def resolve_python_effects(
    arguments: Mapping[str, Any],
    context: AgentExecutionContext,
) -> tuple[Effect, ...]:
    if context.execution_mode is ExecutionMode.HOST:
        return (
            Effect(
                "process.execute.host",
                "python",
                Capability.PROCESS_HOST,
                opaque=True,
                metadata={"execution_root": context.execution_root.path},
            ),
        )
    return (
        Effect(
            "process.execute.sandbox",
            "python",
            Capability.PROCESS_SANDBOX,
            metadata={"execution_root": context.execution_root.path},
        ),
    )


def resolve_workspace_read_effects(
    arguments: Mapping[str, Any],
    context: AgentExecutionContext,
) -> tuple[Effect, ...]:
    paths: list[str] = []
    if isinstance(arguments.get("requests"), Sequence):
        paths.extend(
            str(item.get("path") or ".")
            for item in arguments["requests"]
            if isinstance(item, Mapping)
        )
    else:
        paths.append(str(arguments.get("path") or "."))
    return tuple(
        Effect(
            "filesystem.read",
            WORKSPACE_SERVICE.resolve(context.execution_root.path, path),
            Capability.WORKSPACE_READ,
        )
        for path in paths
    )


def resolve_workspace_patch_effects(
    arguments: Mapping[str, Any],
    context: AgentExecutionContext,
) -> tuple[Effect, ...]:
    effects: list[Effect] = []
    for operation in arguments.get("operations") or []:
        if not isinstance(operation, Mapping):
            continue
        kind = str(operation.get("type") or "write")
        if kind == "structured_patch":
            from src.agent_tools.filesystem_tools import _parse_agent_patch

            for parsed in _parse_agent_patch(
                str(operation.get("patch_text") or "")
            ):
                parsed_kind = str(parsed.get("kind") or "update")
                parsed_path = WORKSPACE_SERVICE.resolve(
                    context.execution_root.path,
                    str(parsed.get("path") or "."),
                )
                effects.append(
                    Effect(
                        (
                            "filesystem.delete"
                            if parsed_kind == "delete"
                            else "filesystem.write"
                        ),
                        parsed_path,
                        Capability.WORKSPACE_WRITE,
                        consequential=parsed_kind == "delete",
                        destructive=parsed_kind == "delete",
                        metadata={"operation": parsed_kind},
                    )
                )
            continue
        if kind == "move":
            source = WORKSPACE_SERVICE.resolve(
                context.execution_root.path,
                str(operation.get("source") or operation.get("path") or "."),
            )
            destination = WORKSPACE_SERVICE.resolve(
                context.execution_root.path,
                str(operation.get("destination") or "."),
            )
            effects.extend(
                (
                    Effect(
                        "filesystem.write",
                        source,
                        Capability.WORKSPACE_WRITE,
                        metadata={"operation": "move_source"},
                    ),
                    Effect(
                        "filesystem.write",
                        destination,
                        Capability.WORKSPACE_WRITE,
                        metadata={"operation": "move_destination"},
                    ),
                )
            )
            continue
        raw_path = str(
            operation.get("destination")
            or operation.get("path")
            or operation.get("source")
            or "."
        )
        path = WORKSPACE_SERVICE.resolve(context.execution_root.path, raw_path)
        effects.append(
            Effect(
                "filesystem.delete" if kind == "delete" else "filesystem.write",
                path,
                Capability.WORKSPACE_WRITE,
                consequential=kind == "delete",
                destructive=kind == "delete",
                metadata={"operation": kind},
            )
        )
    return tuple(effects)


class EffectPolicy:
    _SENSITIVE_KINDS = frozenset(
        {
            "privilege.escalation",
            "package.install",
            "service.mutate",
            "container.mutate",
            "git.remote.write",
            "credential.access",
            "filesystem.delete",
            "external.write",
        }
    )

    def evaluate(
        self,
        context: AgentExecutionContext,
        effects: Sequence[Effect],
        *,
        approval_policy: DefinitionApprovalPolicy,
    ) -> ApprovalOutcome:
        effect_tuple = tuple(effects)
        for effect in effect_tuple:
            if not context.authority_grant.allows(effect.capability):
                return ApprovalOutcome(
                    ApprovalDecision.DENY,
                    f"authority does not grant {effect.capability.value} for {effect.key}",
                    effect_tuple,
                )
        if approval_policy is DefinitionApprovalPolicy.ALWAYS_APPROVE:
            return ApprovalOutcome(
                ApprovalDecision.REQUIRE_APPROVAL,
                "tool requires approval for every call",
                effect_tuple,
            )
        if approval_policy is DefinitionApprovalPolicy.CAPABILITY_ONLY:
            return ApprovalOutcome(ApprovalDecision.ALLOW, "capabilities allow effects", effect_tuple)
        for effect in effect_tuple:
            if effect.key in context.authority_grant.approved_effects:
                continue
            if (
                effect.kind in self._SENSITIVE_KINDS
                or effect.consequential
                or effect.destructive
                or effect.opaque
            ):
                return ApprovalOutcome(
                    ApprovalDecision.REQUIRE_APPROVAL,
                    f"sensitive or opaque effect requires approval: {effect.key}",
                    effect_tuple,
                )
        return ApprovalOutcome(ApprovalDecision.ALLOW, "effects are justified and bounded", effect_tuple)


EFFECT_POLICY = EffectPolicy()
