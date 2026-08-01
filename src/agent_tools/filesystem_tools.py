import asyncio
import hashlib
import json
import os
import re
import difflib
import fnmatch
import shutil
import tempfile
from typing import Optional, Dict, Any, Tuple, List

from src.constants import MAX_READ_CHARS, MAX_DIFF_LINES, MAX_OUTPUT_CHARS

_CODENAV_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".cache", "site-packages", ".idea", ".tox",
})
_CODENAV_MAX_HITS = 200
_CODENAV_MAX_LINE = 400
_CODENAV_MAX_OFFSET = 10_000


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_snapshot(path: str) -> Dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        stat_result = os.stat(path, follow_symlinks=False)
        return {
            "exists": True,
            "data": data,
            "sha256": _digest(data),
            "mode": stat_result.st_mode & 0o777,
        }
    except FileNotFoundError:
        return {"exists": False, "data": b"", "sha256": None, "mode": 0o644}


def _check_expected_hash(snapshot: Dict[str, Any], expected: Optional[str], label: str) -> None:
    if expected is None:
        return
    expected = str(expected).strip().lower()
    actual = snapshot["sha256"] if snapshot["exists"] else "missing"
    if expected != actual:
        raise ValueError(
            f"{label}: content changed (expected sha256={expected}, actual={actual}); "
            "read the file again before overwriting it"
        )


def _verify_snapshot(path: str, snapshot: Dict[str, Any]) -> None:
    current = _read_snapshot(path)
    if current["exists"] != snapshot["exists"] or current["sha256"] != snapshot["sha256"]:
        actual = current["sha256"] if current["exists"] else "missing"
        expected = snapshot["sha256"] if snapshot["exists"] else "missing"
        raise ValueError(
            f"{path}: concurrent modification detected "
            f"(expected sha256={expected}, actual={actual})"
        )


def _fsync_directory(directory: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_bytes(path: str, data: bytes, mode: int) -> str:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, staged = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.ody-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, mode)
        return staged
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise


def _replace_staged(staged: str, destination: str) -> None:
    """Commit hook kept small so rollback behavior is fault-injectable in tests."""

    os.replace(staged, destination)


def _atomic_write_text(path: str, text: str, snapshot: Dict[str, Any]) -> None:
    staged = _stage_bytes(path, text.encode("utf-8"), int(snapshot.get("mode") or 0o644))
    try:
        _verify_snapshot(path, snapshot)
        _replace_staged(staged, path)
        staged = ""
        _fsync_directory(os.path.dirname(path) or ".")
    finally:
        if staged:
            try:
                os.unlink(staged)
            except OSError:
                pass


def _commit_file_transaction(prepared: List[Dict[str, Any]]) -> None:
    """Commit a staged multi-file transaction with best-effort rollback.

    Each individual replacement is atomic. The collection is not globally
    atomic: process death or power loss between replacements can expose a
    partial state because there is no durable journal/recovery pass.
    """

    staged: Dict[str, str] = {}
    backups: Dict[str, str] = {}
    committed: List[Dict[str, Any]] = []
    directories = {os.path.dirname(item["path"]) or "." for item in prepared}
    try:
        for item in prepared:
            path = item["path"]
            snapshot = item["snapshot"]
            if item["kind"] != "delete":
                staged[path] = _stage_bytes(path, item["new"].encode("utf-8"), snapshot["mode"])
            if snapshot["exists"]:
                backups[path] = _stage_bytes(path, snapshot["data"], snapshot["mode"])

        # Reject stale patch plans before making the first visible change.
        for item in prepared:
            _verify_snapshot(item["path"], item["snapshot"])

        for item in prepared:
            path = item["path"]
            # Close the remaining race window for every later operation. If it
            # changed after an earlier commit, the earlier commits roll back.
            _verify_snapshot(path, item["snapshot"])
            if item["kind"] == "delete":
                os.unlink(path)
            else:
                _replace_staged(staged[path], path)
                staged[path] = ""
            committed.append(item)

        for directory in directories:
            _fsync_directory(directory)
    except BaseException:
        rollback_error: Optional[BaseException] = None
        for item in reversed(committed):
            path = item["path"]
            try:
                if item["snapshot"]["exists"]:
                    _replace_staged(backups[path], path)
                    backups[path] = ""
                elif os.path.lexists(path):
                    os.unlink(path)
            except BaseException as exc:  # preserve the original failure below
                rollback_error = rollback_error or exc
        for directory in directories:
            try:
                _fsync_directory(directory)
            except OSError:
                pass
        if rollback_error is not None:
            raise RuntimeError(f"patch failed and rollback was incomplete: {rollback_error}")
        raise
    finally:
        for temporary in (*staged.values(), *backups.values()):
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def _pagination_args(args: Dict[str, Any]) -> Tuple[int, int]:
    try:
        offset = int(args.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(args.get("max_results") or _CODENAV_MAX_HITS)
    except (TypeError, ValueError):
        limit = _CODENAV_MAX_HITS
    return (
        max(0, min(offset, _CODENAV_MAX_OFFSET)),
        max(1, min(limit, _CODENAV_MAX_HITS)),
    )


def _paged_result(
    items: List[str],
    *,
    tool: str,
    offset: int,
    limit: int,
    empty_output: str,
    prefix: str = "",
    source_has_more: bool = False,
    total_results: Optional[int] = None,
) -> Dict[str, Any]:
    """Return a bounded result page with a stable, model-readable cursor."""

    # Reserve room for the continuation notice and structured formatting.
    char_budget = max(MAX_OUTPUT_CHARS - len(prefix) - 400, 1)
    selected: List[str] = []
    used = 0
    for item in items[offset : offset + limit]:
        line = str(item)
        addition = len(line) + (1 if selected else 0)
        if selected and used + addition > char_budget:
            break
        if not selected and addition > char_budget:
            line = line[:char_budget]
            addition = len(line)
        selected.append(line)
        used += addition

    next_offset = offset + len(selected)
    has_more = next_offset < len(items) or source_has_more
    if selected:
        body = "\n".join(selected)
        output = f"{prefix}{body}"
    else:
        output = empty_output
    result: Dict[str, Any] = {
        "output": output,
        "exit_code": 0,
        "offset": offset,
        "returned_results": len(selected),
        "truncated": has_more,
    }
    if total_results is not None:
        result["total_results"] = total_results
    if has_more and selected:
        result["next_offset"] = next_offset
        result["resume_hint"] = (
            f"Call {tool} again with offset={next_offset} and the same "
            "search arguments to continue."
        )
        result["output"] += (
            f"\n... [more results; continue with offset={next_offset}]"
        )
    return result


def _glob_to_regex(pat: str) -> "re.Pattern":
    """Translate a forward-slash glob (**, *, ?) into a compiled regex.
    `**/` matches zero or more complete directories.
    `*` matches within a single path segment (does not cross /).
    """
    i, n, out = 0, len(pat), []
    while i < n:
        if pat[i : i + 3] == "**/":
            out.append("(?:[^/]+/)*")
            i += 3
        elif pat[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out))

def _unified_diff(old: str, new: str, path: str) -> Optional[Dict[str, Any]]:
    if old == new:
        return None
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    label = path or "file"
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{label}", tofile=f"b/{label}",
        lineterm="",
    ))
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    truncated = False
    if len(diff_lines) > MAX_DIFF_LINES:
        diff_lines = diff_lines[:MAX_DIFF_LINES]
        truncated = True
    text = "\n".join(diff_lines)
    if truncated:
        text += f"\n… diff truncated at {MAX_DIFF_LINES} lines"
    return {
        "text": text,
        "added": added,
        "removed": removed,
        "new_file": old == "",
        "file": os.path.basename(path) or (path or "file"),
    }

class EditFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        try:
            args = json.loads(content) if content.strip().startswith("{") else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        raw_path = (args.get("path") or "").strip()
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))
        expected_sha256 = args.get("expected_sha256")
        if not raw_path:
            return {"error": "edit_file: path required", "exit_code": 1}
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"edit_file: {e}", "exit_code": 1}
        if old == "":
            return {"error": "edit_file: old_string required (use write_file to create a file)", "exit_code": 1}
        if old == new:
            return {"error": "edit_file: old_string and new_string are identical", "exit_code": 1}

        def _apply():
            """Helper function that performs the actual string replacement and file writing logic."""
            snapshot = _read_snapshot(path)
            if not snapshot["exists"]:
                raise FileNotFoundError(path)
            _check_expected_hash(snapshot, expected_sha256, path)
            original = snapshot["data"].decode("utf-8")
            count = original.count(old)
            if count == 0:
                return original, None, "not_found"
            if count > 1 and not replace_all:
                return original, None, f"not_unique:{count}"
            updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
            _atomic_write_text(path, updated, snapshot)
            return original, updated, "ok"

        try:
            original, updated, status = await asyncio.to_thread(_apply)
        except FileNotFoundError:
            return {"error": f"edit_file: {path}: not found (use write_file to create it)", "exit_code": 1}
        except (IsADirectoryError, UnicodeDecodeError):
            return {"error": f"edit_file: {path}: not an editable text file", "exit_code": 1}
        except PermissionError:
            return {"error": f"edit_file: {path}: permission denied", "exit_code": 1}
        except (OSError, ValueError) as e:
            return {"error": f"edit_file: {path}: {e}", "exit_code": 1}

        if status == "not_found":
            return {"error": f"edit_file: old_string not found in {path}. Read the file and match it exactly.", "exit_code": 1}
        if status.startswith("not_unique"):
            n = status.split(":", 1)[1]
            return {"error": f"edit_file: old_string is not unique in {path} ({n} matches). Add surrounding context or set replace_all=true.", "exit_code": 1}

        n = original.count(old)
        result = {
            "output": f"Edited {path} ({n} replacement{'s' if n != 1 else ''})",
            "exit_code": 0,
            "previous_sha256": _digest(original.encode("utf-8")),
            "sha256": _digest(updated.encode("utf-8")),
        }
        diff = _unified_diff(original, updated, path)
        if diff:
            result["diff"] = diff
        return result

class ReadFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        from src.agent.execution.result_budget import line_read_cursor
        raw_path, offset, limit = content.split("\n", 1)[0].strip(), 0, 0
        _stripped = content.strip()
        if _stripped.startswith("{"):
            try:
                _a = json.loads(_stripped)
                raw_path = str(_a.get("path", "")).strip()
                offset = int(_a.get("offset") or 0)
                limit = int(_a.get("limit") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"read_file: {e}", "exit_code": 1}
        try:
            def _read():
                if offset > 0 or limit > 0:
                    start = max(offset, 1)
                    out, n, budget = [], 0, MAX_READ_CHARS
                    last_line = start - 1
                    hit_char_budget = False
                    hit_limit = False
                    hit_eof = False
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if i < start:
                                continue
                            if limit > 0 and n >= limit:
                                hit_limit = True
                                break
                            out.append(line)
                            n += 1
                            last_line = i
                            budget -= len(line)
                            if budget <= 0:
                                hit_char_budget = True
                                break
                        else:
                            hit_eof = True
                    return "".join(out), last_line, hit_char_budget, hit_limit, hit_eof
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read(MAX_READ_CHARS + 1), 0, False, False, False
            data, last_line, hit_char_budget, hit_limit, hit_eof = await asyncio.to_thread(_read)
        except FileNotFoundError:
            return {"error": f"read_file: {path}: not found", "exit_code": 1}
        except PermissionError:
            return {"error": f"read_file: {path}: permission denied", "exit_code": 1}
        except IsADirectoryError:
            return {"error": f"read_file: {path}: is a directory (use ls)", "exit_code": 1}
        except OSError as e:
            return {"error": f"read_file: {path}: {e}", "exit_code": 1}

        paged = offset > 0 or limit > 0
        if paged:
            result = {"output": data, "exit_code": 0}
            if hit_char_budget:
                result["output"] += f"\n... [truncated at {MAX_READ_CHARS} chars]"
            result["truncated"] = hit_char_budget
            next_offset = line_read_cursor(
                truncated=hit_char_budget,
                last_line_read=last_line,
                limit=limit,
                hit_eof=hit_eof,
            )
            if next_offset is not None:
                result["next_offset"] = next_offset
            return result

        # Whole-file read (no offset/limit requested).
        if len(data) > MAX_READ_CHARS:
            kept = data[:MAX_READ_CHARS]
            return {
                "output": kept + f"\n... [truncated at {MAX_READ_CHARS} chars]",
                "exit_code": 0,
                "truncated": True,
                # Resume point: count of newlines actually kept, so a
                # follow-up call can pass offset=next_offset to continue
                # exactly where this read stopped.
                "next_offset": kept.count("\n") + 1,
            }
        return {
            "output": data,
            "exit_code": 0,
            "truncated": False,
            "sha256": _digest(data.encode("utf-8")),
        }

class WriteFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        lines = content.split("\n", 1)
        raw_path = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        # Decode JSON-object args (the fenced inline-args shape
        # ```write_file {"path": "...", "content": "..."}```), matching
        # ReadFileTool above. Without this the whole JSON string becomes the
        # path and the file is written under a garbage name. This is the live
        # path: there is no filesystem MCP server, so write_file always runs
        # here via _direct_fallback, not through _build_mcp_args.
        _stripped = content.strip()
        expected_sha256 = None
        if _stripped.startswith("{"):
            try:
                _a = json.loads(_stripped)
                if isinstance(_a, dict) and "path" in _a:
                    raw_path = str(_a.get("path", "")).strip()
                    body = str(_a.get("content", ""))
                    expected_sha256 = _a.get("expected_sha256")
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"write_file: {e}", "exit_code": 1}
        try:
            def _write():
                snapshot = _read_snapshot(path)
                _check_expected_hash(snapshot, expected_sha256, path)
                old = snapshot["data"].decode("utf-8") if snapshot["exists"] else ""
                _atomic_write_text(path, body, snapshot)
                return old, len(body.encode("utf-8")), snapshot["sha256"]
            old_content, size, previous_sha256 = await asyncio.to_thread(_write)
        except PermissionError:
            return {"error": f"write_file: {path}: permission denied", "exit_code": 1}
        except (OSError, ValueError, UnicodeDecodeError) as e:
            return {"error": f"write_file: {path}: {e}", "exit_code": 1}
        diff = _unified_diff(old_content, body, path)
        result = {
            "output": f"Wrote {size} bytes to {path}",
            "exit_code": 0,
            "previous_sha256": previous_sha256,
            "sha256": _digest(body.encode("utf-8")),
        }
        if diff:
            result["diff"] = diff
        return result

class ApplyPatchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        """Apply a small Codex-style patch using exact context matching.

        This is deliberately stricter than git-apply: if an update hunk's old
        text is not found exactly once, the whole patch is rejected before any
        file is changed. That keeps agent edits reviewable and avoids fuzzy
        corruption when the model patches stale context.
        """
        from src.tool_execution import _resolve_tool_path

        patch_text = content or ""
        expected_hashes: Dict[str, str] = {}
        stripped = patch_text.strip()
        if stripped.startswith("{"):
            try:
                args = json.loads(stripped)
                if isinstance(args, dict):
                    patch_text = str(args.get("patch_text") or args.get("patchText") or args.get("patch") or "")
                    raw_expected = args.get("expected_sha256") or args.get("expected_hashes") or {}
                    if isinstance(raw_expected, dict):
                        expected_hashes = {str(key): str(value) for key, value in raw_expected.items()}
            except (json.JSONDecodeError, TypeError):
                pass
        if not patch_text.strip():
            return {"error": "apply_patch: patch_text required", "exit_code": 1}

        try:
            ops = _parse_agent_patch(patch_text)
            if not ops:
                return {"error": "apply_patch: no file operations found", "exit_code": 1}
            prepared = []
            resolved_paths = set()
            for op in ops:
                path = _resolve_tool_path(op["path"])
                if path in resolved_paths:
                    return {
                        "error": f"apply_patch: duplicate operation for {op['path']}",
                        "exit_code": 1,
                    }
                resolved_paths.add(path)
                kind = op["kind"]
                snapshot = _read_snapshot(path)
                _check_expected_hash(snapshot, expected_hashes.get(op["path"]), op["path"])
                if kind == "add":
                    if snapshot["exists"] or os.path.lexists(path):
                        return {"error": f"apply_patch: {op['path']}: already exists", "exit_code": 1}
                    old = ""
                    new = op["content"]
                elif kind == "delete":
                    if not os.path.isfile(path):
                        return {"error": f"apply_patch: {op['path']}: not found", "exit_code": 1}
                    old = snapshot["data"].decode("utf-8")
                    new = ""
                else:
                    if not os.path.isfile(path):
                        return {"error": f"apply_patch: {op['path']}: not found", "exit_code": 1}
                    old = snapshot["data"].decode("utf-8")
                    new = _apply_patch_hunks(old, op["hunks"], op["path"])
                prepared.append({
                    "kind": kind,
                    "path": path,
                    "old": old,
                    "new": new,
                    "snapshot": snapshot,
                })

            diffs = []
            _commit_file_transaction(prepared)
            for item in prepared:
                diff = _unified_diff(item["old"], item["new"], item["path"])
                if diff:
                    diffs.append(diff)
        except (ValueError, UnicodeDecodeError, PermissionError, OSError) as e:
            return {"error": f"apply_patch: {e}", "exit_code": 1}

        added = sum(int(d.get("added") or 0) for d in diffs)
        removed = sum(int(d.get("removed") or 0) for d in diffs)
        text_parts = [d.get("text", "") for d in diffs if d.get("text")]
        diff_text = "\n".join(text_parts)
        if len(diff_text.splitlines()) > MAX_DIFF_LINES:
            diff_text = "\n".join(diff_text.splitlines()[:MAX_DIFF_LINES]) + f"\n... diff truncated at {MAX_DIFF_LINES} lines"
        result = {
            "output": f"Applied patch ({len(prepared)} file{'s' if len(prepared) != 1 else ''}, +{added}/-{removed})",
            "exit_code": 0,
            "transaction": "staged_with_best_effort_rollback",
            "globally_atomic": False,
        }
        if diffs:
            result["diff"] = {
                "text": diff_text,
                "added": added,
                "removed": removed,
                "new_file": any(d.get("new_file") for d in diffs),
                "file": "patch",
            }
        return result

def _parse_agent_patch(patch_text: str) -> List[Dict[str, Any]]:
    lines = patch_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ValueError("patch must start with *** Begin Patch")
    if lines[-1].strip() != "*** End Patch":
        raise ValueError("patch must end with *** End Patch")

    ops: List[Dict[str, Any]] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if not line:
            i += 1
            continue
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: "):].strip()
            body = []
            i += 1
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if not lines[i].startswith("+"):
                    raise ValueError(f"add file {path}: every content line must start with +")
                body.append(lines[i][1:])
                i += 1
            ops.append({"kind": "add", "path": path, "content": "\n".join(body) + ("\n" if body else "")})
            continue
        if line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: "):].strip()
            ops.append({"kind": "delete", "path": path})
            i += 1
            continue
        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: "):].strip()
            hunks = []
            current = []
            i += 1
            if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                raise ValueError("move operations are not supported")
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if lines[i].startswith("@@"):
                    if current:
                        hunks.append(current)
                        current = []
                elif lines[i].startswith((" ", "-", "+")):
                    current.append(lines[i])
                elif lines[i] == "":
                    current.append(" ")
                else:
                    raise ValueError(f"update file {path}: invalid patch line {lines[i]!r}")
                i += 1
            if current:
                hunks.append(current)
            if not hunks:
                raise ValueError(f"update file {path}: no hunks")
            ops.append({"kind": "update", "path": path, "hunks": hunks})
            continue
        raise ValueError(f"unexpected patch line: {line!r}")
    return ops

def _apply_patch_hunks(original: str, hunks: List[List[str]], label: str) -> str:
    updated = original
    for idx, hunk in enumerate(hunks, 1):
        old_lines = []
        new_lines = []
        for line in hunk:
            prefix, body = line[:1], line[1:]
            if prefix in (" ", "-"):
                old_lines.append(body)
            if prefix in (" ", "+"):
                new_lines.append(body)
        old_text = "\n".join(old_lines)
        new_text = "\n".join(new_lines)
        if old_text and old_text in updated:
            occurrences = updated.count(old_text)
            if occurrences != 1:
                raise ValueError(f"{label}: hunk {idx} context matched {occurrences} times")
            updated = updated.replace(old_text, new_text, 1)
        elif old_text + "\n" in updated:
            occurrences = updated.count(old_text + "\n")
            if occurrences != 1:
                raise ValueError(f"{label}: hunk {idx} context matched {occurrences} times")
            updated = updated.replace(old_text + "\n", new_text + "\n", 1)
        else:
            raise ValueError(f"{label}: hunk {idx} context not found")
    return updated

class LsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        raw_path = ""
        args: Dict[str, Any] = {}
        _s = (content or "").strip()
        if _s.startswith("{"):
            try:
                args = json.loads(_s)
                raw_path = str(args.get("path", "")).strip()
            except json.JSONDecodeError:
                raw_path = ""
        else:
            raw_path = _s.split("\n", 1)[0].strip()
        offset, max_hits = _pagination_args(args)
        try:
            root = _resolve_search_root(raw_path)
        except ValueError as e:
            return {"error": f"ls: {e}", "exit_code": 1}

        def _ls():
            if not os.path.isdir(root):
                return None, f"ls: {root}: not a directory"
            rows = []
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            size = entry.stat(follow_symlinks=False).st_size if not is_dir else 0
                        except OSError:
                            continue
                        rows.append((is_dir, entry.name, size))
            except (PermissionError, OSError) as _e:
                return None, f"ls: {_e}"
            rows.sort(key=lambda r: (not r[0], r[1].lower()))
            lines = [
                f"  {name}/" if is_dir else f"  {name}  ({size} B)"
                for is_dir, name, size in rows
            ]
            return lines, None

        lines, err = await asyncio.to_thread(_ls)
        if err:
            return {"error": err, "exit_code": 1}
        return _paged_result(
            lines,
            tool="ls",
            offset=offset,
            limit=max_hits,
            empty_output=f"{root}:\n  (empty)",
            prefix=f"{root}:\n",
            total_results=len(lines),
        )

class GlobTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import (
            _SENSITIVE_BASENAMES,
            _is_sensitive_path,
            _resolve_tool_path,
            _resolve_search_root,
            _truncate,
        )
        args = {}
        _s = (content or "").strip()
        if _s.startswith("{"):
            try:
                args = json.loads(_s)
            except json.JSONDecodeError:
                args = {}
        else:
            args = {"pattern": _s}
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return {"error": "glob: pattern is required", "exit_code": 1}
        offset, max_hits = _pagination_args(args)
        try:
            root = _resolve_search_root(str(args.get("path", "")))
        except ValueError as e:
            return {"error": f"glob: {e}", "exit_code": 1}

        def _glob():
            base = os.path.abspath(root)
            if not os.path.isdir(base):
                return None, f"glob: {root}: not a directory"
            rbase = os.path.realpath(base)
            norm_pat = pattern.replace("\\", "/")
            # Fast path: literal pattern (no wildcards) → direct path lookup.
            if not any(c in norm_pat for c in "*?["):
                cand = os.path.realpath(os.path.join(base, norm_pat))
                # Keep the literal lookup inside the search root. os.path.join
                # lets an absolute pattern (or one containing ../) escape `base`,
                # which would turn glob into an existence/path oracle for
                # arbitrary host files — bypassing the workspace/allowlist
                # confinement that _resolve_search_root applies to the root.
                # An escaping literal falls through to the walk, which only ever
                # yields paths under base.
                nbase = os.path.normcase(rbase)
                try:
                    inside = cand == rbase or os.path.commonpath(
                        [os.path.normcase(cand), nbase]
                    ) == nbase
                except ValueError:
                    inside = False
                # A literal that names a deny-listed sensitive file (.env,
                # .ssh/id_rsa, …) falls through to the walk, which skips it —
                # otherwise glob would surface secret paths that read_file /
                # grep already refuse to touch.
                if inside and os.path.exists(cand) and not _is_sensitive_path(cand):
                    return [cand], None
                # Literal not at exact path — fall through to walk so
                # e.g. "foo.py" still matches at any depth (like rglob).
            # Compile glob to regex: * stays within one segment, **/ spans dirs.
            regex = _glob_to_regex(norm_pat)
            matched: List[str] = []
            # Pagination follows a deterministic directory/path traversal.
            # Do not sort a growing partial result by mtime: doing so lets a
            # later, larger scan insert paths before an already-issued offset.
            cap = offset + max_hits + 1
            scan_capped = False
            try:
                for dp, dns, fns in os.walk(base):
                    # Prune skipped dirs before descending (unlike rglob which
                    # descends first then filters — fatal on large node_modules).
                    # Sensitive dirs (.ssh, .gnupg, …) are pruned too so glob
                    # never enumerates the keys/tokens inside them.
                    dns[:] = sorted(
                        d for d in dns
                        if d not in _CODENAV_SKIP_DIRS and d not in _SENSITIVE_BASENAMES
                    )
                    for name in sorted(fns) + dns:
                        full = os.path.join(dp, name)
                        rel = os.path.relpath(full, base).replace(os.sep, "/")
                        if regex.fullmatch(rel) or regex.fullmatch(name):
                            # Skip deny-listed sensitive files (.env, id_rsa,
                            # known_hosts, …) the same way grep does.
                            if _is_sensitive_path(os.path.realpath(full)):
                                continue
                            matched.append(full)
                            if len(matched) >= cap:
                                scan_capped = True
                                break
                    if scan_capped:
                        break
            except OSError as _e:
                return None, f"glob: {_e}"
            return matched, scan_capped, None

        globbed = await asyncio.to_thread(_glob)
        if len(globbed) == 2:
            # Literal lookup and early errors use the compact return shape.
            paths, err = globbed
            scan_capped = False
        else:
            paths, scan_capped, err = globbed
        if err:
            return {"error": err, "exit_code": 1}
        if not paths:
            return _paged_result(
                [],
                tool="glob",
                offset=offset,
                limit=max_hits,
                empty_output=f"No files matching {pattern!r} under {root}",
            )
        return _paged_result(
            paths,
            tool="glob",
            offset=offset,
            limit=max_hits,
            empty_output=f"No files matching {pattern!r} under {root}",
            source_has_more=scan_capped,
        )

class GrepTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import (
            _SENSITIVE_FILE_PATTERNS,
            _is_sensitive_path,
            _resolve_tool_path,
            _resolve_search_root,
            _truncate,
        )
        args: Dict[str, Any] = {}
        _s = (content or "").strip()
        if _s.startswith("{"):
            try:
                args = json.loads(_s)
            except json.JSONDecodeError:
                args = {}
        else:
            args = {"pattern": _s}
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return {"error": "grep: pattern is required", "exit_code": 1}
        ignore_case = bool(args.get("ignore_case"))
        glob_pat = str(args.get("glob", "") or "").strip()
        offset, max_hits = _pagination_args(args)
        required_hits = offset + max_hits + 1
        try:
            root = _resolve_search_root(str(args.get("path", "")))
        except ValueError as e:
            return {"error": f"grep: {e}", "exit_code": 1}

        def _grep():
            import re as _re
            import shutil
            rg = shutil.which("rg")
            if rg:
                cmd = [
                    rg,
                    "--line-number",
                    "--no-heading",
                    "--color=never",
                    "--sort",
                    "path",
                ]
                if ignore_case:
                    cmd.append("--ignore-case")
                if glob_pat:
                    cmd += ["--glob", glob_pat]
                # --iglob (not --glob) so the exclusion is case-insensitive:
                # on a case-insensitive filesystem "ID_RSA"/"Known_Hosts"
                # resolve to the same secret as their lowercase forms, and the
                # Python fallback below already folds case via _is_sensitive_path.
                for _pat in _SENSITIVE_FILE_PATTERNS:
                    cmd += ["--iglob", f"!*{_pat}*"]
                for _d in _CODENAV_SKIP_DIRS:
                    cmd += ["--glob", f"!**/{_d}/**"]
                cmd += ["--regexp", pattern, root]
                try:
                    import subprocess
                    import threading

                    p = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                    )
                    timed_out = threading.Event()

                    def _kill_on_timeout():
                        timed_out.set()
                        try:
                            p.kill()
                        except ProcessLookupError:
                            pass

                    timer = threading.Timer(20, _kill_on_timeout)
                    timer.daemon = True
                    timer.start()
                    lines = []
                    stopped_early = False
                    try:
                        assert p.stdout is not None
                        for raw_line in p.stdout:
                            line = raw_line.rstrip("\n")
                            if line:
                                lines.append(line)
                            if len(lines) >= required_hits:
                                stopped_early = True
                                try:
                                    p.terminate()
                                except ProcessLookupError:
                                    pass
                                break
                        try:
                            p.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            p.kill()
                            p.wait(timeout=2)
                    finally:
                        timer.cancel()
                    stderr = p.stderr.read() if p.stderr is not None else ""
                    if timed_out.is_set():
                        return None, False, "grep: timed out"
                    if not stopped_early and p.returncode not in (0, 1):
                        detail = stderr.strip() or f"ripgrep exited {p.returncode}"
                        return None, False, f"grep: {detail}"
                    return lines, stopped_early, None
                except subprocess.TimeoutExpired:
                    return None, False, "grep: timed out"
                except Exception as _e:
                    return None, False, f"grep: {_e}"
            try:
                rx = _re.compile(pattern, _re.IGNORECASE if ignore_case else 0)
            except _re.error as _e:
                return None, False, f"grep: bad pattern: {_e}"
            hits = []
            def _files():
                if os.path.isfile(root):
                    yield root
                    return
                for dp, dns, fns in os.walk(root):
                    dns[:] = sorted(d for d in dns if d not in _CODENAV_SKIP_DIRS)
                    for fn in sorted(fns):
                        if not glob_pat or fnmatch.fnmatch(fn, glob_pat):
                            yield os.path.join(dp, fn)

            stopped_early = False
            file_iter = _files()
            for fp in file_iter:
                if len(hits) >= required_hits:
                    stopped_early = True
                    break
                if _is_sensitive_path(os.path.realpath(fp)):
                    continue
                try:
                    with open(fp, "r", encoding="utf-8", errors="strict") as f:
                        for i, line in enumerate(f, 1):
                            if rx.search(line):
                                hits.append(f"{fp}:{i}:{line.rstrip()[:_CODENAV_MAX_LINE]}")
                                if len(hits) >= required_hits:
                                    stopped_early = True
                                    break
                except (UnicodeDecodeError, OSError):
                    continue
            return hits, stopped_early, None

        lines, source_has_more, err = await asyncio.to_thread(_grep)
        if err:
            return {"error": err, "exit_code": 1}
        if not lines:
            return _paged_result(
                [],
                tool="grep",
                offset=offset,
                limit=max_hits,
                empty_output=f"No matches for {pattern!r} under {root}",
            )
        return _paged_result(
            [line[:_CODENAV_MAX_LINE] for line in lines],
            tool="grep",
            offset=offset,
            limit=max_hits,
            empty_output=f"No matches for {pattern!r} under {root}",
            source_has_more=source_has_more,
        )

class GetWorkspaceTool:
    """Report the active workspace folder (no args). File tools are confined to
    it; sandboxed subprocesses mount only it writable."""
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import get_active_workspace
        ws = get_active_workspace()
        if ws:
            return {
                "output": f"{ws}\n(File tools are confined to this folder; sandboxed "
                          f"subprocesses mount it as their only writable workspace.)",
                "exit_code": 0,
            }
        return {
            "output": "No workspace is set. File tools use the default allowed roots; "
                      "resolve paths from the user or use absolute paths.",
            "exit_code": 0,
        }
