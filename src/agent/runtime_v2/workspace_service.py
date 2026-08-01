"""Workspace-confined reads, search, staging, recovery, and revision control."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import shutil
import tempfile
import threading
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
        }
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._revisions: dict[str, int] = {}

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
        return f"{root_id}.{number}"

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
    ) -> Optional[dict[str, Any]]:
        if offset >= total:
            return None
        token = self._token(
            {
                "kind": kind,
                "revision": revision,
                "query": self._query_identity(kind, arguments),
                "offset": offset,
            }
        )
        return {"token": token, "offset": offset, "remaining": total - offset}

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
        files: list[dict[str, Any]] = []
        for current, directories, names in os.walk(base, followlinks=False):
            directories[:] = sorted(
                directory
                for directory in directories
                if directory.casefold() not in self.SKIP_DIRECTORIES
                and directory != self.INTERNAL_DIRECTORY
                and not os.path.islink(os.path.join(current, directory))
            )
            for name in sorted(names):
                path = os.path.join(current, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                relative = self.relative(canonical_root, path)
                base_relative = self.relative(base, path)
                if self._is_sensitive_relative(relative):
                    continue
                if not any(
                    self._path_matches(relative, pattern)
                    or self._path_matches(base_relative, pattern)
                    for pattern in patterns
                ):
                    continue
                if any(self._path_matches(relative, pattern) for pattern in excludes):
                    continue
                stat = os.stat(path, follow_symlinks=False)
                files.append(
                    {
                        "path": relative,
                        "size": stat.st_size,
                        "sha256": self.file_sha256(path),
                    }
                )
        files.sort(key=lambda item: item["path"])
        offset = self._continuation_offset(
            kind="find_files",
            root=canonical_root,
            revision=revision,
            arguments=arguments,
        )
        limit = max(1, min(int(arguments.get("max_results") or 200), 500))
        page = files[offset : offset + limit]
        continuation = self._next_continuation(
            kind="find_files",
            revision=revision,
            arguments=arguments,
            offset=offset + len(page),
            total=len(files),
        )
        return {
            "files": page,
            "workspace_revision": revision,
            "continuation": continuation,
            "backend": "python_walk",
        }

    @staticmethod
    def _rg_available() -> Optional[str]:
        return shutil.which("rg")

    def _ripgrep_matches(
        self,
        root: str,
        arguments: Mapping[str, Any],
        max_results: int,
    ) -> list[dict[str, Any]]:
        rg = self._rg_available()
        if not rg:
            raise FileNotFoundError("ripgrep unavailable")
        pattern = str(arguments.get("pattern") or "")
        argv = [
            rg,
            "--json",
            "--no-messages",
            "--sort",
            "path",
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
        search_path = self.resolve(root, str(arguments.get("path") or "."), allow_missing=False)
        argv.append(search_path)
        from .process_service import PROCESS_SERVICE

        completed = PROCESS_SERVICE.run_workspace_search(
            argv,
            root=root,
            timeout_seconds=30,
        )
        if completed["returncode"] not in (0, 1):
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
        return matches

    def _python_search_matches(
        self,
        root: str,
        arguments: Mapping[str, Any],
        max_results: int,
    ) -> list[dict[str, Any]]:
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
        paths: list[str]
        if os.path.isfile(search_path):
            paths = [search_path]
        else:
            paths = []
            for current, directories, names in os.walk(search_path, followlinks=False):
                directories[:] = sorted(
                    directory
                    for directory in directories
                    if directory.casefold() not in self.SKIP_DIRECTORIES
                    and directory != self.INTERNAL_DIRECTORY
                    and not os.path.islink(os.path.join(current, directory))
                )
                paths.extend(os.path.join(current, name) for name in sorted(names))
        for path in paths:
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
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    lines = handle.read().splitlines()
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
                matches.extend(emitted[index] for index in sorted(emitted))
            except OSError:
                continue
        return matches

    def search_text(self, root: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        canonical_root = self._root(root)
        revision = self.require_revision(
            canonical_root, arguments.get("expected_workspace_revision")
        )
        max_results = max(1, min(int(arguments.get("max_results") or 200), 500))
        try:
            matches = self._ripgrep_matches(canonical_root, arguments, max_results)
            backend = "ripgrep"
        except FileNotFoundError:
            matches = self._python_search_matches(canonical_root, arguments, max_results)
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
        continuation = self._next_continuation(
            kind="search_text",
            revision=revision,
            arguments=arguments,
            offset=offset + len(page),
            total=len(matches),
        )
        return {
            "matches": page,
            "workspace_revision": revision,
            "continuation": continuation,
            "backend": backend,
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
    ) -> tuple[dict[str, Optional[bytes]], list[dict[str, Any]]]:
        expanded: list[Mapping[str, Any]] = []
        for operation in operations:
            if operation.get("type") == "structured_patch":
                expanded.extend(
                    self._structured_changes(root, str(operation.get("patch_text") or ""))
                )
            else:
                expanded.append(operation)
        changes: dict[str, Optional[bytes]] = {}
        summaries: list[dict[str, Any]] = []
        for operation in expanded:
            kind = str(operation.get("type") or "")
            raw_path = str(operation.get("path") or "")
            if kind == "move":
                source_raw = str(operation.get("source") or raw_path)
                destination_raw = str(operation.get("destination") or "")
                source = self.resolve(root, source_raw, allow_missing=False)
                destination = self.resolve(root, destination_raw)
                if not os.path.isfile(source):
                    raise WorkspacePathError(f"move source is not a file: {source_raw}")
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
                summaries.append(
                    {"type": "move", "source": source_raw, "destination": destination_raw}
                )
                continue
            path = self.resolve(root, raw_path, allow_missing=kind in {"create", "write"})
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
            summaries.append(
                {
                    "type": kind,
                    "path": raw_path,
                    "before_sha256": self.sha256_bytes(old) if exists else "missing",
                    "after_sha256": self.sha256_bytes(new) if new is not None else "missing",
                    "diff": self._diff(raw_path, old, new or b""),
                }
            )
        return changes, summaries

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

    def patch_workspace(self, root: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        canonical_root = self._root(root)
        before_revision = self.require_revision(
            canonical_root, arguments.get("expected_workspace_revision")
        )
        operations = arguments.get("operations") or []
        if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)):
            raise WorkspaceError("patch_workspace operations must be an array")
        expected_hashes = arguments.get("expected_sha256") or {}
        if not isinstance(expected_hashes, Mapping):
            raise WorkspaceError("expected_sha256 must be a path-to-hash object")
        changes, summaries = self._plan_patch(
            canonical_root,
            [item for item in operations if isinstance(item, Mapping)],
            {str(key): str(value) for key, value in expected_hashes.items()},
        )
        if len(changes) != len({os.path.realpath(path) for path in changes}):
            raise WorkspaceConflict("patch contains overlapping canonical targets")
        if arguments.get("dry_run"):
            return {
                "summary": "Dry-run preview; no files were changed.",
                "operations": summaries,
                "workspace_revision": before_revision,
                "dry_run": True,
                "transaction": "preview_only",
                "backend": "workspace_service",
            }
        transaction_id = uuid.uuid4().hex
        journal_root = os.path.join(
            canonical_root, self.INTERNAL_DIRECTORY, "transactions", transaction_id
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
                existed = os.path.exists(path)
                backup = None
                if existed:
                    backup = os.path.join(backups, f"{index}.bin")
                    shutil.copyfile(path, backup)
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
                    staged[path] = staged_path
                manifest["changes"].append(
                    {
                        "path": relative,
                        "existed": existed,
                        "backup": os.path.relpath(backup, journal_root) if backup else None,
                        "delete": new is None,
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
                    if new is None:
                        os.unlink(path)
                    else:
                        os.replace(staged[path], path)
                        staged.pop(path, None)
                    self._fsync_directory(os.path.dirname(path))
                    committed.append(path)
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
                        if backup:
                            shutil.copyfile(backup, path)
                        elif os.path.exists(path):
                            os.unlink(path)
                        self._fsync_directory(os.path.dirname(path))
                    except OSError as rollback_error:
                        rollback_errors.append(f"{self.relative(canonical_root, path)}: {rollback_error}")
                manifest["state"] = "recovery_required" if rollback_errors else "rolled_back"
                manifest["rollback_errors"] = rollback_errors
                with open(manifest_path, "w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                if not rollback_errors:
                    shutil.rmtree(journal_root, ignore_errors=True)
                detail = (
                    f"; recovery journal retained at {self.relative(canonical_root, journal_root)}"
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
