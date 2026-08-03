from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one anchor, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# The canonical executor keeps its established implementation, while the
# public entry point becomes a durable fail-closed wrapper.
replace_once(
    "src/agent/runtime_v2/executor.py",
    "async def execute_normalized_tool_call(\n",
    "async def _execute_normalized_tool_call_impl(\n",
)
executor = Path("src/agent/runtime_v2/executor.py")
text = executor.read_text(encoding="utf-8")
marker = "\n\nasync def execute_normalized_tool_call(\n"
if marker in text:
    raise RuntimeError("executor wrapper already exists")
text += '''\n\nasync def execute_normalized_tool_call(\n    call: NormalizedToolCall,\n    execution_context: AgentExecutionContext,\n    *,\n    progress_cb=None,\n    approval_id: Optional[str] = None,\n) -> ToolResult:\n    """Execute one canonical call behind a durable idempotency/effect lease."""\n    from src.agent.runtime_v3.executor_bridge import execute_with_durable_effects\n\n    return await execute_with_durable_effects(\n        call,\n        execution_context,\n        implementation=_execute_normalized_tool_call_impl,\n        progress_cb=progress_cb,\n        approval_id=approval_id,\n    )\n'''
executor.write_text(text, encoding="utf-8")

# Durable session ordering supports restart-safe background ownership checks.
ledger = Path("src/agent/runtime_v3/ledger.py")
text = ledger.read_text(encoding="utf-8")
anchor = '''    def get_run(self, run_id: str) -> RunRecord | None:\n        with self._lock:\n            row = self._conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()\n        return self._record(row) if row else None\n\n'''
addition = anchor + '''    def latest_run_for_session(self, session_id: str) -> RunRecord | None:\n        with self._lock:\n            row = self._conn.execute(\n                "SELECT * FROM agent_runs WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",\n                (str(session_id),),\n            ).fetchone()\n        return self._record(row) if row else None\n\n    def session_run_relation(self, session_id: str, run_id: str) -> str:\n        latest = self.latest_run_for_session(session_id)\n        if latest is None:\n            return "unknown"\n        return "current" if latest.run_id == str(run_id) else "superseded"\n\n'''
if text.count(anchor) != 1:
    raise RuntimeError("ledger get_run anchor changed")
ledger.write_text(text.replace(anchor, addition, 1), encoding="utf-8")

# Background continuations must not replace a foreground run after a racy
# check. The decision and insertion happen under the same run-manager lock.
runs = Path("src/agent_runs.py")
text = runs.read_text(encoding="utf-8")
anchor = "\n\ndef begin_turn(*, session_id: str, owner: Optional[str]) -> TurnLease:\n"
addition = '''\n\ndef start_if_idle(\n    session_id: str,\n    agen: AsyncGenerator[str, None],\n    *,\n    mode: RunMode = RunMode.BACKGROUND,\n    owner: Optional[str] = None,\n    execution_context: Optional[AgentExecutionContext] = None,\n    commit_callback=None,\n) -> Optional[_Run]:\n    """Atomically start background work only while the session is idle.\n\n    Unlike a separate ``is_active`` check followed by ``start``, this cannot\n    replace a foreground run that began between those operations.\n    """\n    with _RUNS_LOCK:\n        current = _RUNS.get(str(session_id))\n        if current is not None and current.status == "running":\n            return None\n        return start(\n            str(session_id),\n            agen,\n            mode=mode,\n            owner=owner,\n            execution_context=execution_context,\n            commit_callback=commit_callback,\n        )\n\n\ndef begin_turn(*, session_id: str, owner: Optional[str]) -> TurnLease:\n'''
if text.count(anchor) != 1:
    raise RuntimeError("agent_runs begin_turn anchor changed")
text = text.replace(anchor, addition, 1)
replace_all_anchor = '    "start",\n    "stop",\n'
if text.count(replace_all_anchor) != 1:
    raise RuntimeError("agent_runs __all__ anchor changed")
text = text.replace(replace_all_anchor, '    "start",\n    "start_if_idle",\n    "stop",\n', 1)
runs.write_text(text, encoding="utf-8")

print("Runtime V3 effects/recovery integration applied")
