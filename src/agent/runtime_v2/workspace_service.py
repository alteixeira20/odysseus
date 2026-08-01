"""Workspace-confined reads, search, staging, recovery, and revision control."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence


class WorkspaceError(RuntimeError):
    code = "workspace_error"


class WorkspaceConflict(WorkspaceError):
    code = "workspace_conflict"


class WorkspaceRevisionConflict(WorkspaceConflict):
    code = "stale_workspace_revision"


class WorkspaceHashConflict(WorkspaceConflict):
    code = "stale_file_hash"


class WorkspacePathError(WorkspaceError):
    code = "invalid_workspace_path"


class WorkspaceService:
    """All migrated filesystem handlers enter through this service."""

    INTERNAL_DIRECTORY = ".odysseus-runtime"
    SKIP_DIRECTORIES = frozenset(
        {
            ".git",
            ".hg",
            ".svn",
            ".ssh",
            ".gnupg",
            ".aws",
            ".kube",
            ".docker",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
        }
    )
    SENSITIVE_BASENAMES = frozenset(
        {
            ".bashrc",
            ".bash_profile",
            ".bash_logout",
            ".zshrc",
            ".zprofile",
            ".zshenv",
            ".profile",
            ".netrc",
            ".gitconfig",
            "authorized_keys",
            "id_rsa",
            "id_ed25519",
            "id_ecdsa",
            "known_hosts",
            "credentials",
            "credentials.json",
            "service-account.json",
            ".npmrc",
            ".pypirc",
            ".git-credentials",
            ".envrc",
            "secrets.json",
            "secrets.yaml",
            "secrets.yml",
        }
    )
    _SENSITIVE_SEARCH_FILENAMES = frozenset({".env"})

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._revisions: dict[str, int] = {}
        self._root_locks: dict[str, threading.RLock] = {}
        configured_journal_base = os.environ.get("ODYSSEUS_RUNTIME_JOURNAL_DIR")
        if configured_journal_base:
            self._journal_bases = (
                os.path.realpath(configured_journal_base),
            )
        else:
            # A workspace may legitimately be rooted at /tmp (for example a
            # disposable checkout).  Keep several process-external candidates
            # so such a workspace can never forge its own recovery journal.
            xdg_state = os.environ.get("XDG_STATE_HOME") or os.path.join(
                os.path.expanduser("~"), ".local", "state"
            )
            self._journal_bases = tuple(
                dict.fromkeys(
                    os.path.realpath(path)
                    for path in (
                        os.path.join(xdg_state, "odysseus-runtime-v2-journals"),
                        "/var/tmp/odysseus-runtime-v2-journals",
                        os.path.join(
                            tempfile.gettempdir(),
                            "odysseus-runtime-v2-journals",
                        ),
                    )
                )
            )

    @staticmethod
    def _root(root: str | os.PathLike[str]) -> str:
        canonical = os.path.realpath(os.fspath(root))
        if not os.path.isabs(canonical) or not os.path.isdir(canonical):
            raise WorkspacePathError(f"invalid execution root: {canonical}")
        return canonical

    def revision(self, root: str | os.PathLike[str]) -> str:
        canonical = self._root(root)
        with self._lock:
            number = self._revisions.setdefault(canonical, 1)
        root_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        fingerprint, stable = self._workspace_fingerprint(canonical)
        strength = "strong" if stable else "partial"
        return f"{root_id}.{number}.{strength}.{fingerprint[:20]}"

    @staticmethod
    def revision_is_strong(revision: str) -> bool:
        return ".strong." in str(revision or "")

    def _root_lock(self, root: str) -> threading.RLock:
        canonical = self._root(root)
        with self._lock:
            return self._root_locks.setdefault(canonical, threading.RLock())

    def note_external_mutation(self, root: str) -> str:
        """Advance the process generation after a granted shell changed files."""

        canonical = self._root(root)
        return self._bump_revision(canonical)

    def _workspace_fingerprint(self, root: str) -> tuple[str, bool]:
        """Hash Git identity plus dirty-file content; bounded metadata fallback."""

        material = hashlib.sha256()
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    root,
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                    "--ignored=no",
                    "--",
                    ".",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            status_bytes = completed.stdout
            material.update(b"git\0")
            material.update(status_bytes)
            try:
                head = subprocess.run(
                    ["git", "-C", root, "rev-parse", "HEAD"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                    check=False,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
                )
            except (OSError, subprocess.SubprocessError):
                return material.hexdigest(), False
            material.update(head.stdout.strip())
            records = [item for item in status_bytes.split(b"\0") if item]
            if len(records) > 20_000 or len(status_bytes) > 4 * 1024 * 1024:
                return material.hexdigest(), False
            content_bytes = 0
            content_budget = 16 * 1024 * 1024
            for raw in records:
                text = raw.decode("utf-8", errors="surrogateescape")
                relative = text[3:] if len(text) > 3 else ""
                if not relative:
                    continue
                path = os.path.realpath(os.path.join(root, relative))
                try:
                    if os.path.commonpath((root, path)) != root:
                        continue
                    info = os.stat(path, follow_symlinks=False)
                except (OSError, ValueError):
                    material.update(relative.encode("utf-8", errors="surrogateescape"))
                    material.update(b"\0missing")
                    continue
                material.update(relative.encode("utf-8", errors="surrogateescape"))
                material.update(
                    f"\0{info.st_mode}:{info.st_size}:{info.st_mtime_ns}:{info.st_ino}".encode()
                )
                if stat.S_ISREG(info.st_mode):
                    sample_bytes = (
                        info.st_size
                        if info.st_size <= 4 * 1024 * 1024
                        else 128 * 1024
                    )
                    if content_bytes + sample_bytes > content_budget:
                        return material.hexdigest(), False
                    try:
                        with open(path, "rb") as handle:
                            if info.st_size <= 4 * 1024 * 1024:
                                material.update(handle.read())
                            else:
                                material.update(handle.read(64 * 1024))
                                handle.seek(max(info.st_size - 64 * 1024, 0))
                                material.update(handle.read(64 * 1024))
                        content_bytes += sample_bytes
                    except OSError:
                        return material.hexdigest(), False
            return material.hexdigest(), True

        examined = 0
        content_bytes = 0
        content_budget = 8 * 1024 * 1024
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = sorted(
                item
                for item in directories
                if item.casefold() not in self.SKIP_DIRECTORIES
                and item != self.INTERNAL_DIRECTORY
            )
            for name in sorted(directories + files):
                examined += 1
                if examined > 20_000:
                    return material.hexdigest(), False
                path = os.path.join(current, name)
                try:
                    info = os.stat(path, follow_symlinks=False)
                except OSError:
                    continue
                relative = os.path.relpath(path, root).replace(os.sep, "/")
                material.update(
                    f"{relative}\0{info.st_mode}:{info.st_size}:{info.st_mtime_ns}:{info.st_ino}\0".encode(
                        "utf-8", errors="surrogateescape"
                    )
                )
                if stat.S_ISREG(info.st_mode):
                    if content_bytes + info.st_size > content_budget:
                        return material.hexdigest(), False
                    try:
                        with open(path, "rb") as handle:
                            material.update(handle.read())
                        content_bytes += info.st_size
                    except OSError:
                        return material.hexdigest(), False
        return material.hexdigest(), True

    def transaction_parent(self, root: str) -> str:
        canonical = self._root(root)
        root_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        for journal_base in self._journal_bases:
            try:
                if os.path.commonpath((canonical, journal_base)) == canonical:
                    continue
            except ValueError:
                pass
            return os.path.join(journal_base, root_id, "transactions")
        raise WorkspacePathError(
            "no recovery-journal location is outside the selected workspace"
        )

    def _bump_revision(self, root: str) -> str:
        with self._lock:
            self._revisions[root] = self._revisions.get(root, 1) + 1
        return self.revision(root)

    def require_revision(self, root: str, expected: Optional[str]) -> str:
        current = self.revision(root)
        if expected and str(expected) != current:
            raise WorkspaceRevisionConflict(
                f"workspace revision changed: expected {expected}, current {current}"
            )
        return current

    @classmethod
    def _is_sensitive_relative(cls, relative_path: str) -> bool:
        parts = tuple(
            part.casefold()
            for part in str(relative_path).replace("\\", "/").split("/")
            if part not in {"", "."}
        )
        if not parts:
            return False
        if any(part in cls.SKIP_DIRECTORIES for part in parts):
            return True
        filename = parts[-1]
        return (
            filename in cls.SENSITIVE_BASENAMES
            or filename == ".env"
            or filename.startswith(".env.")
            or filename.endswith((".pem", ".key", ".p12", ".pfx"))
            or ("private" in filename and "key" in filename)
        )

    @classmethod
    def is_sensitive_path(cls, path: str) -> bool:
        """Return whether a path is excluded from migrated coding tools."""

        return cls._is_sensitive_relative(path)

    @staticmethod
    def _path_matches(relative_path: str, pattern: Any) -> bool:
        """Git-style path matching where a single ``*`` never crosses ``/``."""

        relative = str(relative_path).replace("\\", "/").lstrip("./")
        candidate = PurePosixPath(relative)
        normalized = str(pattern or "").replace("\\", "/").lstrip("./")
        if not normalized:
            return False
        if candidate.match(normalized):
            return True
        return normalized.startswith("**/") and candidate.match(normalized[3:])

    def resolve(self, root: str, relative_path: str, *, allow_missing: bool = True) -> str:
        canonical_root = self._root(root)
        raw = str(relative_path or ".").strip() or "."
        if os.path.isabs(raw):
            candidate = os.path.realpath(raw)
        else:
            candidate = os.path.realpath(os.path.join(canonical_root, raw))
        try:
            inside = os.path.commonpath((canonical_root, candidate)) == canonical_root
        except ValueError:
            inside = False
        if not inside:
            raise WorkspacePathError(f"path is outside the execution root: {raw}")
        relative = os.path.relpath(candidate, canonical_root).replace(os.sep, "/")
        if relative == self.INTERNAL_DIRECTORY or relative.startswith(
            self.INTERNAL_DIRECTORY + "/"
        ):
            raise WorkspacePathError("runtime transaction metadata is not a workspace artifact")
        if self._is_sensitive_relative(relative):
            raise WorkspacePathError(
                f"workspace path is sensitive or excluded from coding tools: {relative}"
            )
        if not allow_missing and not os.path.exists(candidate):
            raise WorkspacePathError(f"workspace path does not exist: {relative}")
        return candidate

    def relative(self, root: str, path: str) -> str:
        return os.path.relpath(path, self._root(root)).replace(os.sep, "/")

    def _assert_target_still_confined(self, root: str, path: str) -> None:
        """Recheck canonical identity immediately before a filesystem effect."""

        canonical_root = self._root(root)
        resolved = os.path.realpath(path)
        try:
            inside = os.path.commonpath((canonical_root, resolved)) == canonical_root
        except ValueError:
            inside = False
        if not inside or resolved != path:
            raise WorkspacePathError(
                "workspace target changed identity during the transaction"
            )
        relative = self.relative(canonical_root, path)
        if self._is_sensitive_relative(relative):
            raise WorkspacePathError(
                f"workspace target became sensitive during the transaction: {relative}"
            )

    @staticmethod
    def sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def file_sha256(self, path: str) -> str:
        if not os.path.exists(path):
            return "missing"
        if not os.path.isfile(path):
            raise WorkspacePathError(f"not a regular file: {path}")
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _check_hash(self, path: str, expected: Optional[str], label: str) -> None:
        if expected is None:
            return
        current = self.file_sha256(path)
        if str(expected) != current:
            raise WorkspaceHashConflict(
                f"file hash changed for {label}: expected {expected}, current {current}"
            )

    @staticmethod
    def _token(payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(encoded).decode().rstrip("=")

    @staticmethod
    def _decode_token(token: str) -> dict[str, Any]:
        try:
            padded = str(token) + "=" * (-len(str(token)) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode())
        except Exception as exc:
            raise WorkspaceConflict("invalid continuation token") from exc
        if not isinstance(value, dict):
            raise WorkspaceConflict("invalid continuation token")
        return value

    @staticmethod
    def _query_identity(kind: str, arguments: Mapping[str, Any]) -> str:
        stable = {
            key: value
            for key, value in arguments.items()
            if key not in {"continuation", "offset", "max_results", "max_output_chars"}
        }
        raw = json.dumps(
            {"kind": kind, "arguments": stable},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    def _continuation_offset(
        self,
        *,
        kind: str,
        root: str,
        revision: str,
        arguments: Mapping[str, Any],
    ) -> int:
        token = arguments.get("continuation")
        if not token:
            try:
                return max(int(arguments.get("offset") or 0), 0)
            except (TypeError, ValueError) as exc:
                raise WorkspaceConflict("invalid pagination offset") from exc
        decoded = self._decode_token(str(token))
        expected = {
            "kind": kind,
            "revision": revision,
            "query": self._query_identity(kind, arguments),
        }
        for key, value in expected.items():
            if decoded.get(key) != value:
                raise WorkspaceConflict(
                    f"continuation no longer matches stable {kind} query or workspace revision"
                )
        try:
            return max(int(decoded.get("offset", 0)), 0)
        except (TypeError, ValueError) as exc:
            raise WorkspaceConflict("invalid continuation offset") from exc

    def _next_continuation(
        self,
        *,
        kind: str,
        revision: str,
        arguments: Mapping[str, Any],
        offset: int,
        total: int,
        cursor: Optional[str] = None,
        remaining: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        if offset >= total:
            return None
        token = self._token(
            {
                "kind": kind,
                "revision": revision,
                "query": self._query_identity(kind, arguments),
                "offset": offset,
                "cursor": cursor,
            }
        )
        return {
            "token": token,
            "offset": offset,
            "remaining": total - offset if remaining is None else remaining,
        }

    def _continuation_cursor(
        self,
        *,
        kind: str,
        revision: str,
        arguments: Mapping[str, Any],
    ) -> Optional[str]:
        token = arguments.get("continuation")
        if not token:
            return None
        decoded = self._decode_token(str(token))
        if (
            decoded.get("kind") != kind
            or decoded.get("revision") != revision
            or decoded.get("query") != self._query_identity(kind, arguments)
        ):
            raise WorkspaceConflict(
                f"continuation no longer matches stable {kind} query or workspace revision"
            )
        cursor = decoded.get("cursor")
        return str(cursor) if cursor else None

    def _iter_workspace_entries(self, base: str):
        """Yield a bounded-consumer-friendly lexical depth-first view.

        Directories are yielded before their children so callers can charge
        traversal work to a scan budget without first materializing the tree.
        """

        def entries(path: str):
            try:
                return iter(sorted(os.scandir(path), key=lambda item: item.name))
            except OSError:
                return iter(())

        stack = [entries(base)]
        while stack:
            try:
                entry = next(stack[-1])
            except StopIteration:
                stack.pop()
                continue
            path = entry.path
            try:
                is_symlink = entry.is_symlink()
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                yield path, False
                continue
            yield path, bool(is_file and not is_symlink)
            if (
                is_directory
                and not is_symlink
                and entry.name.casefold() not in self.SKIP_DIRECTORIES
                and entry.name != self.INTERNAL_DIRECTORY
            ):
                stack.append(entries(path))

    def find_files(self, root: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        canonical_root = self._root(root)
        revision = self.require_revision(
            canonical_root, arguments.get("expected_workspace_revision")
        )
        base = self.resolve(canonical_root, str(arguments.get("path") or "."), allow_missing=False)
        if not os.path.isdir(base):
            raise WorkspacePathError("find_files path must be a directory")
        patterns = arguments.get("patterns") or ["**/*"]
        if isinstance(patterns, str):
            patterns = [patterns]
        excludes = arguments.get("exclude") or []
        if isinstance(excludes, str):
            excludes = [excludes]
        offset = self._continuation_offset(
            kind="find_files",
            root=canonical_root,
            revision=revision,
            arguments=arguments,
        )
        cursor = self._continuation_cursor(
            kind="find_files",
            revision=revision,
            arguments=arguments,
        )
        limit = max(1, min(int(arguments.get("max_results") or 200), 500))
        scan_budget = max(
            1,
            min(int(arguments.get("max_scan_entries") or 20_000), 100_000),
        )
        time_budget = max(
            0.05,
            min(float(arguments.get("scan_timeout_seconds") or 2.0), 10.0),
        )
        started = time.monotonic()
        files: list[dict[str, Any]] = []
        scanned = 0
        matched = offset if cursor else 0
        budget_exhausted = False
        has_more = False
        last_cursor = cursor
        stop = False
        for path, is_file in self._iter_workspace_entries(base):
            relative = self.relative(canonical_root, path)
            if cursor and relative <= cursor:
                continue
            if scanned >= scan_budget or time.monotonic() - started > time_budget:
                budget_exhausted = True
                stop = True
                break
            scanned += 1
            if not is_file:
                last_cursor = relative
                continue
            base_relative = self.relative(base, path)
            if self._is_sensitive_relative(relative):
                last_cursor = relative
                continue
            if not any(
                self._path_matches(relative, pattern)
                or self._path_matches(base_relative, pattern)
                for pattern in patterns
            ):
                last_cursor = relative
                continue
            if any(self._path_matches(relative, pattern) for pattern in excludes):
                last_cursor = relative
                continue
            if not cursor and matched < offset:
                matched += 1
                last_cursor = relative
                continue
            if len(files) >= limit:
                has_more = True
                stop = True
                break
            file_stat = os.stat(path, follow_symlinks=False)
            files.append(
                {
                    "path": relative,
                    "size": file_stat.st_size,
                    "sha256": self.file_sha256(path),
                }
            )
            last_cursor = relative
        scan_complete = not stop
        continuation = None
        if has_more or budget_exhausted:
            continuation = self._next_continuation(
                kind="find_files",
                revision=revision,
                arguments=arguments,
                offset=offset + len(files),
                total=offset + len(files) + (1 if has_more else 0),
                cursor=last_cursor,
                remaining=None if budget_exhausted else 1,
            )
        return {
            "files": files,
            "workspace_revision": revision,
            "continuation": continuation,
            "backend": "python_walk",
            "scan_complete": scan_complete,
            "budget_exhausted": budget_exhausted,
            "scanned_entries": scanned,
            "scan_budget_entries": scan_budget,
        }

    @staticmethod
    def _rg_available() -> Optional[str]:
        return shutil.which("rg")

    def _ripgrep_matches(
        self,
        root: str,
        arguments: Mapping[str, Any],
        max_results: int,
    ) -> tuple[list[dict[str, Any]], bool, bool, Optional[int], int]:
        rg = self._rg_available()
        if not rg:
            raise FileNotFoundError("ripgrep unavailable")
        pattern = str(arguments.get("pattern") or "")
        argv = [
            rg,
            "--json",
            "--no-messages",
            "--max-columns",
            "4096",
            "--max-columns-preview",
        ]
        argv.append("--fixed-strings" if arguments.get("fixed_string") else "--regexp")
        argv.append(pattern)
        argv.extend(("--context", str(max(0, min(int(arguments.get("context_lines") or 0), 20)))))
        case_sensitive = arguments.get("case_sensitive")
        if case_sensitive is False:
            argv.append("--ignore-case")
        elif case_sensitive is True:
            argv.append("--case-sensitive")
        includes = arguments.get("include") or []
        excludes = arguments.get("exclude") or []
        if isinstance(includes, str):
            includes = [includes]
        if isinstance(excludes, str):
            excludes = [excludes]
        for include in includes:
            argv.extend(("--glob", str(include)))
        for exclude in excludes:
            argv.extend(("--glob", "!" + str(exclude).lstrip("!")))
        argv.extend(("--glob", f"!{self.INTERNAL_DIRECTORY}/**"))
        for directory in sorted(self.SKIP_DIRECTORIES):
            argv.extend(("--glob", f"!{directory}/**"))
            argv.extend(("--glob", f"!**/{directory}/**"))
        sensitive_names = set(self.SENSITIVE_BASENAMES) | set(
            self._SENSITIVE_SEARCH_FILENAMES
        )
        for filename in sorted(sensitive_names):
            argv.extend(("--glob", f"!{filename}"))
            argv.extend(("--glob", f"!**/{filename}"))
        for pattern in (
            ".env.*",
            "**/.env.*",
            "*.pem",
            "**/*.pem",
            "*.key",
            "**/*.key",
            "*.p12",
            "**/*.p12",
            "*.pfx",
            "**/*.pfx",
        ):
            argv.extend(("--glob", f"!{pattern}"))
        search_path = self.resolve(root, str(arguments.get("path") or "."), allow_missing=False)
        argv.append(search_path)
        from .process_service import PROCESS_SERVICE

        completed = PROCESS_SERVICE.run_workspace_search(
            argv,
            root=root,
            timeout_seconds=min(
                float(arguments.get("scan_timeout_seconds") or 2.0),
                10.0,
            ),
            max_output_bytes=min(
                max(int(arguments.get("max_scan_bytes") or 1_048_576), 65_536),
                4_194_304,
            ),
        )
        if completed["returncode"] not in (0, 1) and not (
            completed.get("budget_exhausted") or completed.get("timed_out")
        ):
            detail = completed["stderr"].decode("utf-8", errors="replace")[:500]
            raise WorkspaceError(
                f"ripgrep failed: {detail or completed['returncode']}"
            )
        matches: list[dict[str, Any]] = []
        for raw_line in completed["stdout"].splitlines():
            try:
                event = json.loads(raw_line)
            except (TypeError, ValueError):
                continue
            event_type = event.get("type")
            if event_type not in {"match", "context"}:
                continue
            data = event.get("data") or {}
            path_text = ((data.get("path") or {}).get("text") or "")
            if not path_text:
                continue
            absolute = path_text if os.path.isabs(path_text) else os.path.join(root, path_text)
            try:
                relative = self.relative(root, self.resolve(root, absolute, allow_missing=False))
            except WorkspaceError:
                continue
            line_text = str((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
            from src.process_sandbox import redact_sensitive_output

            line_text, _ = redact_sensitive_output(line_text)
            submatches = data.get("submatches") or []
            column = int(submatches[0].get("start", 0)) + 1 if submatches else 1
            matches.append(
                {
                    "path": relative,
                    "line": int(data.get("line_number") or 0),
                    "column": column,
                    "text": line_text,
                    "kind": event_type,
                }
            )
        return (
            matches,
            bool(completed.get("budget_exhausted")),
            bool(completed.get("timed_out")),
            None,
            len(completed["stdout"]),
        )

    def _python_search_matches(
        self,
        root: str,
        arguments: Mapping[str, Any],
        max_results: int,
    ) -> tuple[list[dict[str, Any]], bool, bool, int, int]:
        import re

        pattern = str(arguments.get("pattern") or "")
        flags = 0 if arguments.get("case_sensitive", True) else re.IGNORECASE
        expression = re.compile(re.escape(pattern) if arguments.get("fixed_string") else pattern, flags)
        includes = arguments.get("include") or ["**/*"]
        excludes = arguments.get("exclude") or []
        if isinstance(includes, str):
            includes = [includes]
        if isinstance(excludes, str):
            excludes = [excludes]
        search_path = self.resolve(root, str(arguments.get("path") or "."), allow_missing=False)
        context_lines = max(
            0,
            min(int(arguments.get("context_lines") or 0), 20),
        )
        matches: list[dict[str, Any]] = []
        scan_budget = max(
            1,
            min(int(arguments.get("max_scan_entries") or 20_000), 100_000),
        )
        timeout = min(float(arguments.get("scan_timeout_seconds") or 2.0), 10.0)
        started = time.monotonic()
        scanned = 0
        scanned_bytes = 0
        byte_budget = min(
            max(int(arguments.get("max_scan_bytes") or 1_048_576), 65_536),
            4_194_304,
        )
        budget_exhausted = False
        paths: Any
        if os.path.isfile(search_path):
            paths = iter((search_path,))
        else:
            def iter_paths():
                for current, directories, names in os.walk(
                    search_path, followlinks=False
                ):
                    directories[:] = sorted(
                        directory
                        for directory in directories
                        if directory.casefold() not in self.SKIP_DIRECTORIES
                        and directory != self.INTERNAL_DIRECTORY
                        and not os.path.islink(os.path.join(current, directory))
                    )
                    for name in sorted(names):
                        yield os.path.join(current, name)

            paths = iter_paths()
        for path in paths:
            if scanned >= scan_budget or time.monotonic() - started > timeout:
                budget_exhausted = True
                break
            scanned += 1
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            relative = self.relative(root, path)
            if self._is_sensitive_relative(relative):
                continue
            if not any(
                self._path_matches(relative, item)
                for item in includes
            ):
                continue
            if any(self._path_matches(relative, item) for item in excludes):
                continue
            try:
                remaining_bytes = byte_budget - scanned_bytes
                if remaining_bytes <= 0:
                    budget_exhausted = True
                    break
                with open(path, "rb") as handle:
                    raw = handle.read(remaining_bytes + 1)
                truncated_file = len(raw) > remaining_bytes
                raw = raw[:remaining_bytes]
                scanned_bytes += len(raw)
                lines = raw.decode("utf-8", errors="replace").splitlines()
                matched = {
                    index: expression.search(line)
                    for index, line in enumerate(lines)
                }
                matched = {
                    index: match
                    for index, match in matched.items()
                    if match is not None
                }
                emitted: dict[int, dict[str, Any]] = {}
                for index, match in matched.items():
                    start = max(index - context_lines, 0)
                    end = min(index + context_lines + 1, len(lines))
                    for current in range(start, end):
                        is_match = current in matched
                        current_match = matched.get(current)
                        emitted[current] = {
                            "path": relative,
                            "line": current + 1,
                            "column": (
                                current_match.start() + 1
                                if current_match is not None
                                else 1
                            ),
                            "text": lines[current][:4096],
                            "kind": "match" if is_match else "context",
                        }
                from src.process_sandbox import redact_sensitive_output

                for item in emitted.values():
                    item["text"], _ = redact_sensitive_output(item["text"])
                matches.extend(emitted[index] for index in sorted(emitted))
                if truncated_file:
                    budget_exhausted = True
                    break
            except OSError:
                continue
        return (
            matches,
            budget_exhausted,
            time.monotonic() - started > timeout,
            scanned,
            scanned_bytes,
        )

    def search_text(self, root: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        canonical_root = self._root(root)
        revision = self.require_revision(
            canonical_root, arguments.get("expected_workspace_revision")
        )
        max_results = max(1, min(int(arguments.get("max_results") or 200), 500))
        try:
            if arguments.get("max_scan_entries") is not None:
                raise FileNotFoundError("entry budget requires bounded walker")
            (
                matches,
                byte_budget_exhausted,
                timed_out,
                scanned_entries,
                scanned_bytes,
            ) = self._ripgrep_matches(
                canonical_root, arguments, max_results
            )
            backend = "ripgrep"
        except FileNotFoundError:
            (
                matches,
                byte_budget_exhausted,
                timed_out,
                scanned_entries,
                scanned_bytes,
            ) = self._python_search_matches(
                canonical_root, arguments, max_results
            )
            backend = "python_fallback"
        matches.sort(
            key=lambda item: (
                item["path"],
                item["line"],
                item["column"],
                0 if item["kind"] == "match" else 1,
            )
        )
        offset = self._continuation_offset(
            kind="search_text",
            root=canonical_root,
            revision=revision,
            arguments=arguments,
        )
        page = matches[offset : offset + max_results]
        incomplete_scan = bool(byte_budget_exhausted or timed_out)
        continuation = None
        if offset + len(page) < len(matches):
            continuation = self._next_continuation(
                kind="search_text",
                revision=revision,
                arguments=arguments,
                offset=offset + len(page),
                total=max(len(matches), offset + len(page) + 1),
            )
        return {
            "matches": page,
            "workspace_revision": revision,
            "continuation": continuation,
            "backend": backend,
            "scan_complete": not incomplete_scan,
            "budget_exhausted": bool(byte_budget_exhausted),
            "timed_out": bool(timed_out),
            "scanned_entries": scanned_entries,
            "scanned_bytes": scanned_bytes,
            "scan_output_budget_bytes": min(
                max(int(arguments.get("max_scan_bytes") or 1_048_576), 65_536),
                4_194_304,
            ),
        }

    def read_files(self, root: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        canonical_root = self._root(root)
        revision = self.require_revision(
            canonical_root, arguments.get("expected_workspace_revision")
        )
        requests = arguments.get("requests") or []
        if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)):
            raise WorkspaceError("read_files requests must be an array")
        budget = max(1, min(int(arguments.get("max_output_chars") or 40_000), 200_000))
        used = 0
        files: list[dict[str, Any]] = []
        next_requests: list[dict[str, Any]] = []
        for request in requests:
            if not isinstance(request, Mapping):
                files.append({"status": "error", "error": "invalid request object"})
                continue
            raw_path = str(request.get("path") or "")
            try:
                path = self.resolve(canonical_root, raw_path, allow_missing=False)
                if not os.path.isfile(path):
                    raise WorkspacePathError("not a regular file")
                expected_hash = request.get("expected_sha256")
                self._check_hash(path, str(expected_hash) if expected_hash else None, raw_path)
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    lines = handle.read().splitlines(keepends=True)
                start = max(int(request.get("start_line") or 1), 1)
                end = min(int(request.get("end_line") or len(lines)), len(lines))
                selected: list[dict[str, Any]] = []
                cursor = start
                while cursor <= end:
                    raw_line = lines[cursor - 1]
                    text = raw_line.rstrip("\r\n")
                    line_ending = raw_line[len(text):]
                    cost = len(text) + len(str(cursor)) + 4
                    if used + cost > budget:
                        next_requests.append(
                            {
                                "path": self.relative(canonical_root, path),
                                "start_line": cursor,
                                "end_line": end,
                                "expected_sha256": self.file_sha256(path),
                            }
                        )
                        break
                    selected.append(
                        {
                            "line": cursor,
                            "text": text,
                            "line_ending": line_ending,
                        }
                    )
                    used += cost
                    cursor += 1
                files.append(
                    {
                        "status": "success",
                        "path": self.relative(canonical_root, path),
                        "sha256": self.file_sha256(path),
                        "start_line": start,
                        "end_line": selected[-1]["line"] if selected else start - 1,
                        "total_lines": len(lines),
                        "lines": selected,
                    }
                )
            except WorkspaceError as exc:
                files.append(
                    {
                        "status": "error",
                        "path": raw_path,
                        "error": str(exc),
                        "error_code": getattr(exc, "code", "workspace_error"),
                    }
                )
            except OSError as exc:
                files.append(
                    {
                        "status": "error",
                        "path": raw_path,
                        "error": str(exc),
                        "error_code": "file_read_error",
                    }
                )
        continuation = None
        if next_requests:
            continuation = {
                "requests": next_requests,
                "workspace_revision": revision,
            }
        rendered = "\n\n".join(
            (
                f"## {item['path']}\n"
                + "\n".join(f"{line['line']}: {line['text']}" for line in item["lines"])
                if item.get("status") == "success"
                else f"## {item.get('path', '<invalid>')}\nERROR: {item.get('error')}"
            )
            for item in files
        )
        return {
            "files": files,
            "text": rendered,
            "workspace_revision": revision,
            "output_chars": used,
            "continuation": continuation,
            "backend": "workspace_service",
        }

    @staticmethod
    def _diff(label: str, old: bytes, new: bytes) -> str:
        old_text = old.decode("utf-8", errors="replace").splitlines(keepends=True)
        new_text = new.decode("utf-8", errors="replace").splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(old_text, new_text, fromfile=f"a/{label}", tofile=f"b/{label}")
        )

    def _structured_changes(
        self,
        root: str,
        patch_text: str,
    ) -> list[dict[str, Any]]:
        from src.agent_tools.filesystem_tools import (
            _apply_patch_hunks,
            _parse_agent_patch,
        )

        changes: list[dict[str, Any]] = []
        for operation in _parse_agent_patch(patch_text):
            path = str(operation["path"])
            if operation["kind"] == "add":
                changes.append({"type": "create", "path": path, "content": operation["content"]})
            elif operation["kind"] == "delete":
                changes.append({"type": "delete", "path": path})
            else:
                absolute = self.resolve(root, path, allow_missing=False)
                with open(absolute, "r", encoding="utf-8", errors="replace") as handle:
                    original = handle.read()
                updated = _apply_patch_hunks(original, operation["hunks"], path)
                changes.append({"type": "write", "path": path, "content": updated})
        return changes

    def _plan_patch(
        self,
        root: str,
        operations: Sequence[Mapping[str, Any]],
        expected_hashes: Mapping[str, str],
    ) -> tuple[
        dict[str, Optional[bytes]],
        dict[str, int],
        dict[str, str],
        dict[str, Optional[str]],
        list[dict[str, Any]],
    ]:
        expanded: list[Mapping[str, Any]] = []
        for operation in operations:
            if operation.get("type") == "structured_patch":
                expanded.extend(
                    self._structured_changes(root, str(operation.get("patch_text") or ""))
                )
            else:
                expanded.append(operation)
        changes: dict[str, Optional[bytes]] = {}
        modes: dict[str, int] = {}
        before_hashes: dict[str, str] = {}
        metadata_sources: dict[str, Optional[str]] = {}
        claimed_targets: set[str] = set()
        summaries: list[dict[str, Any]] = []
        for operation in expanded:
            kind = str(operation.get("type") or "")
            raw_path = str(operation.get("path") or "")
            if kind == "move":
                source_raw = str(operation.get("source") or raw_path)
                destination_raw = str(operation.get("destination") or "")
                source = self.resolve(root, source_raw, allow_missing=False)
                destination = self.resolve(root, destination_raw)
                for target, label in (
                    (source, source_raw),
                    (destination, destination_raw),
                ):
                    canonical_target = os.path.realpath(target)
                    if canonical_target in claimed_targets:
                        raise WorkspaceConflict(
                            f"patch contains duplicate target: {label}"
                        )
                    claimed_targets.add(canonical_target)
                if not os.path.isfile(source):
                    raise WorkspacePathError(f"move source is not a file: {source_raw}")
                if os.path.exists(destination):
                    raise WorkspaceConflict(
                        f"move destination already exists: {destination_raw}"
                    )
                self._check_hash(
                    source,
                    str(operation.get("expected_sha256") or expected_hashes.get(source_raw))
                    if operation.get("expected_sha256") or expected_hashes.get(source_raw)
                    else None,
                    source_raw,
                )
                with open(source, "rb") as handle:
                    data = handle.read()
                changes[source] = None
                changes[destination] = data
                before_hashes[source] = self.file_sha256(source)
                before_hashes[destination] = "missing"
                metadata_sources[source] = source
                metadata_sources[destination] = source
                modes[destination] = stat.S_IMODE(
                    os.stat(source, follow_symlinks=False).st_mode
                )
                summaries.append(
                    {"type": "move", "source": source_raw, "destination": destination_raw}
                )
                continue
            path = self.resolve(root, raw_path, allow_missing=kind in {"create", "write"})
            canonical_target = os.path.realpath(path)
            if canonical_target in claimed_targets:
                raise WorkspaceConflict(
                    f"patch contains duplicate target: {raw_path}"
                )
            claimed_targets.add(canonical_target)
            expected = operation.get("expected_sha256") or expected_hashes.get(raw_path)
            self._check_hash(path, str(expected) if expected is not None else None, raw_path)
            exists = os.path.exists(path)
            old = b""
            if exists:
                if not os.path.isfile(path):
                    raise WorkspacePathError(f"not a regular file: {raw_path}")
                with open(path, "rb") as handle:
                    old = handle.read()
            if kind == "create":
                if exists:
                    raise WorkspaceConflict(f"create target already exists: {raw_path}")
                new = str(operation.get("content") or "").encode("utf-8")
            elif kind == "write":
                new = str(operation.get("content") or "").encode("utf-8")
            elif kind == "replace":
                if not exists:
                    raise WorkspaceConflict(f"replace target is missing: {raw_path}")
                old_text = old.decode("utf-8", errors="strict")
                needle = str(operation.get("old") or operation.get("old_string") or "")
                replacement = str(operation.get("new") or operation.get("new_string") or "")
                count = old_text.count(needle)
                if not needle or count == 0:
                    raise WorkspaceConflict(f"exact replacement text not found: {raw_path}")
                replace_all = bool(operation.get("replace_all"))
                if count > 1 and not replace_all:
                    raise WorkspaceConflict(
                        f"exact replacement is ambiguous ({count} matches): {raw_path}"
                    )
                new = old_text.replace(needle, replacement, -1 if replace_all else 1).encode("utf-8")
            elif kind == "delete":
                if not exists:
                    raise WorkspaceConflict(f"delete target is missing: {raw_path}")
                new = None
            else:
                raise WorkspaceError(f"unsupported patch operation: {kind}")
            changes[path] = new
            before_hashes[path] = self.sha256_bytes(old) if exists else "missing"
            metadata_sources[path] = path if exists else None
            if new is not None:
                modes[path] = (
                    stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
                    if exists
                    else 0o644
                )
            summaries.append(
                {
                    "type": kind,
                    "path": raw_path,
                    "before_sha256": self.sha256_bytes(old) if exists else "missing",
                    "after_sha256": self.sha256_bytes(new) if new is not None else "missing",
                    "diff": self._diff(raw_path, old, new or b""),
                }
            )
        return changes, modes, before_hashes, metadata_sources, summaries

    @staticmethod
    def _fsync_directory(path: str) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def recover_transactions(self, root: str) -> list[dict[str, Any]]:
        canonical_root = self._root(root)
        with self._root_lock(canonical_root):
            return self._recover_transactions_locked(canonical_root)

    def _recover_transactions_locked(self, root: str) -> list[dict[str, Any]]:
        """Recover prepared journals and clean completed journals.

        A manifest is fsynced before the first target replacement. If the
        process stops after that point, the next request restores every
        original from the journal before taking its immutable workspace
        revision. A manifest marked committed is only stale cleanup metadata;
        its already-completed changes are preserved.
        """

        canonical_root = self._root(root)
        transaction_parent = self.transaction_parent(canonical_root)
        if not os.path.isdir(transaction_parent):
            return []
        recovered: list[dict[str, Any]] = []
        restored_any = False
        for transaction_id in sorted(os.listdir(transaction_parent)):
            journal_root = os.path.realpath(
                os.path.join(transaction_parent, transaction_id)
            )
            try:
                if os.path.commonpath((transaction_parent, journal_root)) != transaction_parent:
                    raise WorkspaceError("transaction journal escaped the runtime directory")
            except ValueError as exc:
                raise WorkspaceError("invalid transaction journal path") from exc
            if not os.path.isdir(journal_root):
                continue
            manifest_path = os.path.join(journal_root, "manifest.json")
            try:
                with open(manifest_path, "r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
            except (OSError, ValueError, TypeError) as exc:
                raise WorkspaceError(
                    f"cannot recover transaction {transaction_id}: invalid manifest"
                ) from exc
            if not isinstance(manifest, Mapping):
                raise WorkspaceError(
                    f"cannot recover transaction {transaction_id}: invalid manifest"
                )
            if (
                manifest.get("version") != 1
                or str(manifest.get("transaction_id") or "") != transaction_id
                or os.path.realpath(str(manifest.get("root") or "")) != canonical_root
            ):
                raise WorkspaceError(
                    f"cannot recover transaction {transaction_id}: manifest identity mismatch"
                )
            state = str(manifest.get("state") or "")
            if state in {"committed", "rolled_back", "recovered"}:
                shutil.rmtree(journal_root)
                recovered.append(
                    {"transaction_id": transaction_id, "action": "cleaned", "state": state}
                )
                continue
            if state not in {"prepared", "recovery_required"}:
                raise WorkspaceError(
                    f"cannot recover transaction {transaction_id}: unknown state {state!r}"
                )
            changes = manifest.get("changes")
            if not isinstance(changes, list):
                raise WorkspaceError(
                    f"cannot recover transaction {transaction_id}: invalid change list"
                )
            errors: list[str] = []
            for change in reversed(changes):
                if not isinstance(change, Mapping):
                    errors.append("invalid change entry")
                    continue
                relative = str(change.get("path") or "")
                try:
                    target = self.resolve(canonical_root, relative)
                    if bool(change.get("existed")):
                        backup_relative = str(change.get("backup") or "")
                        backup = os.path.realpath(
                            os.path.join(journal_root, backup_relative)
                        )
                        if (
                            not backup_relative
                            or os.path.commonpath((journal_root, backup)) != journal_root
                            or not os.path.isfile(backup)
                        ):
                            raise WorkspaceError("journal backup is missing or invalid")
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        shutil.copy2(backup, target)
                        stored_mode = change.get("mode")
                        if stored_mode is not None:
                            os.chmod(target, int(stored_mode))
                    elif os.path.exists(target):
                        if not os.path.isfile(target) and not os.path.islink(target):
                            raise WorkspaceError("recovery target is not a file")
                        os.unlink(target)
                    self._fsync_directory(os.path.dirname(target))
                    restored_any = True
                except (OSError, ValueError, WorkspaceError) as exc:
                    errors.append(f"{relative or '<missing>'}: {exc}")
            if errors:
                manifest["state"] = "recovery_required"
                manifest["rollback_errors"] = errors
                with open(manifest_path, "w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                raise WorkspaceError(
                    f"transaction {transaction_id} still requires recovery: {'; '.join(errors)}"
                )
            manifest["state"] = "recovered"
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            shutil.rmtree(journal_root)
            recovered.append(
                {"transaction_id": transaction_id, "action": "restored", "state": state}
            )
        if restored_any:
            self._bump_revision(canonical_root)
        for directory in (
            transaction_parent,
            os.path.dirname(transaction_parent),
        ):
            try:
                os.rmdir(directory)
            except OSError:
                pass
        return recovered

    def patch_workspace(self, root: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        canonical_root = self._root(root)
        with self._root_lock(canonical_root):
            return self._patch_workspace_locked(canonical_root, arguments)

    def _patch_workspace_locked(
        self,
        root: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        canonical_root = self._root(root)
        before_revision = self.require_revision(
            canonical_root, arguments.get("expected_workspace_revision")
        )
        if not self.revision_is_strong(before_revision):
            raise WorkspaceRevisionConflict(
                "workspace identity exceeded its safety budget; narrow or clean the workspace before mutation"
            )
        operations = arguments.get("operations") or []
        if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)):
            raise WorkspaceError("patch_workspace operations must be an array")
        expected_hashes = arguments.get("expected_sha256") or {}
        if not isinstance(expected_hashes, Mapping):
            raise WorkspaceError("expected_sha256 must be a path-to-hash object")
        (
            changes,
            planned_modes,
            before_hashes,
            metadata_sources,
            summaries,
        ) = self._plan_patch(
            canonical_root,
            [item for item in operations if isinstance(item, Mapping)],
            {str(key): str(value) for key, value in expected_hashes.items()},
        )
        if arguments.get("dry_run"):
            return {
                "summary": "Dry-run preview; no files were changed.",
                "operations": summaries,
                "workspace_revision": before_revision,
                "dry_run": True,
                "transaction": "preview_only",
                "backend": "workspace_service",
            }
        for path, expected in before_hashes.items():
            self._check_hash(path, expected, self.relative(canonical_root, path))
        transaction_id = uuid.uuid4().hex
        transaction_parent = self.transaction_parent(canonical_root)
        os.makedirs(transaction_parent, mode=0o700, exist_ok=True)
        for protected_directory in (
            os.path.dirname(os.path.dirname(transaction_parent)),
            os.path.dirname(transaction_parent),
            transaction_parent,
        ):
            try:
                os.chmod(protected_directory, 0o700)
            except OSError:
                pass
        journal_root = os.path.join(
            transaction_parent, transaction_id
        )
        backups = os.path.join(journal_root, "backups")
        os.makedirs(backups, mode=0o700, exist_ok=False)
        manifest_path = os.path.join(journal_root, "manifest.json")
        manifest: dict[str, Any] = {
            "version": 1,
            "transaction_id": transaction_id,
            "state": "prepared",
            "root": canonical_root,
            "changes": [],
        }
        staged: dict[str, str] = {}
        originals: dict[str, Optional[str]] = {}
        try:
            for index, (path, new) in enumerate(changes.items()):
                relative = self.relative(canonical_root, path)
                self._assert_target_still_confined(canonical_root, path)
                self._check_hash(path, before_hashes[path], relative)
                existed = os.path.exists(path)
                backup = None
                if existed:
                    backup = os.path.join(backups, f"{index}.bin")
                    shutil.copy2(path, backup)
                    with open(backup, "rb") as handle:
                        os.fsync(handle.fileno())
                originals[path] = backup
                staged_path = None
                if new is not None:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    descriptor, staged_path = tempfile.mkstemp(
                        prefix=".odysseus-stage-", dir=os.path.dirname(path)
                    )
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(new)
                        handle.flush()
                        os.fsync(handle.fileno())
                    metadata_source = metadata_sources.get(path)
                    if metadata_source and os.path.exists(metadata_source):
                        shutil.copystat(
                            metadata_source,
                            staged_path,
                            follow_symlinks=False,
                        )
                    os.chmod(staged_path, planned_modes.get(path, 0o644))
                    staged[path] = staged_path
                manifest["changes"].append(
                    {
                        "path": relative,
                        "existed": existed,
                        "backup": os.path.relpath(backup, journal_root) if backup else None,
                        "delete": new is None,
                        "mode": (
                            stat.S_IMODE(
                                os.stat(path, follow_symlinks=False).st_mode
                            )
                            if existed
                            else None
                        ),
                    }
                )
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_directory(journal_root)
            committed: list[str] = []
            try:
                for path, new in changes.items():
                    self._assert_target_still_confined(canonical_root, path)
                    self._check_hash(
                        path,
                        before_hashes[path],
                        self.relative(canonical_root, path),
                    )
                    if new is None:
                        os.unlink(path)
                    else:
                        os.replace(staged[path], path)
                        staged.pop(path, None)
                    committed.append(path)
                    self._fsync_directory(os.path.dirname(path))
                manifest["state"] = "committed"
                with open(manifest_path, "w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException as commit_error:
                rollback_errors: list[str] = []
                for path in reversed(committed):
                    backup = originals[path]
                    try:
                        self._assert_target_still_confined(canonical_root, path)
                        if backup:
                            shutil.copy2(backup, path)
                        elif os.path.exists(path):
                            os.unlink(path)
                        self._fsync_directory(os.path.dirname(path))
                    except (OSError, WorkspaceError) as rollback_error:
                        rollback_errors.append(f"{self.relative(canonical_root, path)}: {rollback_error}")
                manifest["state"] = "recovery_required" if rollback_errors else "rolled_back"
                manifest["rollback_errors"] = rollback_errors
                with open(manifest_path, "w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._fsync_directory(journal_root)
                if not rollback_errors:
                    shutil.rmtree(journal_root, ignore_errors=True)
                detail = (
                    f"; external recovery journal retained for transaction {transaction_id}"
                    if rollback_errors
                    else "; originals restored"
                )
                raise WorkspaceError(f"staged patch commit failed: {commit_error}{detail}") from commit_error
        finally:
            for staged_path in staged.values():
                try:
                    os.unlink(staged_path)
                except OSError:
                    pass
        after_revision = self._bump_revision(canonical_root)
        shutil.rmtree(journal_root, ignore_errors=True)
        self._fsync_directory(os.path.dirname(journal_root))
        transaction_parent = os.path.dirname(journal_root)
        runtime_parent = os.path.dirname(transaction_parent)
        for directory in (transaction_parent, runtime_parent):
            try:
                os.rmdir(directory)
            except OSError:
                pass
        return {
            "summary": f"Committed {len(changes)} staged workspace change(s).",
            "operations": summaries,
            "workspace_revision_before": before_revision,
            "workspace_revision": after_revision,
            "dry_run": False,
            "transaction": "journaled_staging_with_recovery",
            "transaction_id": transaction_id,
            "recovery": {
                "required": False,
                "journal_retained": False,
            },
            "backend": "workspace_service",
        }


WORKSPACE_SERVICE = WorkspaceService()
