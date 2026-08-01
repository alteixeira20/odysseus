"""Fail-closed process sandbox for model-authored Bash and Python code.

The sandbox is deliberately small and Linux-specific: bubblewrap creates fresh
user, PID, mount, IPC, UTS, cgroup and network namespaces; the host root is
read-only; sensitive home/runtime trees are hidden; and only explicitly listed
paths are writable.  Callers must not fall back to host execution when this
module reports that the sandbox is unavailable.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Sequence


class SandboxUnavailable(RuntimeError):
    """Raised when an enforceable local process sandbox cannot be built."""


@dataclass(frozen=True)
class SandboxCapability:
    available: bool
    reason: str
    platform: str


@lru_cache(maxsize=1)
def sandbox_capability() -> SandboxCapability:
    """Feature-detect the Linux namespace backend, including a real probe."""

    platform = sys.platform
    bwrap = shutil.which("bwrap")
    prlimit = shutil.which("prlimit")
    if os.name != "posix" or not platform.startswith("linux"):
        return SandboxCapability(
            False,
            "safe workspace shell is Linux-only; authorize Full host shell explicitly on this platform",
            platform,
        )
    if not Path("/proc").is_dir() or not bwrap or not prlimit:
        return SandboxCapability(
            False,
            "safe workspace shell requires Bubblewrap, prlimit, and /proc",
            platform,
        )
    try:
        probe = subprocess.run(
            [
                bwrap,
                "--unshare-all",
                "--die-with-parent",
                "--ro-bind", "/", "/",
                "--proc", "/proc",
                "--dev", "/dev",
                "--", "/bin/true",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=minimal_environment({}),
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SandboxCapability(False, f"safe workspace shell probe failed: {exc}", platform)
    if probe.returncode != 0:
        detail = probe.stderr.decode("utf-8", errors="replace").strip()[:240]
        return SandboxCapability(
            False,
            "safe workspace shell namespaces are unavailable"
            + (f": {detail}" if detail else ""),
            platform,
        )
    return SandboxCapability(True, "available", platform)


_SAFE_ENV_KEYS = frozenset({
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "COLUMNS",
    "LINES",
    "PYTHONIOENCODING",
})
_FIXED_PATH = "/usr/local/bin:/usr/bin:/bin"
_SANDBOX_HOME = "/tmp/odysseus-home"


def minimal_environment(
    environment: Mapping[str, object] | None = None,
    *,
    runtime: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Return a minimal environment, never an inherited copy of ``os.environ``.

    ``runtime`` is only for values manufactured by the execution layer (for
    example the invocation nonce and encoded command).  Arbitrary caller keys
    are intentionally not forwarded.
    """

    source = environment or {}
    clean = {
        key: str(source[key])
        for key in _SAFE_ENV_KEYS
        if source.get(key) not in (None, "")
    }
    clean.update({
        "PATH": _FIXED_PATH,
        "HOME": _SANDBOX_HOME,
        "TMPDIR": "/tmp",
        "TERM": clean.get("TERM", "xterm-256color"),
        "COLUMNS": clean.get("COLUMNS", "120"),
        "LINES": clean.get("LINES", "40"),
        "PYTHONIOENCODING": clean.get("PYTHONIOENCODING", "utf-8"),
    })
    for key, value in (runtime or {}).items():
        if not str(key).startswith("ODY_"):
            raise ValueError(f"sandbox runtime variable must use the ODY_ prefix: {key}")
        clean[str(key)] = str(value)
    return clean


def _mkdir_targets(path: str) -> list[str]:
    """Build bubblewrap ``--dir`` arguments below a masked top-level tree."""

    resolved = Path(path)
    parts = resolved.parts
    if not resolved.is_absolute() or len(parts) < 2:
        return []
    arguments: list[str] = []
    current = Path(parts[0]) / parts[1]
    # /tmp, /home, /root and /run already exist as tmpfs mount points.
    for part in parts[2:]:
        current /= part
        arguments.extend(("--dir", str(current)))
    return arguments


def _canonical_writable_directories(paths: Iterable[str | os.PathLike[str]]) -> list[str]:
    directories: list[str] = []
    for raw in paths:
        canonical = os.path.realpath(os.fspath(raw))
        if not os.path.isdir(canonical):
            raise SandboxUnavailable(f"sandbox writable path is not a directory: {canonical}")
        if canonical == os.path.sep:
            raise SandboxUnavailable("refusing to mount the filesystem root writable")
        if canonical not in directories:
            directories.append(canonical)
    # If a parent is already writable, a nested bind adds no confinement.
    return [
        path
        for path in directories
        if not any(path != other and os.path.commonpath((path, other)) == other for other in directories)
    ]


def sandbox_command(
    command: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    writable_paths: Iterable[str | os.PathLike[str]] = (),
    environment: Mapping[str, object] | None = None,
    runtime_environment: Mapping[str, object] | None = None,
    timeout_seconds: float = 120,
) -> tuple[list[str], dict[str, str]]:
    """Wrap ``command`` in bubblewrap and resource limits.

    The returned environment is also minimal for the outer bubblewrap process;
    ``--clearenv`` independently enforces the same boundary inside the sandbox.
    Network access is absent because ``--unshare-all`` is used without
    ``--share-net``.
    """

    bwrap = shutil.which("bwrap")
    prlimit = shutil.which("prlimit")
    if os.name != "posix" or not Path("/proc").is_dir() or not bwrap or not prlimit:
        raise SandboxUnavailable(
            "Bash/Python execution requires Linux bubblewrap and prlimit; host fallback is disabled"
        )
    if not command:
        raise ValueError("sandbox command may not be empty")

    canonical_cwd = os.path.realpath(os.fspath(cwd))
    if not os.path.isdir(canonical_cwd):
        raise SandboxUnavailable(f"sandbox working directory does not exist: {canonical_cwd}")
    writable = _canonical_writable_directories((canonical_cwd, *writable_paths))
    clean_env = minimal_environment(environment, runtime=runtime_environment)

    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--cap-drop", "ALL",
        "--hostname", "odysseus-sandbox",
        "--ro-bind", "/", "/",
        # Hide host user data and mutable runtime/temp trees before selectively
        # mounting the run workspace and private execution metadata back in.
        "--tmpfs", "/home",
        "--tmpfs", "/root",
        "--tmpfs", "/run",
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
        "--dir", _SANDBOX_HOME,
        "--clearenv",
    ]
    for key in sorted(clean_env):
        argv.extend(("--setenv", key, clean_env[key]))

    data_directory = ""
    data_mask_before_workspace = False
    data_mask_after_workspace = False
    try:
        from src.constants import DATA_DIR

        data_directory = os.path.realpath(DATA_DIR)
        if canonical_cwd == data_directory:
            raise SandboxUnavailable(
                "safe workspace shell cannot use the Odysseus application-data root; "
                "select a repository or a non-sensitive subdirectory"
            )
        cwd_inside_data = os.path.commonpath(
            (canonical_cwd, data_directory)
        ) == data_directory
        if os.path.isdir(data_directory):
            data_mask_before_workspace = cwd_inside_data
            data_mask_after_workspace = not cwd_inside_data
    except (ImportError, OSError, ValueError):
        data_directory = ""

    if data_mask_before_workspace:
        argv.extend(_mkdir_targets(data_directory))
        argv.extend(("--tmpfs", data_directory))
    for directory in writable:
        argv.extend(_mkdir_targets(directory))
        argv.extend(("--bind", directory, directory))

    # Application state can contain API keys, OAuth material, mail data and
    # vault contents. Hide it even when it sits outside /home (notably
    # /app/data in containers), or when the selected repository is its parent.
    # DATA_DIR itself is rejected above. A non-sensitive nested workspace is
    # rebound after masking the parent; a parent workspace is bound first and
    # then has its data subtree masked.
    if data_mask_after_workspace:
        argv.extend(_mkdir_targets(data_directory))
        argv.extend(("--tmpfs", data_directory))

    cpu_limit = max(1, int(math.ceil(float(timeout_seconds))) + 5)
    argv.extend((
        "--chdir", canonical_cwd,
        "--",
        prlimit,
        f"--cpu={cpu_limit}:{cpu_limit}",
        "--as=2147483648:2147483648",
        "--nproc=256:256",
        "--fsize=268435456:268435456",
        "--nofile=256:256",
        "--",
        *[str(part) for part in command],
    ))
    return argv, clean_env


__all__ = [
    "SandboxCapability",
    "SandboxUnavailable",
    "minimal_environment",
    "sandbox_capability",
    "sandbox_command",
]
