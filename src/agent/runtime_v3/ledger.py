from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterator, Mapping

from .config import load_runtime_v3_limits
from .contracts import EffectClass, EffectLease, EffectStatus, RetryPolicy, RunRecord, RunStatus


_ALLOWED_TRANSITIONS = {
    RunStatus.CREATED: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_USER, RunStatus.WAITING_APPROVAL, RunStatus.CANCELLING,
        RunStatus.COMPLETED, RunStatus.INCOMPLETE, RunStatus.CANCELLED,
        RunStatus.FAILED, RunStatus.INTERRUPTED,
    },
    RunStatus.WAITING_USER: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.INTERRUPTED},
    RunStatus.WAITING_APPROVAL: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.INTERRUPTED},
    RunStatus.CANCELLING: {RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.INTERRUPTED},
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _default_path() -> Path:
    configured = os.getenv("ODYSSEUS_RUNTIME_V3_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    root = os.getenv("ODYSSEUS_RUNTIME_STATE_DIR", "").strip()
    if not root:
        try:
            from src.constants import DATA_DIR
            root = str(DATA_DIR)
        except Exception:
            root = "data"
    return Path(root).expanduser() / "agent-runtime-v3.sqlite3"


class DurableRunLedger:
    """SQLite-backed run/effect/event ledger with crash-safe transactions.

    The ledger never guesses whether an interrupted effect committed. Any
    effect left in STARTED at recovery becomes UNKNOWN and is not retryable
    without an explicit reconciliation step.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else _default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        # sqlite3.executescript() controls its own transaction boundary. Running
        # it inside _tx() can leave the outer COMMIT with no active transaction.
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    owner TEXT,
                    workload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    terminal_reason TEXT,
                    resumable INTEGER NOT NULL DEFAULT 0,
                    request_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    limits_json TEXT NOT NULL,
                    selected_model TEXT,
                    selected_endpoint TEXT,
                    process_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    base_event_seq INTEGER NOT NULL DEFAULT 1,
                    last_event_seq INTEGER NOT NULL DEFAULT 0,
                    event_bytes INTEGER NOT NULL DEFAULT 0,
                    error_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status, heartbeat_at);
                CREATE TABLE IF NOT EXISTS agent_events (
                    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
                CREATE TABLE IF NOT EXISTS agent_effects (
                    effect_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                    tool_name TEXT NOT NULL,
                    idempotency_key TEXT,
                    effect_class TEXT NOT NULL,
                    retry_policy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    started_at REAL,
                    finished_at REAL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(run_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_effects_run ON agent_effects(run_id, status);
                CREATE TABLE IF NOT EXISTS agent_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                    event_seq INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_observations (
                    observation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(agent_runs)").fetchall()
            }
            if "base_event_seq" not in columns:
                self._conn.execute(
                    "ALTER TABLE agent_runs ADD COLUMN base_event_seq INTEGER NOT NULL DEFAULT 1"
                )
            if "event_bytes" not in columns:
                self._conn.execute(
                    "ALTER TABLE agent_runs ADD COLUMN event_bytes INTEGER NOT NULL DEFAULT 0"
                )
                self._conn.execute(
                    """UPDATE agent_runs SET event_bytes=COALESCE((
                           SELECT SUM(length(CAST(payload_json AS BLOB)))
                           FROM agent_events WHERE agent_events.run_id=agent_runs.run_id
                       ),0)"""
                )

    def create_run(self, *, run_id: str, session_id: str | None, owner: str | None,
                   workload: str, request: Mapping[str, Any], limits: Mapping[str, Any],
                   model: str | None, endpoint: str | None) -> RunRecord:
        now = time.time()
        request_json = _canonical(request)
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        with self._tx() as db:
            existing = db.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise RuntimeError(f"run_id collision with different request: {run_id}")
                return self._record(existing)
            db.execute(
                """INSERT INTO agent_runs(
                    run_id, session_id, owner, workload, status, request_hash,
                    request_json, limits_json, selected_model, selected_endpoint,
                    process_id, created_at, updated_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, session_id, owner, workload, RunStatus.CREATED.value,
                 request_hash, request_json, _canonical(limits), model, endpoint,
                 os.getpid(), now, now, now),
            )
            return self._record(db.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone())

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._record(row) if row else None

    def latest_run_for_session(self, session_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_runs WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (str(session_id),),
            ).fetchone()
        return self._record(row) if row else None

    def session_run_relation(self, session_id: str, run_id: str) -> str:
        latest = self.latest_run_for_session(session_id)
        if latest is None:
            return "unknown"
        return "current" if latest.run_id == str(run_id) else "superseded"

    def transition(self, run_id: str, status: RunStatus, *, reason: str | None = None,
                   resumable: bool | None = None, expected_revision: int | None = None,
                   error: Mapping[str, Any] | None = None) -> RunRecord:
        now = time.time()
        with self._tx() as db:
            row = db.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = RunStatus(row["status"])
            if expected_revision is not None and row["revision"] != expected_revision:
                raise RuntimeError("run revision conflict")
            if current != status and status not in _ALLOWED_TRANSITIONS.get(current, set()):
                raise RuntimeError(f"illegal run transition {current.value} -> {status.value}")
            next_resumable = int(row["resumable"] if resumable is None else bool(resumable))
            db.execute(
                """UPDATE agent_runs SET status=?, terminal_reason=?, resumable=?,
                   error_json=?, updated_at=?, heartbeat_at=?, revision=revision+1
                   WHERE run_id=?""",
                (status.value, reason, next_resumable,
                 _canonical(error) if error else None, now, now, run_id),
            )
            return self._record(db.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone())

    def heartbeat(self, run_id: str) -> None:
        now = time.time()
        with self._tx() as db:
            db.execute("UPDATE agent_runs SET heartbeat_at=?, updated_at=? WHERE run_id=?", (now, now, run_id))

    def append_event(self, run_id: str, event_type: str, payload: Any, *, max_bytes: int = 1_048_576) -> int:
        payload_json = _canonical(payload)
        encoded = payload_json.encode("utf-8")
        if len(encoded) > max_bytes:
            raise ValueError(f"event payload exceeds {max_bytes} bytes")
        limits = load_runtime_v3_limits()
        if len(encoded) > limits.max_replay_bytes:
            raise ValueError(
                f"event payload exceeds total replay byte budget {limits.max_replay_bytes}"
            )
        now = time.time()
        with self._tx() as db:
            row = db.execute(
                "SELECT base_event_seq,last_event_seq,event_bytes FROM agent_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            base_seq = max(1, int(row["base_event_seq"] or 1))
            seq = int(row["last_event_seq"]) + 1
            retained_bytes = int(row["event_bytes"] or 0) + len(encoded)
            db.execute(
                "INSERT INTO agent_events(run_id, seq, event_type, payload_json, payload_sha256, created_at) VALUES(?,?,?,?,?,?)",
                (run_id, seq, event_type, payload_json, hashlib.sha256(encoded).hexdigest(), now),
            )
            while (
                seq - base_seq + 1 > limits.max_replay_events
                or retained_bytes > limits.max_replay_bytes
            ):
                oldest = db.execute(
                    """SELECT seq,length(CAST(payload_json AS BLOB)) AS payload_bytes
                       FROM agent_events WHERE run_id=? ORDER BY seq LIMIT 1""",
                    (run_id,),
                ).fetchone()
                if oldest is None or int(oldest["seq"]) >= seq:
                    break
                db.execute(
                    "DELETE FROM agent_events WHERE run_id=? AND seq=?",
                    (run_id, int(oldest["seq"])),
                )
                retained_bytes = max(0, retained_bytes - int(oldest["payload_bytes"] or 0))
                base_seq = int(oldest["seq"]) + 1
            db.execute(
                """UPDATE agent_runs SET base_event_seq=?,last_event_seq=?,event_bytes=?,
                          heartbeat_at=?,updated_at=?,revision=revision+1 WHERE run_id=?""",
                (base_seq, seq, retained_bytes, now, now, run_id),
            )
            return seq

    def event_window(self, run_id: str) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT base_event_seq,last_event_seq,event_bytes FROM agent_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return {
            "available_from_seq": max(1, int(row["base_event_seq"] or 1)),
            "last_event_seq": int(row["last_event_seq"] or 0),
            "retained_bytes": int(row["event_bytes"] or 0),
        }

    def replay(self, run_id: str, *, after_seq: int = 0, limit: int = 8192) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq,event_type,payload_json,created_at FROM agent_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after_seq, max(1, min(100_000, int(limit)))),
            ).fetchall()
        return [{"seq": r["seq"], "type": r["event_type"], "payload": json.loads(r["payload_json"]),
                 "created_at": r["created_at"]} for r in rows]

    def begin_effect(self, *, run_id: str, tool_name: str, arguments: Mapping[str, Any],
                     effect_class: EffectClass, retry_policy: RetryPolicy,
                     idempotency_key: str | None = None) -> EffectLease:
        request_hash = _digest(arguments)
        now = time.time()
        with self._tx() as db:
            if idempotency_key:
                row = db.execute(
                    "SELECT * FROM agent_effects WHERE run_id=? AND idempotency_key=?",
                    (run_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    if row["request_sha256"] != request_hash or row["tool_name"] != tool_name:
                        raise RuntimeError("idempotency key reused for a different effect")
                    status = EffectStatus(row["status"])
                    stored_policy = RetryPolicy(row["retry_policy"])
                    if (
                        status is EffectStatus.FAILED
                        and stored_policy is RetryPolicy.SAFE
                        and retry_policy is RetryPolicy.SAFE
                    ):
                        db.execute(
                            """UPDATE agent_effects SET status=?,result_json=NULL,error_json=NULL,
                               started_at=?,finished_at=NULL,attempt=attempt+1,revision=revision+1
                               WHERE effect_id=?""",
                            (EffectStatus.STARTED.value, now, row["effect_id"]),
                        )
                        return EffectLease(
                            row["effect_id"], True, EffectStatus.STARTED, None,
                            "safe_retry_after_proven_failure",
                        )
                    cached = json.loads(row["result_json"]) if row["result_json"] else None
                    return EffectLease(row["effect_id"], False, status, cached, "duplicate")
            effect_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO agent_effects(
                    effect_id,run_id,tool_name,idempotency_key,effect_class,retry_policy,
                    status,request_sha256,request_json,started_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (effect_id, run_id, tool_name, idempotency_key, effect_class.value,
                 retry_policy.value, EffectStatus.STARTED.value, request_hash,
                 _canonical(arguments), now),
            )
            return EffectLease(effect_id, True, EffectStatus.STARTED)

    def finish_effect(self, effect_id: str, result: Mapping[str, Any]) -> None:
        self._finish_effect(effect_id, EffectStatus.COMMITTED, result=result)

    def fail_effect(self, effect_id: str, error: Mapping[str, Any]) -> None:
        self._finish_effect(effect_id, EffectStatus.FAILED, error=error)

    def mark_effect_unknown(self, effect_id: str, error: Mapping[str, Any] | None = None) -> None:
        self._finish_effect(effect_id, EffectStatus.UNKNOWN,
                            error=error or {"reason": "interrupted_after_start"})

    def _finish_effect(self, effect_id: str, status: EffectStatus, *, result=None, error=None) -> None:
        now = time.time()
        with self._tx() as db:
            row = db.execute("SELECT status FROM agent_effects WHERE effect_id=?", (effect_id,)).fetchone()
            if row is None:
                raise KeyError(effect_id)
            current = EffectStatus(row[0])
            if current is not EffectStatus.STARTED:
                raise RuntimeError(f"effect is already terminal: {current.value}")
            db.execute(
                """UPDATE agent_effects SET status=?,result_json=?,error_json=?,finished_at=?,revision=revision+1
                   WHERE effect_id=?""",
                (status.value, _canonical(result) if result is not None else None,
                 _canonical(error) if error is not None else None, now, effect_id),
            )

    def checkpoint(self, run_id: str, state: Mapping[str, Any]) -> str:
        checkpoint_id = str(uuid.uuid4())
        with self._tx() as db:
            row = db.execute("SELECT last_event_seq FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            db.execute(
                "INSERT INTO agent_checkpoints(checkpoint_id,run_id,event_seq,state_json,created_at) VALUES(?,?,?,?,?)",
                (checkpoint_id, run_id, int(row[0]), _canonical(state), time.time()),
            )
        return checkpoint_id

    def store_observation(self, run_id: str, kind: str, content: str) -> dict[str, Any]:
        data = content.encode("utf-8", errors="replace")
        observation_id = str(uuid.uuid4())
        digest = hashlib.sha256(data).hexdigest()
        with self._tx() as db:
            db.execute(
                "INSERT INTO agent_observations(observation_id,run_id,kind,sha256,size_bytes,content,created_at) VALUES(?,?,?,?,?,?,?)",
                (observation_id, run_id, kind, digest, len(data), content, time.time()),
            )
        return {"observation_id": observation_id, "kind": kind, "sha256": digest, "size_bytes": len(data)}

    def recover_interrupted(self, *, stale_after_seconds: float = 0.0) -> list[str]:
        cutoff = time.time() - max(0.0, stale_after_seconds)
        recovered: list[str] = []
        with self._tx() as db:
            rows = db.execute(
                "SELECT run_id FROM agent_runs WHERE status IN (?,?,?,?) AND (process_id != ? OR heartbeat_at <= ?)",
                (RunStatus.CREATED.value, RunStatus.RUNNING.value, RunStatus.CANCELLING.value,
                 RunStatus.WAITING_APPROVAL.value, os.getpid(), cutoff),
            ).fetchall()
            for row in rows:
                run_id = row[0]
                db.execute(
                    "UPDATE agent_runs SET status=?,terminal_reason=?,resumable=1,updated_at=?,revision=revision+1 WHERE run_id=?",
                    (RunStatus.INTERRUPTED.value, "process_interrupted", time.time(), run_id),
                )
                db.execute(
                    "UPDATE agent_effects SET status=?,error_json=?,finished_at=?,revision=revision+1 WHERE run_id=? AND status=?",
                    (EffectStatus.UNKNOWN.value,
                     _canonical({"reason": "process_interrupted_after_effect_start"}),
                     time.time(), run_id, EffectStatus.STARTED.value),
                )
                recovered.append(run_id)
        return recovered

    @staticmethod
    def _record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"], session_id=row["session_id"], owner=row["owner"],
            status=RunStatus(row["status"]), revision=int(row["revision"]),
            last_event_seq=int(row["last_event_seq"]), terminal_reason=row["terminal_reason"],
            resumable=bool(row["resumable"]),
        )


_LEDGER: DurableRunLedger | None = None
_LEDGER_LOCK = threading.Lock()


def get_runtime_ledger() -> DurableRunLedger:
    global _LEDGER
    if _LEDGER is None:
        with _LEDGER_LOCK:
            if _LEDGER is None:
                _LEDGER = DurableRunLedger()
                _LEDGER.recover_interrupted(stale_after_seconds=300)
    return _LEDGER
