from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Mapping

from .contracts import EffectStatus, RunStatus
from .ledger import DurableRunLedger, get_runtime_ledger
from .routing import trust_domain


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _endpoint_summary(url: str | None) -> dict[str, str] | None:
    if not url:
        return None
    return trust_domain(url).public_dict()


def _owner_key(owner: str | None) -> str:
    return str(owner or "").strip()


class RuntimeAccessDenied(PermissionError):
    pass


class RuntimeConflict(RuntimeError):
    pass


class RuntimeOperations:
    """Owner-scoped operational API for the durable Agent ledger."""

    def __init__(self, ledger: DurableRunLedger | None = None) -> None:
        self.ledger = ledger or get_runtime_ledger()
        with self.ledger._tx() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_commands (
                    command_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                    command_type TEXT NOT NULL,
                    requested_by TEXT,
                    reason TEXT,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    completed_at REAL,
                    result_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_commands_run
                    ON agent_commands(run_id, created_at DESC);
                """
            )

    def _owned_row(self, run_id: str, owner: str | None):
        with self.ledger._lock:
            row = self.ledger._conn.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        stored = _owner_key(row["owner"])
        caller = _owner_key(owner)
        if stored != caller:
            raise RuntimeAccessDenied("run is not owned by the current user")
        return row

    def list_runs(
        self,
        *,
        owner: str | None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        before: float | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["COALESCE(owner, '') = ?"]
        params: list[Any] = [_owner_key(owner)]
        if session_id:
            clauses.append("session_id = ?")
            params.append(str(session_id))
        if status:
            try:
                normalized = RunStatus(str(status)).value
            except ValueError as exc:
                raise ValueError(f"invalid run status: {status}") from exc
            clauses.append("status = ?")
            params.append(normalized)
        if before is not None:
            clauses.append("created_at < ?")
            params.append(float(before))
        params.append(max(1, min(200, int(limit))))
        query = (
            "SELECT * FROM agent_runs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        )
        with self.ledger._lock:
            rows = self.ledger._conn.execute(query, tuple(params)).fetchall()
        return [self._run_summary(row) for row in rows]

    def get_run(self, run_id: str, *, owner: str | None) -> dict[str, Any]:
        row = self._owned_row(run_id, owner)
        with self.ledger._lock:
            effects = self.ledger._conn.execute(
                """SELECT effect_id,tool_name,effect_class,retry_policy,status,
                          request_json,result_json,error_json,started_at,finished_at,
                          attempt,revision
                   FROM agent_effects WHERE run_id=? ORDER BY started_at,effect_id""",
                (str(run_id),),
            ).fetchall()
            commands = self.ledger._conn.execute(
                """SELECT command_id,command_type,requested_by,reason,status,
                          created_at,completed_at,result_json
                   FROM agent_commands WHERE run_id=? ORDER BY created_at""",
                (str(run_id),),
            ).fetchall()
            checkpoints = self.ledger._conn.execute(
                """SELECT checkpoint_id,event_seq,created_at
                   FROM agent_checkpoints WHERE run_id=? ORDER BY created_at""",
                (str(run_id),),
            ).fetchall()
            observations = self.ledger._conn.execute(
                """SELECT observation_id,kind,sha256,size_bytes,created_at
                   FROM agent_observations WHERE run_id=? ORDER BY created_at""",
                (str(run_id),),
            ).fetchall()
        summary = self._run_summary(row)
        summary.update(
            {
                "request": _json(row["request_json"], {}),
                "request_sha256": row["request_hash"],
                "limits": _json(row["limits_json"], {}),
                "effects": [
                    {
                        "effect_id": effect["effect_id"],
                        "tool_name": effect["tool_name"],
                        "effect_class": effect["effect_class"],
                        "retry_policy": effect["retry_policy"],
                        "status": effect["status"],
                        "request": _json(effect["request_json"], {}),
                        "result": _json(effect["result_json"], None),
                        "error": _json(effect["error_json"], None),
                        "started_at": effect["started_at"],
                        "finished_at": effect["finished_at"],
                        "attempt": effect["attempt"],
                        "revision": effect["revision"],
                    }
                    for effect in effects
                ],
                "commands": [
                    {
                        "command_id": command["command_id"],
                        "type": command["command_type"],
                        "requested_by": command["requested_by"],
                        "reason": command["reason"],
                        "status": command["status"],
                        "created_at": command["created_at"],
                        "completed_at": command["completed_at"],
                        "result": _json(command["result_json"], None),
                    }
                    for command in commands
                ],
                "checkpoints": [dict(item) for item in checkpoints],
                "observations": [dict(item) for item in observations],
            }
        )
        return summary

    def events(
        self,
        run_id: str,
        *,
        owner: str | None,
        after_seq: int = 0,
        limit: int = 8192,
    ) -> list[dict[str, Any]]:
        self._owned_row(run_id, owner)
        return self.ledger.replay(
            str(run_id),
            after_seq=max(0, int(after_seq)),
            limit=max(1, min(100_000, int(limit))),
        )

    def request_cancel(self, run_id: str, *, owner: str | None, reason: str) -> dict[str, Any]:
        row = self._owned_row(run_id, owner)
        current = RunStatus(row["status"])
        if current.terminal:
            raise RuntimeConflict(f"run is already terminal: {current.value}")
        command_id = str(uuid.uuid4())
        now = time.time()
        reason = str(reason or "user_requested").strip()[:1000]
        with self.ledger._tx() as db:
            db.execute(
                """INSERT INTO agent_commands(
                       command_id,run_id,command_type,requested_by,reason,status,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (command_id, str(run_id), "cancel", _owner_key(owner), reason, "accepted", now),
            )
        self.ledger.append_event(
            str(run_id),
            "cancel_requested",
            {"command_id": command_id, "reason": reason},
        )
        if current is RunStatus.RUNNING:
            try:
                self.ledger.transition(str(run_id), RunStatus.CANCELLING, reason=reason)
            except RuntimeError:
                pass

        from src import agent_runs

        stopped = bool(row["session_id"] and agent_runs.stop(str(row["session_id"])))
        final_status = "dispatched" if stopped else "completed"
        result = {"active_task_cancelled": stopped}
        if not stopped:
            refreshed = self.ledger.get_run(str(run_id))
            if refreshed and not refreshed.status.terminal:
                self.ledger.transition(
                    str(run_id),
                    RunStatus.CANCELLED,
                    reason="cancelled_without_active_worker",
                    resumable=False,
                )
        with self.ledger._tx() as db:
            db.execute(
                """UPDATE agent_commands SET status=?,completed_at=?,result_json=?
                   WHERE command_id=?""",
                (final_status, time.time(), json.dumps(result, sort_keys=True), command_id),
            )
        return {"command_id": command_id, "status": final_status, **result}

    def reconcile_effect(
        self,
        run_id: str,
        effect_id: str,
        *,
        owner: str | None,
        outcome: str,
        note: str,
        expected_revision: int,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._owned_row(run_id, owner)
        try:
            target = EffectStatus(str(outcome))
        except ValueError as exc:
            raise ValueError("outcome must be committed or failed") from exc
        if target not in {EffectStatus.COMMITTED, EffectStatus.FAILED}:
            raise ValueError("outcome must be committed or failed")
        note = str(note or "").strip()
        if len(note) < 8:
            raise ValueError("a reconciliation note of at least 8 characters is required")
        record = {
            "manual_reconciliation": True,
            "outcome": target.value,
            "note": note[:4000],
            "evidence": dict(evidence or {}),
            "reconciled_by": _owner_key(owner),
            "reconciled_at": time.time(),
        }
        with self.ledger._tx() as db:
            row = db.execute(
                "SELECT status,revision,run_id FROM agent_effects WHERE effect_id=?",
                (str(effect_id),),
            ).fetchone()
            if row is None or row["run_id"] != str(run_id):
                raise KeyError(effect_id)
            if EffectStatus(row["status"]) is not EffectStatus.UNKNOWN:
                raise RuntimeConflict("only unknown effects can be manually reconciled")
            if int(row["revision"]) != int(expected_revision):
                raise RuntimeConflict("effect revision changed; refresh before reconciling")
            cursor = db.execute(
                """UPDATE agent_effects SET status=?,error_json=?,finished_at=?,revision=revision+1
                   WHERE effect_id=? AND revision=? AND status=?""",
                (
                    target.value,
                    json.dumps(record, sort_keys=True, separators=(",", ":")),
                    time.time(),
                    str(effect_id),
                    int(expected_revision),
                    EffectStatus.UNKNOWN.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeConflict("effect reconciliation lost a concurrent update race")
        self.ledger.append_event(
            str(run_id),
            "effect_reconciled",
            {
                "effect_id": str(effect_id),
                "outcome": target.value,
                "note_sha256": hashlib.sha256(note.encode("utf-8")).hexdigest(),
            },
        )
        return {"effect_id": str(effect_id), "status": target.value, "revision": int(expected_revision) + 1}

    @staticmethod
    def _run_summary(row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "session_id": row["session_id"],
            "workload": row["workload"],
            "status": row["status"],
            "terminal_reason": row["terminal_reason"],
            "resumable": bool(row["resumable"]),
            "model": row["selected_model"],
            "endpoint": _endpoint_summary(row["selected_endpoint"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "heartbeat_at": row["heartbeat_at"],
            "revision": row["revision"],
            "last_event_seq": row["last_event_seq"],
            "error": _json(row["error_json"], None),
        }


_OPERATIONS: RuntimeOperations | None = None


def get_runtime_operations() -> RuntimeOperations:
    global _OPERATIONS
    if _OPERATIONS is None:
        _OPERATIONS = RuntimeOperations()
    return _OPERATIONS
