"""Single canonical planning backend for the agent runtime.

Root cause context: ``todowrite`` (``src/agent_tools/coding_tools.py``) and
``update_plan`` (``src/agent_tools/interaction_tools.py``) were two
independent planning systems. ``todowrite`` persisted a structured todo
list to a per-session JSON file; ``update_plan`` persisted nothing at all
server-side — it just relayed a raw markdown checklist string through an
SSE ``plan_update`` event for the frontend to hold in memory, recoverable
only by re-scanning conversation history for the last such tool call. A
session had no single, versioned, reloadable record of "what is the plan
right now" — see ``specs/agent-runtime-v2-progress.md``.

This module is that single record. ``PlanService`` owns a typed
``Plan``/``PlanStep`` model with atomic, session-scoped, versioned
persistence. ``todowrite`` and ``update_plan`` (see
``src/agent_tools/coding_tools.py`` / ``interaction_tools.py``) are now
thin adapters that translate their legacy wire formats into
``PlanService.replace()`` calls and read back through it — they hold no
independent state of their own. ``manage_plan`` (new; registered in
``src/agent_tools/planning_tools.py``) exposes the full action surface
(add/update/complete/block/skip/remove/reorder/...) directly to the model
for partial updates that don't require resending the whole plan.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, replace as _dc_replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from src.constants import DATA_DIR


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class PlanStepPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class PlanStep:
    id: str
    content: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    priority: PlanStepPriority = PlanStepPriority.MEDIUM
    depends_on: tuple[str, ...] = ()
    acceptance_criteria: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    notes: str = ""
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    completed_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status.value,
            "priority": self.priority.value,
            "depends_on": list(self.depends_on),
            "acceptance_criteria": self.acceptance_criteria,
            "evidence": dict(self.evidence),
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlanStep":
        return cls(
            id=str(data["id"]),
            content=str(data.get("content", "")),
            status=PlanStepStatus(data.get("status", "pending")),
            priority=PlanStepPriority(data.get("priority", "medium")),
            depends_on=tuple(data.get("depends_on") or ()),
            acceptance_criteria=str(data.get("acceptance_criteria", "")),
            evidence=dict(data.get("evidence") or {}),
            notes=str(data.get("notes", "")),
            created_at=float(data.get("created_at") or _now()),
            updated_at=float(data.get("updated_at") or _now()),
            completed_at=data.get("completed_at"),
        )


@dataclass
class Plan:
    id: str
    owner_id: Optional[str]
    session_id: str
    title: str = ""
    summary: str = ""
    version: int = 1
    steps: tuple[PlanStep, ...] = ()
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    archived_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "title": self.title,
            "summary": self.summary,
            "version": self.version,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Plan":
        return cls(
            id=str(data["id"]),
            owner_id=data.get("owner_id"),
            session_id=str(data.get("session_id", "")),
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            version=int(data.get("version", 1)),
            steps=tuple(PlanStep.from_dict(s) for s in data.get("steps") or ()),
            created_at=float(data.get("created_at") or _now()),
            updated_at=float(data.get("updated_at") or _now()),
            archived_at=data.get("archived_at"),
        )

    def step(self, step_id: str) -> Optional[PlanStep]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None


class PlanNotFound(Exception):
    """No plan exists yet for this owner/session."""


class PlanStepNotFound(Exception):
    def __init__(self, step_id: str):
        super().__init__(f"No such plan step: {step_id}")
        self.step_id = step_id


class PlanVersionConflict(Exception):
    """A caller's expected_version is stale — includes the latest plan."""

    def __init__(self, expected_version: Optional[int], latest: Plan):
        super().__init__(
            f"Plan version conflict: expected {expected_version}, "
            f"latest is {latest.version}"
        )
        self.expected_version = expected_version
        self.latest = latest


def _safe_key_part(value: Optional[str], default: str) -> str:
    value = (value or default).strip() or default
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120]


class PlanService:
    """Atomic, versioned, owner/session-scoped plan persistence.

    File-backed by default (one JSON document per owner+session, written
    via temp-file + atomic rename so a crash mid-write can never leave a
    torn/partial plan on disk). All filesystem work runs off the event
    loop via ``asyncio.to_thread``; an in-process ``asyncio.Lock`` per plan
    key serializes concurrent mutations to the same plan so two tool calls
    racing on the same session cannot silently clobber each other's step
    updates (this is in addition to, not instead of, the optimistic
    ``expected_version`` check every mutation performs).
    """

    def __init__(self, root: Optional[str] = None):
        self._root = Path(root) if root else Path(DATA_DIR) / "agent_plans"
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def make_key(owner_id: Optional[str], session_id: Optional[str]) -> str:
        return (
            _safe_key_part(owner_id, "local")
            + "__"
            + _safe_key_part(session_id, "current")
        )

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _path_for(self, key: str) -> Path:
        return self._root / f"{key}.json"

    def _read_sync(self, path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_sync(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)  # atomic on POSIX

    async def _load(self, key: str) -> Optional[Plan]:
        path = self._path_for(key)
        data = await asyncio.to_thread(self._read_sync, path)
        return Plan.from_dict(data) if data is not None else None

    async def _save(self, key: str, plan: Plan) -> None:
        path = self._path_for(key)
        await asyncio.to_thread(self._write_sync, path, plan.to_dict())

    async def read(
        self, *, owner_id: Optional[str], session_id: Optional[str]
    ) -> Optional[Plan]:
        return await self._load(self.make_key(owner_id, session_id))

    def _check_version(
        self, plan: Plan, expected_version: Optional[int]
    ) -> None:
        if expected_version is not None and plan.version != expected_version:
            raise PlanVersionConflict(expected_version, plan)

    async def _mutate(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        expected_version: Optional[int],
        create_if_missing: bool,
        mutator,
    ) -> Plan:
        key = self.make_key(owner_id, session_id)
        async with self._lock_for(key):
            plan = await self._load(key)
            if plan is None:
                if not create_if_missing:
                    raise PlanNotFound(
                        f"No plan for owner={owner_id!r} session={session_id!r}"
                    )
                plan = Plan(
                    id=_new_id("plan"),
                    owner_id=owner_id,
                    session_id=session_id or "current",
                )
            else:
                self._check_version(plan, expected_version)

            plan = mutator(plan)
            plan.version += 1
            plan.updated_at = _now()
            await self._save(key, plan)
            return plan

    # ── Whole-plan actions ──────────────────────────────────────────

    async def create(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        title: str = "",
        summary: str = "",
        steps: Sequence[Mapping[str, Any]] = (),
    ) -> Plan:
        def _mutator(_existing: Plan) -> Plan:
            return Plan(
                id=_existing.id,
                owner_id=owner_id,
                session_id=session_id or "current",
                title=title,
                summary=summary,
                version=_existing.version,
                steps=tuple(_step_from_input(s) for s in steps),
                created_at=_existing.created_at,
            )

        key = self.make_key(owner_id, session_id)
        async with self._lock_for(key):
            existing = await self._load(key)
            base = existing or Plan(
                id=_new_id("plan"), owner_id=owner_id, session_id=session_id or "current"
            )
            plan = _mutator(base)
            plan.version = base.version + 1
            plan.updated_at = _now()
            await self._save(key, plan)
            return plan

    async def replace(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        steps: Sequence[Mapping[str, Any]],
        title: Optional[str] = None,
        summary: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> Plan:
        def _mutator(plan: Plan) -> Plan:
            plan.steps = tuple(_step_from_input(s) for s in steps)
            if title is not None:
                plan.title = title
            if summary is not None:
                plan.summary = summary
            return plan

        return await self._mutate(
            owner_id=owner_id,
            session_id=session_id,
            expected_version=expected_version,
            create_if_missing=True,
            mutator=_mutator,
        )

    async def clear(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        expected_version: Optional[int] = None,
    ) -> Plan:
        def _mutator(plan: Plan) -> Plan:
            plan.steps = ()
            return plan

        return await self._mutate(
            owner_id=owner_id,
            session_id=session_id,
            expected_version=expected_version,
            create_if_missing=True,
            mutator=_mutator,
        )

    async def archive(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        expected_version: Optional[int] = None,
    ) -> Plan:
        def _mutator(plan: Plan) -> Plan:
            plan.archived_at = _now()
            return plan

        return await self._mutate(
            owner_id=owner_id,
            session_id=session_id,
            expected_version=expected_version,
            create_if_missing=False,
            mutator=_mutator,
        )

    # ── Step-level partial actions ──────────────────────────────────

    async def add_step(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        content: str,
        priority: str = "medium",
        acceptance_criteria: str = "",
        depends_on: Sequence[str] = (),
        expected_version: Optional[int] = None,
    ) -> Plan:
        def _mutator(plan: Plan) -> Plan:
            step = PlanStep(
                id=_new_id("step"),
                content=content,
                priority=PlanStepPriority(priority),
                acceptance_criteria=acceptance_criteria,
                depends_on=tuple(depends_on),
            )
            plan.steps = plan.steps + (step,)
            return plan

        return await self._mutate(
            owner_id=owner_id,
            session_id=session_id,
            expected_version=expected_version,
            create_if_missing=True,
            mutator=_mutator,
        )

    def _update_step_mutator(self, step_id: str, **fields):
        def _mutator(plan: Plan) -> Plan:
            target = plan.step(step_id)
            if target is None:
                raise PlanStepNotFound(step_id)
            updated = _dc_replace(target, **fields, updated_at=_now())
            plan.steps = tuple(
                updated if s.id == step_id else s for s in plan.steps
            )
            return plan

        return _mutator

    async def update_step(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        step_id: str,
        content: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        acceptance_criteria: Optional[str] = None,
        notes: Optional[str] = None,
        depends_on: Optional[Sequence[str]] = None,
        expected_version: Optional[int] = None,
    ) -> Plan:
        fields: dict[str, Any] = {}
        if content is not None:
            fields["content"] = content
        if status is not None:
            fields["status"] = PlanStepStatus(status)
        if priority is not None:
            fields["priority"] = PlanStepPriority(priority)
        if acceptance_criteria is not None:
            fields["acceptance_criteria"] = acceptance_criteria
        if notes is not None:
            fields["notes"] = notes
        if depends_on is not None:
            fields["depends_on"] = tuple(depends_on)

        return await self._mutate(
            owner_id=owner_id,
            session_id=session_id,
            expected_version=expected_version,
            create_if_missing=False,
            mutator=self._update_step_mutator(step_id, **fields),
        )

    async def _set_status(
        self,
        *,
        owner_id,
        session_id,
        step_id,
        status: PlanStepStatus,
        expected_version,
        evidence: Optional[Mapping[str, Any]] = None,
        notes: Optional[str] = None,
    ) -> Plan:
        extra: dict[str, Any] = {"status": status}
        if status == PlanStepStatus.COMPLETED:
            extra["completed_at"] = _now()
        if evidence is not None:
            extra["evidence"] = dict(evidence)
        if notes is not None:
            extra["notes"] = notes
        return await self._mutate(
            owner_id=owner_id,
            session_id=session_id,
            expected_version=expected_version,
            create_if_missing=False,
            mutator=self._update_step_mutator(step_id, **extra),
        )

    async def complete_step(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        step_id: str,
        evidence: Optional[Mapping[str, Any]] = None,
        expected_version: Optional[int] = None,
    ) -> Plan:
        # Completion is always an explicit action with its own verb — never
        # inferred from a tool's exit code elsewhere in the runtime.
        return await self._set_status(
            owner_id=owner_id,
            session_id=session_id,
            step_id=step_id,
            status=PlanStepStatus.COMPLETED,
            evidence=evidence,
            expected_version=expected_version,
        )

    async def block_step(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        step_id: str,
        notes: str = "",
        expected_version: Optional[int] = None,
    ) -> Plan:
        return await self._set_status(
            owner_id=owner_id,
            session_id=session_id,
            step_id=step_id,
            status=PlanStepStatus.BLOCKED,
            notes=notes or None,
            expected_version=expected_version,
        )

    async def skip_step(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        step_id: str,
        notes: str = "",
        expected_version: Optional[int] = None,
    ) -> Plan:
        return await self._set_status(
            owner_id=owner_id,
            session_id=session_id,
            step_id=step_id,
            status=PlanStepStatus.SKIPPED,
            notes=notes or None,
            expected_version=expected_version,
        )

    async def remove_step(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        step_id: str,
        expected_version: Optional[int] = None,
    ) -> Plan:
        def _mutator(plan: Plan) -> Plan:
            if plan.step(step_id) is None:
                raise PlanStepNotFound(step_id)
            plan.steps = tuple(s for s in plan.steps if s.id != step_id)
            return plan

        return await self._mutate(
            owner_id=owner_id,
            session_id=session_id,
            expected_version=expected_version,
            create_if_missing=False,
            mutator=_mutator,
        )

    async def reorder(
        self,
        *,
        owner_id: Optional[str],
        session_id: Optional[str],
        step_ids: Sequence[str],
        expected_version: Optional[int] = None,
    ) -> Plan:
        def _mutator(plan: Plan) -> Plan:
            by_id = {s.id: s for s in plan.steps}
            missing = [sid for sid in step_ids if sid not in by_id]
            if missing:
                raise PlanStepNotFound(missing[0])
            reordered = [by_id[sid] for sid in step_ids]
            # Any step not named in step_ids keeps its relative order,
            # appended after the explicitly reordered ones — a partial
            # reorder call must never silently drop steps.
            remaining = [s for s in plan.steps if s.id not in set(step_ids)]
            plan.steps = tuple(reordered + remaining)
            return plan

        return await self._mutate(
            owner_id=owner_id,
            session_id=session_id,
            expected_version=expected_version,
            create_if_missing=False,
            mutator=_mutator,
        )


def _step_from_input(data: Mapping[str, Any]) -> PlanStep:
    """Build a PlanStep from loosely-typed input (tool args / legacy adapters)."""

    status = data.get("status", "pending")
    priority = data.get("priority", "medium")
    return PlanStep(
        id=str(data.get("id") or _new_id("step")),
        content=str(data.get("content") or data.get("text") or "").strip(),
        status=PlanStepStatus(status) if status else PlanStepStatus.PENDING,
        priority=PlanStepPriority(priority) if priority else PlanStepPriority.MEDIUM,
        depends_on=tuple(data.get("depends_on") or ()),
        acceptance_criteria=str(data.get("acceptance_criteria", "")),
        evidence=dict(data.get("evidence") or {}),
        notes=str(data.get("notes", "")),
    )


# Process-wide singleton — mirrors how other session-scoped stores in this
# codebase (e.g. background job tracking) are accessed: one instance per
# process, keyed internally by owner+session, not a per-request object.
PLAN_SERVICE = PlanService()


_CHECKLIST_LINE_RE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+)$")


def markdown_checklist_to_steps(markdown: str) -> list[dict]:
    """Deterministically parse a GitHub-style checklist into step inputs.

    One step per ``- [ ]``/``- [x]`` line, in document order. Lines that
    don't match the checklist shape are ignored (legacy ``update_plan``
    callers sometimes include a heading or blank lines) rather than
    rejected outright, matching the tool's previous lenient behavior.
    """

    steps: list[dict] = []
    for line in (markdown or "").splitlines():
        match = _CHECKLIST_LINE_RE.match(line)
        if not match:
            continue
        mark, content = match.groups()
        steps.append(
            {
                "content": content.strip(),
                "status": (
                    PlanStepStatus.COMPLETED.value
                    if mark.lower() == "x"
                    else PlanStepStatus.PENDING.value
                ),
            }
        )
    return steps


def steps_to_markdown_checklist(steps: Sequence[PlanStep]) -> str:
    """Regenerate a checklist from canonical steps (inverse of the parser above)."""

    lines = []
    for step in steps:
        mark = "x" if step.status == PlanStepStatus.COMPLETED else " "
        lines.append(f"- [{mark}] {step.content}")
    return "\n".join(lines)
