from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Iterator, Mapping, Sequence


class WorkspaceJournalError(RuntimeError):
    pass


class WorkspaceRecoveryRequired(WorkspaceJournalError):
    pass


class SimulatedProcessCrash(BaseException):
    """Fault-injection sentinel that deliberately bypasses in-process rollback."""


@dataclass(frozen=True)
class WorkspaceTransactionReceipt:
    transaction_id: str
    state: str
    file_count: int
    recovered: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "state": self.state,
            "file_count": self.file_count,
            "recovered": self.recovered,
            "journaled": True,
            "globally_atomic_after_recovery": True,
        }


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_directory(path: str | os.PathLike[str]) -> None:
    directory = os.fspath(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical(payload).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = ""
        _fsync_directory(path.parent)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _read_file(path: str) -> tuple[bool, bytes, int]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        mode = os.stat(path, follow_symlinks=False).st_mode & 0o777
        return True, data, mode
    except FileNotFoundError:
        return False, b"", 0o644


def _path_state(path: str) -> tuple[bool, str | None]:
    try:
        with open(path, "rb") as handle:
            return True, _sha256_bytes(handle.read())
    except FileNotFoundError:
        return False, None


def _stage(path: str, data: bytes, mode: int, transaction_id: str, role: str) -> str:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    prefix = f".{os.path.basename(path)}.ody-v3-{transaction_id}-{role}-"
    descriptor, staged = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, mode)
        _fsync_directory(directory)
        return staged
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise


def _payload_path(path: str, transaction_id: str, role: str, index: int) -> str:
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
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        return


def _default_root() -> Path:
    configured = os.getenv("ODYSSEUS_WORKSPACE_JOURNAL_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    try:
        from src.constants import DATA_DIR

        base = Path(DATA_DIR)
    except Exception:
        base = Path("data")
    return base / "agent-runtime-v3" / "workspace-journal"


class _ProcessFileLock:
    """Cross-process exclusive file lock with bounded acquisition."""

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno not in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EDEADLK,
                }:
                    self.handle.close()
                    self.handle = None
                    raise
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise WorkspaceJournalError("timed out acquiring workspace transaction lock")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class WorkspaceTransactionJournal:
    """Crash-recoverable multi-file workspace transaction manager.

    There is a single global lock because destination sets can overlap in ways
    that directory-scoped locks cannot safely detect. Transactions are short,
    and correctness is more important than concurrent patch throughput.
    """

    VERSION = 1
    _TERMINAL = {"committed", "rolled_back"}

    def __init__(self, root: str | os.PathLike[str] | None = None, *, lock_timeout_seconds: float = 30.0) -> None:
        self.root = Path(root) if root is not None else _default_root()
        self.pending_dir = self.root / "pending"
        self.receipts_dir = self.root / "receipts"
        self.lock_path = self.root / "workspace-transactions.lock"
        self.lock_timeout_seconds = max(0.1, float(lock_timeout_seconds))
        self._thread_lock = threading.RLock()
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
            self.pending_dir.chmod(0o700)
            self.receipts_dir.chmod(0o700)
        except OSError:
            pass

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with self._thread_lock:
            with _ProcessFileLock(self.lock_path, self.lock_timeout_seconds):
                yield

    def commit(
        self,
        prepared: Sequence[Mapping[str, Any]],
        *,
        fault_injector: Callable[[str, int, Mapping[str, Any]], None] | None = None,
    ) -> WorkspaceTransactionReceipt:
        if not prepared:
            raise ValueError("workspace transaction requires at least one operation")
        with self._exclusive():
            self._recover_locked()
            transaction_id = uuid.uuid4().hex
            manifest_path = self.pending_dir / f"{transaction_id}.json"
            manifest = self._plan_manifest(transaction_id, prepared)
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
                    self._apply_new(operation)
                    manifest["applied_count"] = index + 1
                    self._write_manifest(manifest_path, manifest)
                    if fault_injector:
                        fault_injector("after_replace", index, operation)
                if fault_injector:
                    fault_injector("after_all_replacements", len(manifest["operations"]), manifest)
                self._set_phase(manifest_path, manifest, "committed")
                self._cleanup_payload_files(manifest)
                self._archive_and_remove(manifest_path, manifest)
                return WorkspaceTransactionReceipt(
                    transaction_id=transaction_id,
                    state="committed",
                    file_count=len(manifest["operations"]),
                )
            except SimulatedProcessCrash:
                # Intentionally emulate an abrupt process exit: journal and
                # payload files remain for a fresh process to recover.
                raise
            except BaseException:
                if manifest.get("phase") == "aborted_concurrent_modification":
                    raise
                try:
                    self._rollback(manifest_path, manifest)
                except BaseException as rollback_exc:
                    manifest["phase"] = "recovery_required"
                    manifest["recovery_error"] = {
                        "type": type(rollback_exc).__name__,
                        "message": str(rollback_exc)[:2000],
                    }
                    self._write_manifest(manifest_path, manifest)
                    raise WorkspaceRecoveryRequired(
                        f"workspace transaction {transaction_id} failed and rollback requires recovery: {rollback_exc}"
                    )
                raise

    def recover(self) -> list[WorkspaceTransactionReceipt]:
        with self._exclusive():
            return self._recover_locked()

    def _recover_locked(self) -> list[WorkspaceTransactionReceipt]:
        receipts: list[WorkspaceTransactionReceipt] = []
        for manifest_path in sorted(self.pending_dir.glob("*.json")):
            manifest = self._load_manifest(manifest_path)
            phase = str(manifest.get("phase") or "")
            operations = manifest["operations"]
            if phase in self._TERMINAL:
                self._cleanup_payload_files(manifest)
                self._archive_and_remove(manifest_path, manifest)
                receipts.append(
                    WorkspaceTransactionReceipt(
                        manifest["transaction_id"], phase, len(operations), recovered=True
                    )
                )
                continue
            if self._all_match_new(operations):
                manifest["phase"] = "committed"
                manifest["recovered_at"] = time.time()
                self._write_manifest(manifest_path, manifest)
                self._cleanup_payload_files(manifest)
                self._archive_and_remove(manifest_path, manifest)
                receipts.append(
                    WorkspaceTransactionReceipt(
                        manifest["transaction_id"], "committed", len(operations), recovered=True
                    )
                )
                continue
            try:
                self._rollback(manifest_path, manifest)
            except BaseException as exc:
                manifest["phase"] = "recovery_required"
                manifest["recovery_error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc)[:2000],
                }
                self._write_manifest(manifest_path, manifest)
                raise WorkspaceRecoveryRequired(
                    f"workspace transaction {manifest['transaction_id']} cannot be recovered automatically: {exc}"
                ) from exc
            receipts.append(
                WorkspaceTransactionReceipt(
                    manifest["transaction_id"], "rolled_back", len(operations), recovered=True
                )
            )
        return receipts

    def _plan_manifest(self, transaction_id: str, prepared: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
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

    def _manifest_digest(self, manifest: Mapping[str, Any]) -> str:
        body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()

    def _write_manifest(self, path: Path, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = time.time()
        serializable = self._serializable_manifest(manifest)
        serializable["manifest_sha256"] = self._manifest_digest(serializable)
        manifest["manifest_sha256"] = serializable["manifest_sha256"]
        _atomic_json(path, serializable)

    def _load_manifest(self, path: Path) -> dict[str, Any]:
        try:
            raw = path.read_text(encoding="utf-8")
            manifest = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceRecoveryRequired(f"workspace journal is unreadable: {path}: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("version") != self.VERSION:
            raise WorkspaceRecoveryRequired(f"unsupported workspace journal: {path}")
        expected = manifest.get("manifest_sha256")
        actual = self._manifest_digest(manifest)
        if not expected or expected != actual:
            raise WorkspaceRecoveryRequired(f"workspace journal integrity check failed: {path}")
        transaction_id = str(manifest.get("transaction_id") or "")
        if path.name != f"{transaction_id}.json" or len(transaction_id) != 32:
            raise WorkspaceRecoveryRequired(f"workspace journal identity mismatch: {path}")
        operations = manifest.get("operations")
        if not isinstance(operations, list) or not operations:
            raise WorkspaceRecoveryRequired(f"workspace journal has no operations: {path}")
        seen: set[str] = set()
        for operation in operations:
            self._validate_operation(operation, transaction_id)
            destination = operation["path"]
            if destination in seen:
                raise WorkspaceRecoveryRequired(f"workspace journal contains duplicate destination: {destination}")
            seen.add(destination)
        return manifest

    def _validate_operation(self, operation: Mapping[str, Any], transaction_id: str) -> None:
        path = operation.get("path")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise WorkspaceRecoveryRequired("workspace journal contains a non-absolute destination")
        if os.path.dirname(path) != operation.get("directory"):
            raise WorkspaceRecoveryRequired("workspace journal destination directory mismatch")
        for key in ("backup_path", "staged_path"):
            payload = operation.get(key)
            if payload is None:
                continue
            if not isinstance(payload, str) or not os.path.isabs(payload):
                raise WorkspaceRecoveryRequired("workspace journal payload path is invalid")
            if os.path.dirname(payload) != operation["directory"]:
                raise WorkspaceRecoveryRequired("workspace journal payload escaped destination filesystem")
            if f"ody-v3-{transaction_id}-" not in os.path.basename(payload):
                raise WorkspaceRecoveryRequired("workspace journal payload identity mismatch")

    def _set_phase(self, path: Path, manifest: dict[str, Any], phase: str) -> None:
        manifest["phase"] = phase
        self._write_manifest(path, manifest)

    def _verify_all_originals(self, operations: Sequence[Mapping[str, Any]]) -> None:
        for operation in operations:
            self._verify_original(operation)

    def _verify_original(self, operation: Mapping[str, Any]) -> None:
        exists, digest = _path_state(operation["path"])
        if exists != bool(operation["old_exists"]) or digest != operation["old_sha256"]:
            raise WorkspaceJournalError(
                f"concurrent modification detected before transaction commit: {operation['path']}"
            )

    def _apply_new(self, operation: Mapping[str, Any]) -> None:
        path = operation["path"]
        if operation["new_exists"]:
            staged = operation.get("staged_path")
            if not staged or not os.path.isfile(staged):
                raise WorkspaceJournalError(f"staged payload missing for {path}")
            os.replace(staged, path)
            operation["staged_path"] = None
        else:
            os.unlink(path)
        _fsync_directory(operation["directory"])

    def _restore_old(self, operation: Mapping[str, Any]) -> None:
        path = operation["path"]
        current_exists, current_hash = _path_state(path)
        if (
            current_exists == bool(operation["old_exists"])
            and current_hash == operation["old_sha256"]
        ):
            return
        if operation["old_exists"]:
            backup = operation.get("backup_path")
            if not backup or not os.path.isfile(backup):
                current_exists, current_hash = _path_state(path)
                if current_exists and current_hash == operation["old_sha256"]:
                    return
                raise WorkspaceRecoveryRequired(f"backup payload missing for {path}")
            with open(backup, "rb") as handle:
                original = handle.read()
            if _sha256_bytes(original) != operation["old_sha256"]:
                raise WorkspaceRecoveryRequired(f"backup digest mismatch for {path}")
            temporary = _stage(
                path,
                original,
                int(operation["mode"]),
                str(uuid.uuid4().hex),
                "restore",
            )
            try:
                os.replace(temporary, path)
                temporary = ""
            finally:
                _safe_unlink(temporary)
        else:
            _safe_unlink(path)
        _fsync_directory(operation["directory"])
        exists, digest = _path_state(path)
        if exists != bool(operation["old_exists"]) or digest != operation["old_sha256"]:
            raise WorkspaceRecoveryRequired(f"rollback verification failed for {path}")

    def _rollback(self, manifest_path: Path, manifest: dict[str, Any]) -> None:
        # Never overwrite a third-party edit. Automatic rollback is safe only
        # when every path is in exactly the recorded old or recorded new state.
        for operation in manifest["operations"]:
            state = self._operation_state(operation)
            if state == "other":
                raise WorkspaceRecoveryRequired(
                    f"destination changed outside the transaction: {operation['path']}"
                )
        self._set_phase(manifest_path, manifest, "rolling_back")
        for reverse_index, operation in enumerate(reversed(manifest["operations"]), start=1):
            self._restore_old(operation)
            manifest["rollback_count"] = reverse_index
            self._write_manifest(manifest_path, manifest)
        self._set_phase(manifest_path, manifest, "rolled_back")
        self._cleanup_payload_files(manifest)
        self._archive_and_remove(manifest_path, manifest)

    def _operation_state(self, operation: Mapping[str, Any]) -> str:
        exists, digest = _path_state(operation["path"])
        if exists == bool(operation["old_exists"]) and digest == operation["old_sha256"]:
            return "old"
        if exists == bool(operation["new_exists"]) and digest == operation["new_sha256"]:
            return "new"
        return "other"

    def _all_match_new(self, operations: Sequence[Mapping[str, Any]]) -> bool:
        for operation in operations:
            exists, digest = _path_state(operation["path"])
            if exists != bool(operation["new_exists"]) or digest != operation["new_sha256"]:
                return False
        return True

    def _cleanup_payload_files(self, manifest: Mapping[str, Any]) -> None:
        directories: set[str] = set()
        for operation in manifest["operations"]:
            directories.add(operation["directory"])
            _safe_unlink(operation.get("backup_path"))
            _safe_unlink(operation.get("staged_path"))
        for directory in directories:
            try:
                _fsync_directory(directory)
            except OSError:
                pass

    def _archive_and_remove(self, manifest_path: Path, manifest: Mapping[str, Any]) -> None:
        receipt = {
            "version": self.VERSION,
            "transaction_id": manifest["transaction_id"],
            "phase": manifest["phase"],
            "created_at": manifest.get("created_at"),
            "completed_at": time.time(),
            "file_count": len(manifest["operations"]),
            "destinations_sha256": [
                hashlib.sha256(operation["path"].encode("utf-8")).hexdigest()
                for operation in manifest["operations"]
            ],
        }
        receipt["receipt_sha256"] = hashlib.sha256(_canonical(receipt).encode("utf-8")).hexdigest()
        receipt_path = self.receipts_dir / f"{manifest['transaction_id']}.json"
        _atomic_json(receipt_path, receipt)
        _safe_unlink(os.fspath(manifest_path))
        _fsync_directory(self.pending_dir)
        self._prune_receipts()

    def _prune_receipts(self, keep: int = 1000) -> None:
        receipts = sorted(
            self.receipts_dir.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale in receipts[keep:]:
            _safe_unlink(os.fspath(stale))


_JOURNAL: WorkspaceTransactionJournal | None = None
_JOURNAL_LOCK = threading.Lock()


def get_workspace_journal() -> WorkspaceTransactionJournal:
    global _JOURNAL
    if _JOURNAL is None:
        with _JOURNAL_LOCK:
            if _JOURNAL is None:
                _JOURNAL = WorkspaceTransactionJournal()
    return _JOURNAL
