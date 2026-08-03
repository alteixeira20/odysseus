from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one anchor, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/agent_runs.py",
    '        "terminal",\n        "execution_context",\n',
    '        "terminal",\n        "terminal_callback",\n        "execution_context",\n',
)
replace_once(
    "src/agent_runs.py",
    '''        execution_context: Optional[AgentExecutionContext],\n    ) -> None:\n''',
    '''        execution_context: Optional[AgentExecutionContext],\n        terminal_callback=None,\n    ) -> None:\n''',
)
replace_once(
    "src/agent_runs.py",
    '''        self.terminal: Optional[RunTerminal] = None\n        self.execution_context = execution_context\n''',
    '''        self.terminal: Optional[RunTerminal] = None\n        self.terminal_callback = terminal_callback\n        self.execution_context = execution_context\n''',
)
replace_once(
    "src/agent_runs.py",
    '''def _commit_terminal(\n    run: _Run,\n    terminal: RunTerminal,\n    *,\n    runtime_wire: Optional[str] = None,\n) -> None:\n    run.terminal = terminal\n''',
    '''def _commit_terminal(\n    run: _Run,\n    terminal: RunTerminal,\n    *,\n    runtime_wire: Optional[str] = None,\n) -> None:\n    callback = run.terminal_callback\n    run.terminal_callback = None\n    if callback is not None:\n        try:\n            callback(terminal)\n        except Exception:\n            logger.exception("run terminal callback failed")\n            terminal = _make_terminal(\n                RunDisposition.ERROR,\n                reason="terminal_callback_failed",\n                resumable=True,\n            )\n    run.terminal = terminal\n''',
)
replace_once(
    "src/agent_runs.py",
    '''    prepared_turn: Optional[PreparedTurn] = None,\n    commit_callback=None,\n) -> _Run:\n''',
    '''    prepared_turn: Optional[PreparedTurn] = None,\n    commit_callback=None,\n    terminal_callback=None,\n) -> _Run:\n''',
)
replace_once(
    "src/agent_runs.py",
    "        run = _Run(mode, owner, execution_context)\n",
    "        run = _Run(mode, owner, execution_context, terminal_callback)\n",
)
replace_once(
    "src/agent_runs.py",
    '''    execution_context: Optional[AgentExecutionContext] = None,\n    commit_callback=None,\n) -> Optional[_Run]:\n''',
    '''    execution_context: Optional[AgentExecutionContext] = None,\n    commit_callback=None,\n    terminal_callback=None,\n) -> Optional[_Run]:\n''',
)
replace_once(
    "src/agent_runs.py",
    '''            execution_context=execution_context,\n            commit_callback=commit_callback,\n        )\n\n\ndef begin_turn''',
    '''            execution_context=execution_context,\n            commit_callback=commit_callback,\n            terminal_callback=terminal_callback,\n        )\n\n\ndef begin_turn''',
)

print("Runtime V3 background serialization patch applied")
