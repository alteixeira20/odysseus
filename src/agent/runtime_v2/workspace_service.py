"""Workspace-confined reads, search, staging, recovery, and revision control."""

from __future__ import annotations

import base64
import difflib
import hashlib
import heapq
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence

from .internal_git import HARDENED_GIT


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
    _MAX_SEARCH_LINE_BYTES = 4 * 1024 * 1024
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
        """Hash non-executing Git identity plus bounded workspace contents."""

        material = hashlib.sha256()
        git_identity = HARDENED_GIT.inspect(root)
        material.update(b"git-metadata\0")
        material.update(git_identity.digest.encode("ascii"))
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
        return material.hexdigest(), git_identity.stable

    def transaction_parent(self, root: str) -> str:
        canonical = self._root(root)
        root_id = hashlib.sha256(
            json.dumps(
                self._filesystem_identity(canonical),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
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

    @staticmethod
    def _filesystem_identity(root: str) -> dict[str, Any]:
        info = os.stat(root, follow_symlinks=False)
        return {
            "root": os.path.realpath(root),
            "device": int(info.st_dev),
            "inode": int(info.st_ino),
        }

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
        total: Optional[int],
        cursor: Any = None,
        remaining: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        if total is not None and offset >= total:
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
            "remaining": (
                None
                if total is None and remaining is None
                else (total - offset if remaining is None else remaining)
            ),
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

    def _search_scan_cursor(
        self,
        *,
        revision: str,
        arguments: Mapping[str, Any],
    ) -> Optional[dict[str, Any]]:
        token = arguments.get("continuation")
        if not token:
            return None
        decoded = self._decode_token(str(token))
        if (
            decoded.get("kind") != "search_text"
            or decoded.get("revision") != revision
            or decoded.get("query")
            != self._query_identity("search_text", arguments)
        ):
            raise WorkspaceConflict(
                "continuation no longer matches stable search_text query or workspace revision"
            )
        cursor = decoded.get("cursor")
        if cursor is None:
            return None
        if not isinstance(cursor, Mapping):
            raise WorkspaceConflict("invalid search continuation cursor")
        try:
            return {
                "path": str(cursor.get("path") or ""),
                "offset": max(int(cursor.get("offset") or 0), 0),
                "line": max(int(cursor.get("line") or 0), 0),
            }
        except (TypeError, ValueError) as exc:
            raise WorkspaceConflict("invalid search continuation cursor") from exc

    def _iter_workspace_entries(self, base: str):
        """Yield entries in global lexical order with a bounded frontier.

        A depth-first walk is not globally lexical for prefix-collision paths
        such as ``a.txt`` and ``a/child.txt``.  A heap frontier makes the cursor
        order exactly match iteration order without materializing the tree.
        """

        frontier: list[tuple[str, str]] = []

        def push_children(path: str) -> None:
            try:
                entries = os.scandir(path)
            except OSError:
                return
            with entries:
                for entry in entries:
                    relative = os.path.relpath(entry.path, base).replace(os.sep, "/")
                    heapq.heappush(frontier, (relative, entry.path))

        push_children(base)
        while frontier:
            _, path = heapq.heappop(frontier)
            try:
                entry = os.stat(path, follow_symlinks=False)
                is_symlink = stat.S_ISLNK(entry.st_mode)
                is_directory = stat.S_ISDIR(entry.st_mode)
                is_file = stat.S_ISREG(entry.st_mode)
            except OSError:
                yield path, False
                continue
            yield path, bool(is_file and not is_symlink)
            if (
                is_directory
                and not is_symlink
                and os.path.basename(path).casefold() not in self.SKIP_DIRECTORIES
                and os.path.basename(path) != self.INTERNAL_DIRECTORY
            ):
                push_children(path)

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
                total=(
                    offset + len(files) + 1
                    if has_more
                    else None
                ),
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

    def _python_search_matches(
        self,
        root: str,
        arguments: Mapping[str, Any],
        max_results: int,
        *,
        scan_cursor: Optional[Mapping[str, Any]] = None,
    ) -> tuple[
        list[dict[str, Any]],
        bool,
        bool,
        int,
        int,
        Optional[dict[str, Any]],
    ]:
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
            paths = (
                path
                for path, is_file in self._iter_workspace_entries(search_path)
                if is_file
            )
        cursor_path = str((scan_cursor or {}).get("path") or "")
        cursor_offset = max(int((scan_cursor or {}).get("offset") or 0), 0)
        cursor_line = max(int((scan_cursor or {}).get("line") or 0), 0)
        last_cursor: Optional[dict[str, Any]] = (
            {"path": cursor_path, "offset": cursor_offset, "line": cursor_line}
            if cursor_path
            else None
        )
        for path in paths:
            relative = self.relative(root, path)
            if cursor_path and relative < cursor_path:
                continue
            if scanned >= scan_budget or time.monotonic() - started > timeout:
                budget_exhausted = True
                break
            scanned += 1
            if os.path.islink(path) or not os.path.isfile(path):
                continue
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
                start_offset = cursor_offset if relative == cursor_path else 0
                start_line = cursor_line if relative == cursor_path else 0
                with open(path, "rb") as handle:
                    handle.seek(start_offset)
                    raw = handle.read(remaining_bytes + 1)
                truncated_file = len(raw) > remaining_bytes
                raw = raw[:remaining_bytes]
                scanned_bytes += len(raw)
                decoded_lines = raw.decode("utf-8", errors="replace").splitlines()
                lines = decoded_lines
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
                            "line": start_line + current + 1,
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
                last_cursor = {
                    "path": relative,
                    "offset": start_offset + len(raw),
                    "line": start_line + len(decoded_lines),
                }
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
            last_cursor,
        )

    def _python_search_page(
        self,
        root: str,
        arguments: Mapping[str, Any],
        max_results: int,
        *,
        scan_cursor: Optional[Mapping[str, Any]],
    ) -> tuple[
        list[dict[str, Any]],
        bool,
        bool,
        int,
        int,
        Optional[dict[str, Any]],
    ]:
        """Scan a deterministic page without advancing past an unreturned match.

        This path is used whenever traversal/byte/time budgets can interrupt a
        search.  Its cursor identifies the next unread byte of the globally
        lexical file stream, so even an empty match page remains resumable.
        Context rows are deliberately omitted on interrupted pages; exact match
        continuation takes precedence over best-effort presentation context.
        """

        import re

        pattern = str(arguments.get("pattern") or "")
        flags = 0 if arguments.get("case_sensitive", True) else re.IGNORECASE
        expression = re.compile(
            re.escape(pattern) if arguments.get("fixed_string") else pattern,
            flags,
        )
        includes = arguments.get("include") or ["**/*"]
        excludes = arguments.get("exclude") or []
        if isinstance(includes, str):
            includes = [includes]
        if isinstance(excludes, str):
            excludes = [excludes]
        search_path = self.resolve(
            root,
            str(arguments.get("path") or "."),
            allow_missing=False,
        )
        paths: Any
        if os.path.isfile(search_path):
            paths = iter((search_path,))
        else:
            paths = (
                path
                for path, is_file in self._iter_workspace_entries(search_path)
                if is_file
            )
        scan_budget = max(
            1,
            min(int(arguments.get("max_scan_entries") or 20_000), 100_000),
        )
        byte_budget = min(
            max(int(arguments.get("max_scan_bytes") or 1_048_576), 65_536),
            4_194_304,
        )
        timeout = max(
            0.05,
            min(float(arguments.get("scan_timeout_seconds") or 2.0), 10.0),
        )
        cursor_path = str((scan_cursor or {}).get("path") or "")
        cursor_offset = max(int((scan_cursor or {}).get("offset") or 0), 0)
        cursor_line = max(int((scan_cursor or {}).get("line") or 0), 0)
        matches: list[dict[str, Any]] = []
        scanned_entries = 0
        scanned_bytes = 0
        started = time.monotonic()
        incomplete = False
        timed_out = False
        next_cursor: Optional[dict[str, Any]] = None

        for path in paths:
            relative = self.relative(root, path)
            if cursor_path and relative < cursor_path:
                continue
            start_offset = cursor_offset if relative == cursor_path else 0
            completed_lines = cursor_line if relative == cursor_path else 0
            try:
                file_size = os.stat(path, follow_symlinks=False).st_size
            except OSError:
                continue
            # An EOF cursor means this file is already fully consumed. Skip it
            # without charging the next page's entry budget; otherwise a
            # one-entry page can become permanently stuck on the prior file.
            if relative == cursor_path and start_offset >= file_size:
                continue
            if scanned_entries >= scan_budget:
                incomplete = True
                break
            if time.monotonic() - started > timeout:
                incomplete = True
                timed_out = True
                break
            scanned_entries += 1
            if (
                os.path.islink(path)
                or not os.path.isfile(path)
                or self._is_sensitive_relative(relative)
                or not any(self._path_matches(relative, item) for item in includes)
                or any(self._path_matches(relative, item) for item in excludes)
            ):
                next_cursor = {
                    "path": relative,
                    "offset": max(file_size, 0),
                    "line": completed_lines,
                }
                continue
            try:
                with open(path, "rb") as handle:
                    handle.seek(min(start_offset, file_size))
                    while True:
                        if len(matches) >= max_results:
                            incomplete = True
                            next_cursor = {
                                "path": relative,
                                "offset": handle.tell(),
                                "line": completed_lines,
                            }
                            break
                        if time.monotonic() - started > timeout:
                            incomplete = True
                            timed_out = True
                            next_cursor = {
                                "path": relative,
                                "offset": handle.tell(),
                                "line": completed_lines,
                            }
                            break
                        if scanned_bytes >= byte_budget:
                            incomplete = True
                            next_cursor = {
                                "path": relative,
                                "offset": handle.tell(),
                                "line": completed_lines,
                            }
                            break
                        raw = handle.readline(self._MAX_SEARCH_LINE_BYTES + 1)
                        if not raw:
                            next_cursor = {
                                "path": relative,
                                "offset": handle.tell(),
                                "line": completed_lines,
                            }
                            break
                        if (
                            len(raw) > self._MAX_SEARCH_LINE_BYTES
                            and not raw.endswith(b"\n")
                            and handle.tell() < file_size
                        ):
                            raise WorkspaceError(
                                "search line exceeds the 4 MiB deterministic scan limit"
                            )
                        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        match = expression.search(text)
                        if match is not None:
                            from src.process_sandbox import redact_sensitive_output

                            redacted, _ = redact_sensitive_output(text[:4096])
                            matches.append(
                                {
                                    "path": relative,
                                    "line": completed_lines + 1,
                                    "column": match.start() + 1,
                                    "text": redacted,
                                    "kind": "match",
                                }
                            )
                        scanned_bytes += len(raw)
                        completed_lines += 1
                        next_cursor = {
                            "path": relative,
                            "offset": handle.tell(),
                            "line": completed_lines,
                        }
            except OSError:
                continue
            if incomplete:
                break
        return (
            matches,
            incomplete,
            timed_out,
            scanned_entries,
            scanned_bytes,
            next_cursor if incomplete else None,
        )

    def search_text(self, root: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        canonical_root = self._root(root)
        revision = self.require_revision(
            canonical_root, arguments.get("expected_workspace_revision")
        )
        max_results = max(1, min(int(arguments.get("max_results") or 200), 500))
        scan_cursor = self._search_scan_cursor(
            revision=revision,
            arguments=arguments,
        )
        force_bounded = (
            arguments.get("max_scan_entries") is not None
            or scan_cursor is not None
        )
        if force_bounded:
            (
                matches,
                byte_budget_exhausted,
                timed_out,
                scanned_entries,
                scanned_bytes,
                next_scan_cursor,
            ) = self._python_search_page(
                canonical_root,
                arguments,
                max_results,
                scan_cursor=scan_cursor,
            )
            backend = "python_bounded"
        else:
            (
                matches,
                byte_budget_exhausted,
                timed_out,
                scanned_entries,
                scanned_bytes,
                next_scan_cursor,
            ) = self._python_search_matches(
                canonical_root,
                arguments,
                max_results,
                scan_cursor=None,
            )
            backend = "python_native"
            if byte_budget_exhausted or timed_out:
                (
                    matches,
                    byte_budget_exhausted,
                    timed_out,
                    scanned_entries,
                    scanned_bytes,
                    next_scan_cursor,
                ) = self._python_search_page(
                    canonical_root,
                    arguments,
                    max_results,
                    scan_cursor=None,
                )
                backend = "python_bounded"
        matches.sort(
            key=lambda item: (
                item["path"],
                item["line"],
                item["column"],
                0 if item["kind"] == "match" else 1,
            )
        )
        offset = (
            0
            if scan_cursor is not None
            else self._continuation_offset(
            kind="search_text",
            root=canonical_root,
            revision=revision,
            arguments=arguments,
            )
        )
        page = matches[offset : offset + max_results]
        incomplete_scan = bool(byte_budget_exhausted or timed_out)
        continuation = None
        if incomplete_scan:
            continuation = self._next_continuation(
                kind="search_text",
                revision=revision,
                arguments=arguments,
                offset=0,
                total=None,
                cursor=next_scan_cursor
                or scan_cursor
                or {"path": "", "offset": 0, "line": 0},
                remaining=None,
            )
        elif offset + len(page) < len(matches):
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
                manifest.get("version") != 2
                or str(manifest.get("transaction_id") or "") != transaction_id
                or os.path.realpath(str(manifest.get("root") or "")) != canonical_root
                or manifest.get("filesystem_identity")
                != self._filesystem_identity(canonical_root)
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

    def patch_workspace(
        self,
        root: str,
        arguments: Mapping[str, Any],
        *,
        effect_started_cb=None,
    ) -> dict[str, Any]:
        canonical_root = self._root(root)
        with self._root_lock(canonical_root):
            return self._patch_workspace_locked(
                canonical_root,
                arguments,
                effect_started_cb=effect_started_cb,
            )

    def _patch_workspace_locked(
        self,
        root: str,
        arguments: Mapping[str, Any],
        *,
        effect_started_cb=None,
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
            "version": 2,
            "transaction_id": transaction_id,
            "state": "prepared",
            "root": canonical_root,
            "filesystem_identity": self._filesystem_identity(canonical_root),
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
                boundary_started = False
                for path, new in changes.items():
                    self._assert_target_still_confined(canonical_root, path)
                    self._check_hash(
                        path,
                        before_hashes[path],
                        self.relative(canonical_root, path),
                    )
                    if not boundary_started and effect_started_cb is not None:
                        effect_started_cb()
                        boundary_started = True
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
