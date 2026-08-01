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
import re
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
_SENSITIVE_DIRECTORIES = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        ".terraform.d",
        ".password-store",
        ".config/gh",
        ".config/gcloud",
        ".config/hub",
        ".local/share/keyrings",
    }
)
_SENSITIVE_FILENAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        ".envrc",
        "credentials",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "service-account.json",
    }
)
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_SCAN_SKIP_DIRECTORIES = frozenset(
    {"node_modules", ".venv", "venv", "vendor", "target", "dist", "build", "__pycache__"}
)


def _workspace_secret_mounts(
    root: str,
    *,
    excluded_roots: Iterable[str] = (),
    max_entries: int = 50_000,
) -> tuple[list[str], list[str]]:
    """Return common credential directories/files to mask inside Bubblewrap.

    Ignored and untracked project files remain visible but read-only; this is
    deliberate so tests can use local fixtures and dependency trees. Common
    credential material is masked regardless of Git tracking state.
    """

    secret_directories: list[str] = []
    secret_files: list[str] = []
    excluded = {
        os.path.realpath(path)
        for path in excluded_roots
        if path and os.path.isdir(os.path.realpath(path))
    }
    examined = 0
    for current, directories, files in os.walk(root, followlinks=False):
        kept: list[str] = []
        for directory in directories:
            examined += 1
            absolute = os.path.join(current, directory)
            canonical_absolute = os.path.realpath(absolute)
            if any(
                canonical_absolute == excluded_root
                or os.path.commonpath((canonical_absolute, excluded_root))
                == excluded_root
                for excluded_root in excluded
            ):
                continue
            relative = os.path.relpath(absolute, root).replace(os.sep, "/").casefold()
            if directory.casefold() in _SENSITIVE_DIRECTORIES or relative in _SENSITIVE_DIRECTORIES:
                secret_directories.append(absolute)
                continue
            if directory.casefold() in _SCAN_SKIP_DIRECTORIES or directory == ".git":
                continue
            if not os.path.islink(absolute):
                kept.append(directory)
        directories[:] = kept
        for filename in files:
            examined += 1
            lowered = filename.casefold()
            if (
                lowered in _SENSITIVE_FILENAMES
                or lowered.startswith(".env.")
                or lowered.endswith(_SECRET_SUFFIXES)
                or ("private" in lowered and "key" in lowered)
            ):
                secret_files.append(os.path.join(current, filename))
        if examined > max_entries:
            raise SandboxUnavailable(
                "safe workspace secret scan exceeded its entry budget; narrow the workspace"
            )
    git_config = os.path.join(root, ".git", "config")
    if os.path.isfile(git_config):
        secret_files.append(git_config)
    return secret_directories, secret_files


_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|passwd|authorization|cookie)"
    r"(\s*[:=]\s*)([^,;\r\n]+)"
)
_JSON_SECRET_RE = re.compile(
    r'''(?i)(["'](?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|passwd|authorization|cookie)["']\s*:\s*)'''
    r'''(?P<quote>["'])(.*?)(?P=quote)'''
)
_PEM_SECRET_RE = re.compile(
    r"-----BEGIN [^-]*(?:PRIVATE KEY|CERTIFICATE)-----.*?-----END [^-]+-----",
    re.DOTALL,
)
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_SERVICE_TOKEN_RE = re.compile(
    r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
    r"|\bAIza[A-Za-z0-9_-]{24,}\b"
    r"|\bxox(?:a|b|p|r|s)-[A-Za-z0-9-]{16,}\b"
)


def redact_sensitive_output(value: str) -> tuple[str, int]:
    """Redact recognizable credentials before process output reaches a model."""

    text = str(value or "")
    count = 0

    def json_assignment(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        quote = match.group("quote")
        return f"{match.group(1)}{quote}<redacted>{quote}"

    text = _JSON_SECRET_RE.sub(json_assignment, text)

    def assignment(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}{match.group(2)}<redacted>"

    text = _ASSIGNMENT_SECRET_RE.sub(assignment, text)
    for expression in (_PEM_SECRET_RE, _AWS_KEY_RE, _JWT_RE, _SERVICE_TOKEN_RE):
        text, replaced = expression.subn("<redacted-sensitive-output>", text)
        count += replaced
    return text, count


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
    writable = _canonical_writable_directories(writable_paths)
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
    # A read-only host root is not a confidentiality boundary: service users
    # can often read application configuration below /etc or /var, and mounted
    # media can contain credentials. Replace those trees with empty ephemeral
    # views, then restore only the small system files needed for ordinary
    # language runtimes and certificate verification.
    for host_tree in ("/etc", "/var", "/srv", "/mnt", "/media"):
        if os.path.isdir(host_tree):
            argv.extend(("--tmpfs", host_tree))
    safe_system_paths = (
        "/etc/alternatives",
        "/etc/ssl/certs",
        "/etc/ca-certificates",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
        "/etc/ld.so.conf.d",
        "/etc/localtime",
        "/etc/timezone",
        "/etc/passwd",
        "/etc/group",
        "/etc/nsswitch.conf",
        "/etc/hosts",
        "/etc/resolv.conf",
    )
    for safe_path in safe_system_paths:
        if not os.path.exists(safe_path):
            continue
        argv.extend(_mkdir_targets(os.path.dirname(safe_path)))
        argv.extend(("--ro-bind", safe_path, safe_path))
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

    service_root = os.path.realpath(os.getcwd())
    service_mask_before_workspace = False
    service_mask_after_workspace = False
    if (
        service_root != os.path.sep
        and os.path.isdir(service_root)
        and service_root != canonical_cwd
        and not service_root.startswith(("/usr/", "/etc/", "/var/"))
    ):
        try:
            workspace_inside_service = (
                os.path.commonpath((canonical_cwd, service_root)) == service_root
            )
            service_inside_workspace = (
                os.path.commonpath((canonical_cwd, service_root)) == canonical_cwd
            )
            service_mask_before_workspace = workspace_inside_service
            service_mask_after_workspace = service_inside_workspace
            if not workspace_inside_service and not service_inside_workspace:
                service_mask_before_workspace = True
        except ValueError:
            service_mask_before_workspace = True

    if data_mask_before_workspace:
        argv.extend(_mkdir_targets(data_directory))
        argv.extend(("--tmpfs", data_directory))
    if service_mask_before_workspace:
        argv.extend(_mkdir_targets(service_root))
        argv.extend(("--tmpfs", service_root))
    cwd_writable = any(
        canonical_cwd == directory
        or os.path.commonpath((canonical_cwd, directory)) == directory
        for directory in writable
    )
    if not cwd_writable:
        argv.extend(_mkdir_targets(canonical_cwd))
        argv.extend(("--ro-bind", canonical_cwd, canonical_cwd))
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
    if service_mask_after_workspace:
        argv.extend(_mkdir_targets(service_root))
        argv.extend(("--tmpfs", service_root))

    secret_directories, secret_files = _workspace_secret_mounts(
        canonical_cwd,
        excluded_roots=(data_directory, service_root if service_mask_after_workspace else ""),
    )
    for directory in secret_directories:
        argv.extend(("--tmpfs", directory))
    for path in secret_files:
        argv.extend(("--ro-bind", "/dev/null", path))

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


def host_command_boundary(
    command: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    workspace_writable: bool,
    environment: Mapping[str, object],
    runtime_environment: Mapping[str, object] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Expose the host while independently overlaying the workspace read-only."""

    child_environment = {str(key): str(value) for key, value in environment.items()}
    child_environment.update(
        {str(key): str(value) for key, value in (runtime_environment or {}).items()}
    )
    if workspace_writable:
        return [str(part) for part in command], child_environment
    bwrap = shutil.which("bwrap")
    canonical_cwd = os.path.realpath(os.fspath(cwd))
    if not bwrap or os.name != "posix" or not os.path.isdir(canonical_cwd):
        raise SandboxUnavailable(
            "read-only host execution requires Bubblewrap; host fallback is disabled"
        )
    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--share-net",
        "--bind",
        "/",
        "/",
        "--ro-bind",
        canonical_cwd,
        canonical_cwd,
        "--proc",
        "/proc",
        "--dev-bind",
        "/dev",
        "/dev",
        "--chdir",
        canonical_cwd,
        "--",
        *[str(part) for part in command],
    ]
    return argv, child_environment


__all__ = [
    "SandboxCapability",
    "SandboxUnavailable",
    "minimal_environment",
    "host_command_boundary",
    "redact_sensitive_output",
    "sandbox_capability",
    "sandbox_command",
]
