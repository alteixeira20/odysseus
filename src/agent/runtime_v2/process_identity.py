"""Behavioral identity snapshots for approved generic process execution."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from core.platform_compat import find_bash
from src.execution_policy import ExecutionMode, normalize_execution_mode
from src.process_sandbox import minimal_environment

from .internal_git import HARDENED_GIT
from .workspace_service import WORKSPACE_SERVICE


class ProcessIdentityError(RuntimeError):
    code = "process_identity_unavailable"


@dataclass(frozen=True)
class ProcessIdentity:
    digest: str
    executable: str
    environment_digest: str
    dependency_digests: tuple[tuple[str, str], ...]


def execution_environment() -> dict[str, str]:
    """The exact minimal environment used by Runtime V2 host processes."""

    environment = minimal_environment(os.environ)
    # Exact-approved Git commands must not acquire ambient behavior from the
    # host's system/global configuration, helpers, prompts, or pagers.
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def _file_digest(path: str) -> tuple[str, str]:
    canonical = os.path.realpath(path)
    try:
        info = os.stat(canonical, follow_symlinks=False)
    except OSError as exc:
        raise ProcessIdentityError(f"process dependency is unavailable: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ProcessIdentityError(f"process dependency is not a regular file: {path}")
    if info.st_size > 256 * 1024 * 1024:
        raise ProcessIdentityError(f"process dependency exceeds the identity budget: {path}")
    digest = hashlib.sha256()
    try:
        with open(canonical, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ProcessIdentityError(f"process dependency cannot be read: {path}") from exc
    identity = {
        "sha256": digest.hexdigest(),
        "mode": stat.S_IMODE(info.st_mode),
        "size": info.st_size,
        "device": info.st_dev,
        "inode": info.st_ino,
    }
    return canonical, hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _add_dependency(
    path: str,
    *,
    dependencies: dict[str, str],
    environment: Mapping[str, str],
    root: str,
) -> None:
    """Hash a dependency and recursively bind a script's shebang runtime."""

    canonical, digest = _file_digest(path)
    if canonical in dependencies:
        return
    dependencies[canonical] = digest
    try:
        with open(canonical, "rb") as handle:
            first_line = handle.readline(4096)
    except OSError as exc:
        raise ProcessIdentityError(
            f"process dependency cannot be inspected: {canonical}"
        ) from exc
    if not first_line.startswith(b"#!"):
        return
    try:
        shebang = first_line[2:].decode("utf-8", errors="strict").strip()
        argv = shlex.split(shebang, posix=True)
    except (UnicodeError, ValueError) as exc:
        raise ProcessIdentityError(
            f"process script has an unbindable shebang: {canonical}"
        ) from exc
    if not argv:
        raise ProcessIdentityError(
            f"process script has an empty shebang: {canonical}"
        )
    interpreter = _resolve_program(argv[0], environment, root)
    if not interpreter:
        raise ProcessIdentityError(
            f"process script interpreter is unavailable: {argv[0]}"
        )
    _add_dependency(
        interpreter,
        dependencies=dependencies,
        environment=environment,
        root=root,
    )
    if os.path.basename(os.path.realpath(interpreter)) != "env":
        return
    # ``#!/usr/bin/env python`` and ``env -S 'python -I'`` delegate to a
    # second executable selected through the exact approved PATH.
    delegated = argv[1:]
    if delegated[:1] == ["-S"] and len(delegated) > 1:
        delegated = shlex.split(" ".join(delegated[1:]), posix=True)
    target_name = ""
    index = 0
    while index < len(delegated):
        item = delegated[index]
        if item in {"-u", "--unset", "-C", "--chdir"}:
            index += 2
            continue
        if item.startswith(("--unset=", "--chdir=")) or item.startswith("-"):
            index += 1
            continue
        if "=" in item:
            index += 1
            continue
        target_name = item
        break
    if target_name:
        target = _resolve_program(target_name, environment, root)
        if not target:
            raise ProcessIdentityError(
                f"process script interpreter is unavailable: {target_name}"
            )
        _add_dependency(
            target,
            dependencies=dependencies,
            environment=environment,
            root=root,
        )


def _resolve_program(program: str, environment: Mapping[str, str], root: str) -> Optional[str]:
    value = str(program or "")
    if not value:
        return None
    if os.path.isabs(value):
        return value if os.path.isfile(os.path.realpath(value)) else None
    if "/" in value or "\\" in value:
        candidate = os.path.realpath(os.path.join(root, value))
        return candidate if os.path.isfile(candidate) else None
    return shutil.which(value, path=environment.get("PATH", ""))


def _candidate_tokens(command: str) -> Iterable[str]:
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ProcessIdentityError(f"host command cannot be snapshotted: {exc}") from exc
    yield from argv
    for index, token in enumerate(argv):
        if token == "--pre" and index + 1 < len(argv):
            try:
                yield from shlex.split(argv[index + 1], posix=True)
            except ValueError as exc:
                raise ProcessIdentityError("rg --pre command cannot be snapshotted") from exc
        elif token.startswith("--pre="):
            try:
                yield from shlex.split(token.split("=", 1)[1], posix=True)
            except ValueError as exc:
                raise ProcessIdentityError("rg --pre command cannot be snapshotted") from exc


def snapshot_process(
    command: str,
    *,
    root: str,
    execution_mode: ExecutionMode | str = ExecutionMode.HOST,
) -> ProcessIdentity:
    """Hash the shell, reachable executable/script inputs, environment and Git metadata.

    The command remains exact-approved as text.  This snapshot additionally
    prevents that approval from surviving executable, interpreter, explicit
    script, repository Git configuration, or relevant environment drift.
    """

    canonical_root = os.path.realpath(root)
    mode = normalize_execution_mode(execution_mode)
    environment = (
        execution_environment()
        if mode is ExecutionMode.HOST
        else minimal_environment({})
    )
    shell = find_bash() if mode is ExecutionMode.HOST else "/bin/bash"
    if not shell:
        raise ProcessIdentityError("host command approval requires an installed Bash executable")
    dependencies: dict[str, str] = {}
    _add_dependency(
        shell,
        dependencies=dependencies,
        environment=environment,
        root=canonical_root,
    )
    shell_path = os.path.realpath(shell)
    if mode is ExecutionMode.SANDBOXED:
        for boundary_program in ("bwrap", "prlimit"):
            boundary_path = shutil.which(
                boundary_program,
                path=environment.get("PATH", ""),
            )
            if not boundary_path:
                raise ProcessIdentityError(
                    f"sandbox process approval requires {boundary_program}"
                )
            _add_dependency(
                boundary_path,
                dependencies=dependencies,
                environment=environment,
                root=canonical_root,
            )
    tokens = list(_candidate_tokens(command))
    if not tokens:
        raise ProcessIdentityError("host command may not be empty")

    for token in tokens:
        raw = str(token)
        candidate = _resolve_program(raw, environment, canonical_root)
        if candidate:
            _add_dependency(
                candidate,
                dependencies=dependencies,
                environment=environment,
                root=canonical_root,
            )
            continue
        # Explicit existing script/data arguments can alter interpreter
        # behavior even when they are not executable themselves.
        if os.path.isabs(raw) or "/" in raw or "\\" in raw:
            explicit = os.path.realpath(
                raw if os.path.isabs(raw) else os.path.join(canonical_root, raw)
            )
            if os.path.isfile(explicit):
                _add_dependency(
                    explicit,
                    dependencies=dependencies,
                    environment=environment,
                    root=canonical_root,
                )

    environment_digest = hashlib.sha256(
        json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    git_identity = HARDENED_GIT.inspect(canonical_root)
    workspace_revision = WORKSPACE_SERVICE.revision(canonical_root)
    payload = {
        "version": 2,
        "execution_mode": mode.value,
        "command": str(command),
        "root": canonical_root,
        "shell": shell_path,
        "environment": environment_digest,
        "dependencies": sorted(dependencies.items()),
        "git_identity": git_identity.digest,
        "git_identity_stable": git_identity.stable,
        "workspace_revision": workspace_revision,
    }
    if not git_identity.stable:
        raise ProcessIdentityError("repository Git identity exceeded its safety budget")
    for hook_path in HARDENED_GIT.default_hook_paths(canonical_root):
        _add_dependency(
            hook_path,
            dependencies=dependencies,
            environment=environment,
            root=canonical_root,
        )
    # Hook interpreter dependencies are part of the digest as well as the
    # public dependency list, so a shebang runtime change burns the approval.
    payload["dependencies"] = sorted(dependencies.items())
    if not WORKSPACE_SERVICE.revision_is_strong(workspace_revision):
        raise ProcessIdentityError("workspace identity exceeded its process safety budget")
    return ProcessIdentity(
        digest=hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        executable=shell_path,
        environment_digest=environment_digest,
        dependency_digests=tuple(sorted(dependencies.items())),
    )


__all__ = [
    "ProcessIdentity",
    "ProcessIdentityError",
    "execution_environment",
    "snapshot_process",
]
