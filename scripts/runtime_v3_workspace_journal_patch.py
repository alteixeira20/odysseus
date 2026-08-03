from pathlib import Path
import re


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one anchor, found {count}: {old[:160]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def regex_once(path: str, pattern: str, replacement: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{path}: expected one regex anchor, found {count}: {pattern[:160]!r}")
    target.write_text(updated, encoding="utf-8")


# ---------------------------------------------------------------------------
# Make preparation itself durable before creating payload files.
# ---------------------------------------------------------------------------
replace_once(
    "src/agent/runtime_v3/workspace_journal.py",
    '''def _safe_unlink(path: str | None) -> None:
''',
    '''def _payload_path(path: str, transaction_id: str, role: str, index: int) -> str:
    directory = os.path.dirname(path) or "."
    basename = os.path.basename(path)
    return os.path.join(directory, f".{basename}.ody-v3-{transaction_id}-{role}-{index}")


def _write_payload(path: str, data: bytes, mode: int) -> None:
    directory = os.path.dirname(path) or "."
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode)
        _fsync_directory(directory)
    except BaseException:
        _safe_unlink(path)
        raise


def _safe_unlink(path: str | None) -> None:
''',
)

replace_once(
    "src/agent/runtime_v3/workspace_journal.py",
    '''            manifest = self._prepare_manifest(transaction_id, prepared)
            try:
                self._write_manifest(manifest_path, manifest)
                self._set_phase(manifest_path, manifest, "committing")
                for index, operation in enumerate(manifest["operations"]):
                    self._verify_original(operation)
''',
    '''            manifest = self._plan_manifest(transaction_id, prepared)
            try:
                # The manifest exists before any payload is created, so a kill
                # during preparation leaves a fully discoverable transaction.
                self._write_manifest(manifest_path, manifest)
                if fault_injector:
                    fault_injector("after_manifest", -1, manifest)
                self._stage_payloads(manifest_path, manifest, fault_injector=fault_injector)
                self._set_phase(manifest_path, manifest, "prepared")
                if fault_injector:
                    fault_injector("after_payloads_ready", -1, manifest)
                try:
                    self._verify_all_originals(manifest["operations"])
                except BaseException:
                    # No destination has changed yet. Preserve any external edit,
                    # delete only our hidden payloads, and close the transaction.
                    manifest["phase"] = "aborted_concurrent_modification"
                    self._write_manifest(manifest_path, manifest)
                    self._cleanup_payload_files(manifest)
                    self._archive_and_remove(manifest_path, manifest)
                    raise
                self._set_phase(manifest_path, manifest, "committing")
                for index, operation in enumerate(manifest["operations"]):
                    self._verify_original(operation)
''',
)

regex_once(
    "src/agent/runtime_v3/workspace_journal.py",
    r'''    def _prepare_manifest\(self, transaction_id: str, prepared: Sequence\[Mapping\[str, Any\]\]\) -> dict\[str, Any\]:.*?\n    def _manifest_digest''',
    '''    def _plan_manifest(self, transaction_id: str, prepared: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        operations: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(prepared):
            path = os.path.abspath(os.fspath(item["path"]))
            if path in seen:
                raise ValueError(f"duplicate transaction destination: {path}")
            seen.add(path)
            kind = str(item["kind"])
            if kind not in {"add", "update", "delete"}:
                raise ValueError(f"unsupported transaction operation: {kind}")
            snapshot = item["snapshot"]
            old_exists = bool(snapshot["exists"])
            old_data = bytes(snapshot.get("data") or b"")
            old_hash = _sha256_bytes(old_data) if old_exists else None
            supplied_hash = snapshot.get("sha256")
            if old_hash != supplied_hash:
                raise ValueError(f"snapshot digest mismatch for {path}")
            mode = int(snapshot.get("mode") or 0o644)
            new_exists = kind != "delete"
            new_data = str(item.get("new") or "").encode("utf-8") if new_exists else b""
            new_hash = _sha256_bytes(new_data) if new_exists else None
            operations.append(
                {
                    "index": index,
                    "kind": kind,
                    "path": path,
                    "directory": os.path.dirname(path) or ".",
                    "mode": mode,
                    "old_exists": old_exists,
                    "old_sha256": old_hash,
                    "new_exists": new_exists,
                    "new_sha256": new_hash,
                    "backup_path": (
                        _payload_path(path, transaction_id, "backup", index)
                        if old_exists else None
                    ),
                    "staged_path": (
                        _payload_path(path, transaction_id, "new", index)
                        if new_exists else None
                    ),
                    # Payload bytes exist only in memory until the preparing
                    # manifest has been durably written.
                    "_old_data": old_data,
                    "_new_data": new_data,
                }
            )
        manifest = {
            "version": self.VERSION,
            "transaction_id": transaction_id,
            "phase": "preparing",
            "created_at": time.time(),
            "updated_at": time.time(),
            "applied_count": 0,
            "operations": operations,
        }
        manifest["manifest_sha256"] = self._manifest_digest(manifest)
        return manifest

    def _serializable_manifest(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        cloned = dict(manifest)
        cloned["operations"] = [
            {key: value for key, value in operation.items() if not key.startswith("_")}
            for operation in manifest["operations"]
        ]
        return cloned

    def _stage_payloads(
        self,
        manifest_path: Path,
        manifest: dict[str, Any],
        *,
        fault_injector=None,
    ) -> None:
        for index, operation in enumerate(manifest["operations"]):
            backup = operation.get("backup_path")
            staged = operation.get("staged_path")
            if backup:
                _write_payload(backup, operation["_old_data"], int(operation["mode"]))
            if staged:
                _write_payload(staged, operation["_new_data"], int(operation["mode"]))
            operation.pop("_old_data", None)
            operation.pop("_new_data", None)
            manifest["prepared_count"] = index + 1
            self._write_manifest(manifest_path, manifest)
            if fault_injector:
                fault_injector("after_payload", index, operation)

    def _manifest_digest''',
)

replace_once(
    "src/agent/runtime_v3/workspace_journal.py",
    '''    def _write_manifest(self, path: Path, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = time.time()
        manifest["manifest_sha256"] = self._manifest_digest(manifest)
        _atomic_json(path, manifest)
''',
    '''    def _write_manifest(self, path: Path, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = time.time()
        serializable = self._serializable_manifest(manifest)
        serializable["manifest_sha256"] = self._manifest_digest(serializable)
        manifest["manifest_sha256"] = serializable["manifest_sha256"]
        _atomic_json(path, serializable)
''',
)

replace_once(
    "src/agent/runtime_v3/workspace_journal.py",
    '''    def _verify_original(self, operation: Mapping[str, Any]) -> None:
''',
    '''    def _verify_all_originals(self, operations: Sequence[Mapping[str, Any]]) -> None:
        for operation in operations:
            self._verify_original(operation)

    def _verify_original(self, operation: Mapping[str, Any]) -> None:
''',
)

replace_once(
    "src/agent/runtime_v3/workspace_journal.py",
    '''    def _restore_old(self, operation: Mapping[str, Any]) -> None:
        path = operation["path"]
        if operation["old_exists"]:
''',
    '''    def _restore_old(self, operation: Mapping[str, Any]) -> None:
        path = operation["path"]
        current_exists, current_hash = _path_state(path)
        if (
            current_exists == bool(operation["old_exists"])
            and current_hash == operation["old_sha256"]
        ):
            return
        if operation["old_exists"]:
''',
)

replace_once(
    "src/agent/runtime_v3/workspace_journal.py",
    '''    def _rollback(self, manifest_path: Path, manifest: dict[str, Any]) -> None:
        self._set_phase(manifest_path, manifest, "rolling_back")
''',
    '''    def _rollback(self, manifest_path: Path, manifest: dict[str, Any]) -> None:
        # Never overwrite a third-party edit. Automatic rollback is safe only
        # when every path is in exactly the recorded old or recorded new state.
        for operation in manifest["operations"]:
            state = self._operation_state(operation)
            if state == "other":
                raise WorkspaceRecoveryRequired(
                    f"destination changed outside the transaction: {operation['path']}"
                )
        self._set_phase(manifest_path, manifest, "rolling_back")
''',
)

replace_once(
    "src/agent/runtime_v3/workspace_journal.py",
    '''    def _all_match_new(self, operations: Sequence[Mapping[str, Any]]) -> bool:
''',
    '''    def _operation_state(self, operation: Mapping[str, Any]) -> str:
        exists, digest = _path_state(operation["path"])
        if exists == bool(operation["old_exists"]) and digest == operation["old_sha256"]:
            return "old"
        if exists == bool(operation["new_exists"]) and digest == operation["new_sha256"]:
            return "new"
        return "other"

    def _all_match_new(self, operations: Sequence[Mapping[str, Any]]) -> bool:
''',
)

replace_once(
    "src/agent/runtime_v3/workspace_journal.py",
    '''            if _JOURNAL is None:
                _JOURNAL = WorkspaceTransactionJournal()
                _JOURNAL.recover()
''',
    '''            if _JOURNAL is None:
                _JOURNAL = WorkspaceTransactionJournal()
''',
)

# ---------------------------------------------------------------------------
# Make apply_patch use the journal and report honest semantics.
# ---------------------------------------------------------------------------
replace_once(
    "src/agent_tools/filesystem_tools.py",
    '''from src.constants import MAX_READ_CHARS, MAX_DIFF_LINES, MAX_OUTPUT_CHARS
''',
    '''from src.constants import MAX_READ_CHARS, MAX_DIFF_LINES, MAX_OUTPUT_CHARS
from src.agent.runtime_v3.workspace_journal import (
    WorkspaceJournalError,
    WorkspaceRecoveryRequired,
    get_workspace_journal,
)
''',
)
regex_once(
    "src/agent_tools/filesystem_tools.py",
    r'''def _commit_file_transaction\(prepared: List\[Dict\[str, Any\]\]\) -> None:.*?\n\nclass ListDirTool:''',
    '''def _commit_file_transaction(prepared: List[Dict[str, Any]]):
    """Commit a crash-recoverable multi-file transaction.

    Visibility is sequential while the process is alive, but a crash or restart
    deterministically converges to all-old or all-new before traffic is served.
    """
    return get_workspace_journal().commit(prepared)


class ListDirTool:''',
)
replace_once(
    "src/agent_tools/filesystem_tools.py",
    '''            diffs = []
            _commit_file_transaction(prepared)
''',
    '''            diffs = []
            transaction_receipt = _commit_file_transaction(prepared)
''',
)
replace_once(
    "src/agent_tools/filesystem_tools.py",
    '''        except (ValueError, UnicodeDecodeError, PermissionError, OSError) as e:
''',
    '''        except (
            ValueError,
            UnicodeDecodeError,
            PermissionError,
            OSError,
            WorkspaceJournalError,
            WorkspaceRecoveryRequired,
        ) as e:
''',
)
replace_once(
    "src/agent_tools/filesystem_tools.py",
    '''            "transaction": "staged_with_best_effort_rollback",
            "globally_atomic": False,
''',
    '''            "transaction": transaction_receipt.as_dict(),
            # Multi-path visibility cannot be atomic on a normal filesystem,
            # but process death is recoverable to one complete state.
            "globally_atomic": False,
            "crash_atomic": True,
            "recovery_semantics": "all_old_or_all_new",
''',
)

# ---------------------------------------------------------------------------
# Recover before startup exposes any workspace read endpoint.
# ---------------------------------------------------------------------------
replace_once(
    "app.py",
    '''    enforce_single_runtime_worker()
    webhook_manager.set_loop(asyncio.get_running_loop())
''',
    '''    enforce_single_runtime_worker()
    try:
        from src.agent.runtime_v3.workspace_journal import get_workspace_journal

        recovered_transactions = await asyncio.to_thread(get_workspace_journal().recover)
        if recovered_transactions:
            logger.warning(
                "Recovered %d interrupted workspace transaction(s) before serving traffic",
                len(recovered_transactions),
            )
    except Exception:
        logger.exception("Workspace transaction recovery failed; refusing startup")
        raise
    webhook_manager.set_loop(asyncio.get_running_loop())
''',
)

print("Runtime V3 workspace journal integration applied")
