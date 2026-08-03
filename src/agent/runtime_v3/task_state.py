from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import time
import uuid
from typing import Any, Iterable, Mapping, Sequence

from .ledger import DurableRunLedger, get_runtime_ledger


_MAX_OBJECTIVE_CHARS = 64_000
_MAX_PLAN_CHARS = 128_000
_MAX_FACT_CHARS = 16_000
_MAX_EVENT_CHARS = 64_000
_MAX_PROJECTION_CHARS = 24_000
_TERMINAL_TASK_STATES = {"completed", "cancelled", "failed", "interrupted", "incomplete"}
_SENSITIVE_TEXT = re.compile(
    r"(?i)(authorization\s*[:=]|api[_-]?key\s*[:=]|access[_-]?token\s*[:=]|refresh[_-]?token\s*[:=]|password\s*[:=])\s*([^\s,;]+)"
)


class TaskStateConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    owner: str | None
    session_id: str | None
    root_run_id: str
    objective: str
    status: str
    current_step_id: str | None
    next_action: str | None
    revision: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class TaskProjection:
    task_id: str
    revision: int
    message: Mapping[str, Any]
    sha256: str


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 80)] + f"\n… [truncated; original_chars={len(text)}]"


def _redact_text(value: Any) -> str:
    text = str(value or "")
    return _SENSITIVE_TEXT.sub(lambda match: match.group(1) + " [REDACTED]", text)


def _latest_user_text(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in reversed(messages):
        if str(message.get("role") or "") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, Mapping) and item.get("type") in {"text", "input_text"}:
                    parts.append(str(item.get("text") or ""))
            if parts:
                return "\n".join(parts)
        return str(content or "")
    return ""


def insert_task_projection(
    messages: Sequence[Mapping[str, Any]],
    projection_message: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Insert canonical state after system policy but before conversation turns."""
    result = [dict(message) for message in messages]
    index = 0
    while index < len(result) and result[index].get("role") == "system":
        index += 1
    result.insert(index, dict(projection_message))
    return result


class DurableTaskState:
    """Canonical, append-audited task state stored beside the run ledger."""

    def __init__(self, ledger: DurableRunLedger | None = None) -> None:
        self.ledger = ledger or get_runtime_ledger()
        with self.ledger._lock:
            self.ledger._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    owner TEXT,
                    session_id TEXT,
                    root_run_id TEXT NOT NULL UNIQUE,
                    objective TEXT NOT NULL,
                    objective_sha256 TEXT NOT NULL,
                    accepted_plan TEXT,
                    constraints_json TEXT NOT NULL,
                    acceptance_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step_id TEXT,
                    next_action TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_owner_updated
                    ON agent_tasks(owner, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_session_updated
                    ON agent_tasks(session_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS agent_task_runs (
                    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL UNIQUE REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                    relation TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(task_id, run_id)
                );

                CREATE TABLE IF NOT EXISTS agent_task_steps (
                    step_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    depends_json TEXT NOT NULL,
                    result_ref TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(task_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_task_steps_task
                    ON agent_task_steps(task_id, ordinal);

                CREATE TABLE IF NOT EXISTS agent_task_facts (
                    fact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    supersedes_fact_id TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_task_facts_task
                    ON agent_task_facts(task_id, created_at);

                CREATE TABLE IF NOT EXISTS agent_task_events (
                    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(task_id, seq)
                );

                CREATE TABLE IF NOT EXISTS agent_task_projections (
                    projection_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                    task_revision INTEGER NOT NULL,
                    projection_sha256 TEXT NOT NULL,
                    estimated_chars INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )

    def create_for_run(
        self,
        *,
        run_id: str,
        owner: str | None,
        session_id: str | None,
        messages: Sequence[Mapping[str, Any]],
        approved_plan: str | None = None,
        constraints: Mapping[str, Any] | None = None,
        acceptance_criteria: Sequence[Any] | None = None,
    ) -> TaskRecord:
        existing = self.for_run(run_id)
        if existing is not None:
            return existing
        objective = _clip(_redact_text(_latest_user_text(messages)), _MAX_OBJECTIVE_CHARS).strip()
        if not objective:
            objective = "Continue the accepted Agent task represented by the durable run."
        plan = _clip(_redact_text(approved_plan), _MAX_PLAN_CHARS) if approved_plan else None
        now = time.time()
        task_id = str(uuid.uuid4())
        constraints_json = _canonical(dict(constraints or {}))
        acceptance_json = _canonical(list(acceptance_criteria or ()))
        with self.ledger._tx() as db:
            db.execute(
                """INSERT INTO agent_tasks(
                       task_id,owner,session_id,root_run_id,objective,objective_sha256,
                       accepted_plan,constraints_json,acceptance_json,status,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    owner,
                    session_id,
                    str(run_id),
                    objective,
                    hashlib.sha256(objective.encode("utf-8")).hexdigest(),
                    plan,
                    constraints_json,
                    acceptance_json,
                    "active",
                    now,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO agent_task_runs(task_id,run_id,relation,created_at)
                   VALUES(?,?,?,?)""",
                (task_id, str(run_id), "root", now),
            )
        self.append_event(task_id, "task_created", {"run_id": str(run_id)})
        if plan:
            self.replace_plan(task_id, plan, expected_revision=1)
        return self.get(task_id)

    def attach_run(self, task_id: str, run_id: str, *, relation: str = "continuation") -> None:
        now = time.time()
        with self.ledger._tx() as db:
            db.execute(
                """INSERT OR IGNORE INTO agent_task_runs(task_id,run_id,relation,created_at)
                   VALUES(?,?,?,?)""",
                (str(task_id), str(run_id), str(relation), now),
            )
            db.execute(
                "UPDATE agent_tasks SET updated_at=?,revision=revision+1 WHERE task_id=?",
                (now, str(task_id)),
            )
        self.append_event(task_id, "run_attached", {"run_id": str(run_id), "relation": relation})

    def get(self, task_id: str) -> TaskRecord:
        with self.ledger._lock:
            row = self.ledger._conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=?", (str(task_id),)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return TaskRecord(
            task_id=row["task_id"],
            owner=row["owner"],
            session_id=row["session_id"],
            root_run_id=row["root_run_id"],
            objective=row["objective"],
            status=row["status"],
            current_step_id=row["current_step_id"],
            next_action=row["next_action"],
            revision=int(row["revision"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def for_run(self, run_id: str) -> TaskRecord | None:
        with self.ledger._lock:
            row = self.ledger._conn.execute(
                """SELECT t.* FROM agent_tasks t
                   JOIN agent_task_runs r ON r.task_id=t.task_id
                   WHERE r.run_id=?""",
                (str(run_id),),
            ).fetchone()
        if row is None:
            return None
        return TaskRecord(
            task_id=row["task_id"], owner=row["owner"], session_id=row["session_id"],
            root_run_id=row["root_run_id"], objective=row["objective"], status=row["status"],
            current_step_id=row["current_step_id"], next_action=row["next_action"],
            revision=int(row["revision"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def replace_plan(self, task_id: str, plan: str, *, expected_revision: int) -> TaskRecord:
        plan = _clip(_redact_text(plan), _MAX_PLAN_CHARS)
        steps = self._plan_steps(plan)
        now = time.time()
        with self.ledger._tx() as db:
            cursor = db.execute(
                """UPDATE agent_tasks SET accepted_plan=?,updated_at=?,revision=revision+1
                   WHERE task_id=? AND revision=? AND status NOT IN ('completed','cancelled','failed')""",
                (plan, now, str(task_id), int(expected_revision)),
            )
            if cursor.rowcount != 1:
                raise TaskStateConflict("task revision changed or task is terminal")
            db.execute("DELETE FROM agent_task_steps WHERE task_id=?", (str(task_id),))
            for ordinal, title in enumerate(steps, start=1):
                db.execute(
                    """INSERT INTO agent_task_steps(
                           step_id,task_id,ordinal,title,description,status,depends_json,
                           created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()), str(task_id), ordinal, title, title, "pending",
                        _canonical([ordinal - 1] if ordinal > 1 else []), now, now,
                    ),
                )
        self.append_event(task_id, "plan_replaced", {"step_count": len(steps), "plan_sha256": _hash(plan)})
        return self.get(task_id)

    def set_step_status(
        self,
        task_id: str,
        step_id: str,
        *,
        status: str,
        result_ref: str | None = None,
        expected_revision: int | None = None,
    ) -> None:
        if status not in {"pending", "running", "completed", "failed", "blocked", "skipped"}:
            raise ValueError(f"invalid step status: {status}")
        now = time.time()
        with self.ledger._tx() as db:
            params: list[Any] = [status, result_ref, now, str(step_id), str(task_id)]
            where = "step_id=? AND task_id=?"
            if expected_revision is not None:
                where += " AND revision=?"
                params.append(int(expected_revision))
            cursor = db.execute(
                f"""UPDATE agent_task_steps SET status=?,result_ref=?,updated_at=?,revision=revision+1
                    WHERE {where}""",
                tuple(params),
            )
            if cursor.rowcount != 1:
                raise TaskStateConflict("step revision changed or step does not exist")
            current = str(step_id) if status == "running" else None
            db.execute(
                """UPDATE agent_tasks SET current_step_id=COALESCE(?,current_step_id),
                          updated_at=?,revision=revision+1 WHERE task_id=?""",
                (current, now, str(task_id)),
            )
        self.append_event(task_id, "step_status", {"step_id": step_id, "status": status, "result_ref": result_ref})

    def add_fact(
        self,
        task_id: str,
        *,
        category: str,
        content: str,
        source_type: str,
        source_ref: str | None = None,
        confidence: float = 1.0,
        status: str = "asserted",
        supersedes_fact_id: str | None = None,
    ) -> str:
        if status not in {"asserted", "verified", "retracted"}:
            raise ValueError(f"invalid fact status: {status}")
        normalized = _clip(_redact_text(content), _MAX_FACT_CHARS).strip()
        if not normalized:
            raise ValueError("fact content is empty")
        fact_id = str(uuid.uuid4())
        now = time.time()
        with self.ledger._tx() as db:
            db.execute(
                """INSERT INTO agent_task_facts(
                       fact_id,task_id,category,content,content_sha256,source_type,
                       source_ref,confidence,status,supersedes_fact_id,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fact_id, str(task_id), _clip(category, 200), normalized,
                    hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                    _clip(source_type, 100), _clip(source_ref, 1000) if source_ref else None,
                    max(0.0, min(1.0, float(confidence))), status,
                    supersedes_fact_id, now,
                ),
            )
            db.execute(
                "UPDATE agent_tasks SET updated_at=?,revision=revision+1 WHERE task_id=?",
                (now, str(task_id)),
            )
        self.append_event(task_id, "fact_added", {"fact_id": fact_id, "category": category, "status": status})
        return fact_id

    def set_next_action(self, task_id: str, next_action: str | None) -> None:
        value = _clip(_redact_text(next_action), 4000) if next_action else None
        with self.ledger._tx() as db:
            db.execute(
                """UPDATE agent_tasks SET next_action=?,updated_at=?,revision=revision+1
                   WHERE task_id=?""",
                (value, time.time(), str(task_id)),
            )
        self.append_event(task_id, "next_action", {"next_action": value})

    def mark_status(self, task_id: str, status: str, *, reason: str | None = None) -> None:
        if status not in {"active", "blocked", "completed", "cancelled", "failed", "interrupted", "incomplete"}:
            raise ValueError(f"invalid task status: {status}")
        with self.ledger._tx() as db:
            db.execute(
                """UPDATE agent_tasks SET status=?,updated_at=?,revision=revision+1
                   WHERE task_id=?""",
                (status, time.time(), str(task_id)),
            )
        self.append_event(task_id, "task_status", {"status": status, "reason": _clip(reason, 1000) if reason else None})

    def append_event(self, task_id: str, event_type: str, payload: Mapping[str, Any]) -> int:
        safe_payload = _clip(_redact_text(_canonical(dict(payload))), _MAX_EVENT_CHARS)
        with self.ledger._tx() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM agent_task_events WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
            seq = int(row[0])
            db.execute(
                """INSERT INTO agent_task_events(task_id,seq,event_type,payload_json,created_at)
                   VALUES(?,?,?,?,?)""",
                (str(task_id), seq, str(event_type), safe_payload, time.time()),
            )
        return seq

    def observe_runtime_event(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        task = self.for_run(run_id)
        if task is None:
            return
        self.append_event(task.task_id, f"runtime:{event_type}", dict(payload))
        if event_type == "agent_step":
            round_number = payload.get("round")
            if round_number is not None:
                self.set_next_action(task.task_id, f"Continue Agent round {round_number} using durable task state.")
        elif event_type == "tool_output":
            tool = str(payload.get("tool") or "tool")
            exit_code = payload.get("exit_code")
            output = _clip(payload.get("output"), 4000)
            self.add_fact(
                task.task_id,
                category="tool_observation",
                content=f"{tool} exit_code={exit_code}\n{output}",
                source_type="runtime_event",
                source_ref=f"run:{run_id}",
                confidence=1.0 if exit_code in {0, "0", None} else 0.7,
                status="asserted",
            )
        elif event_type == "run_state" and payload.get("terminal"):
            state = str(payload.get("state") or payload.get("status") or "incomplete")
            status = state if state in _TERMINAL_TASK_STATES else "incomplete"
            self.mark_status(task.task_id, status, reason=str(payload.get("reason") or ""))

    def projection_for_run(self, run_id: str, *, max_chars: int = _MAX_PROJECTION_CHARS) -> TaskProjection:
        task = self.for_run(run_id)
        if task is None:
            raise KeyError(run_id)
        with self.ledger._lock:
            row = self.ledger._conn.execute(
                "SELECT accepted_plan,constraints_json,acceptance_json FROM agent_tasks WHERE task_id=?",
                (task.task_id,),
            ).fetchone()
            steps = self.ledger._conn.execute(
                """SELECT step_id,ordinal,title,status,result_ref FROM agent_task_steps
                   WHERE task_id=? ORDER BY ordinal""",
                (task.task_id,),
            ).fetchall()
            facts = self.ledger._conn.execute(
                """SELECT fact_id,category,content,source_type,source_ref,confidence,status
                   FROM agent_task_facts WHERE task_id=? AND status!='retracted'
                   ORDER BY created_at DESC LIMIT 40""",
                (task.task_id,),
            ).fetchall()
        document = {
            "schema": "odysseus.runtime.task_state.v1",
            "task_id": task.task_id,
            "revision": task.revision,
            "objective": task.objective,
            "accepted_plan": row["accepted_plan"],
            "constraints": json.loads(row["constraints_json"]),
            "acceptance_criteria": json.loads(row["acceptance_json"]),
            "status": task.status,
            "current_step_id": task.current_step_id,
            "next_action": task.next_action,
            "steps": [dict(item) for item in steps],
            "facts": [dict(item) for item in reversed(facts)],
            "instructions": [
                "Treat this durable state as canonical execution memory.",
                "Do not infer that an unknown external effect rolled back.",
                "Conversation history may be compacted; preserve objective, constraints, accepted plan, and verified facts.",
                "Update progress through tools/runtime events rather than rewriting history.",
            ],
        }
        body = _clip(_canonical(document), max(2000, min(_MAX_PROJECTION_CHARS, int(max_chars))))
        message = {
            "role": "system",
            "content": "DURABLE TASK STATE (canonical; generated by Runtime V3)\n" + body,
        }
        digest = _hash(message)
        with self.ledger._tx() as db:
            db.execute(
                """INSERT INTO agent_task_projections(
                       projection_id,task_id,run_id,task_revision,projection_sha256,
                       estimated_chars,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), task.task_id, str(run_id), task.revision,
                    digest, len(message["content"]), time.time(),
                ),
            )
        return TaskProjection(task.task_id, task.revision, message, digest)

    def detail_for_run(self, run_id: str) -> dict[str, Any] | None:
        task = self.for_run(run_id)
        if task is None:
            return None
        projection = self.projection_for_run(run_id)
        return {
            "task_id": task.task_id,
            "objective": task.objective,
            "status": task.status,
            "current_step_id": task.current_step_id,
            "next_action": task.next_action,
            "revision": task.revision,
            "projection_sha256": projection.sha256,
        }

    @staticmethod
    def _plan_steps(plan: str) -> list[str]:
        steps: list[str] = []
        for raw in str(plan or "").splitlines():
            line = raw.strip()
            line = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+)", "", line).strip()
            if len(line) >= 3:
                steps.append(_clip(line, 1000))
            if len(steps) >= 200:
                break
        return steps or ["Execute the accepted plan and verify the requested outcome."]


_TASK_STATE: DurableTaskState | None = None


def get_task_state() -> DurableTaskState:
    global _TASK_STATE
    if _TASK_STATE is None:
        _TASK_STATE = DurableTaskState()
    return _TASK_STATE
