#!/usr/bin/env python3
"""Development-only source materializer for the kernel authority boundary."""

from __future__ import annotations

import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def rewrite_agent_loop() -> None:
    path = ROOT / "src" / "agent_loop.py"
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "from src.execution_policy import ExecutionMode, normalize_execution_mode\n",
        "from src.execution_policy import ExecutionMode\n",
        label="execution policy import",
    )
    text = replace_once(
        text,
        "from src.agent.runtime_v2.authority import prepare_execution_context\n",
        "",
        label="runtime v2 authority import",
    )
    text = replace_once(
        text,
        "    RunBudgets,\n",
        "",
        label="RunBudgets import",
    )

    start_anchor = (
        "    _settings = AgentSettingsSnapshot.capture(get_setting)\n"
        "    if execution_context is None:\n"
    )
    end_anchor = "    workspace = execution_context.execution_root.path\n"
    if text.count(start_anchor) != 1:
        raise SystemExit(
            "kernel compatibility preparation: expected one start anchor, "
            f"found {text.count(start_anchor)}"
        )
    start = text.index(start_anchor)
    end = text.index(end_anchor, start)
    replacement = (
        "    _settings = AgentSettingsSnapshot.capture(get_setting)\n"
        "    if execution_context is None:\n"
        "        raise RuntimeError(\n"
        "            \"_legacy_stream_agent_kernel requires a prepared \"\n"
        "            \"AgentExecutionContext\"\n"
        "        )\n"
    )
    text = text[:start] + replacement + text[end:]

    forbidden = (
        "prepare_execution_context(",
        "normalize_execution_mode(",
        "RunBudgets(",
        "_compatibility_disabled",
        "_legacy_budgets",
        "_legacy_mode",
    )
    for token in forbidden:
        if token in text:
            raise SystemExit(f"agent_loop still contains removed authority token: {token}")

    path.write_text(text, encoding="utf-8")
    py_compile.compile(str(path), doraise=True)


def rewrite_characterization_test(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    import_anchor = "from src import agent_loop"
    if path.name == "test_agent_runtime_contract.py":
        import_anchor = "from src import agent_loop, agent_runs, bg_jobs"
    import_line = import_anchor + "\n"
    replacement_import = (
        import_line
        + "from tests.helpers.agent_kernel import stream_legacy_kernel\n"
    )
    text = replace_once(
        text,
        import_line,
        replacement_import,
        label=f"{path.name} helper import",
    )
    count = text.count("agent_loop._legacy_stream_agent_kernel(")
    if count < 1:
        raise SystemExit(f"{path.name}: no internal kernel calls found")
    text = text.replace(
        "agent_loop._legacy_stream_agent_kernel(",
        "stream_legacy_kernel(",
    )
    if "agent_loop._legacy_stream_agent_kernel(" in text:
        raise SystemExit(f"{path.name}: direct internal kernel call remains")
    path.write_text(text, encoding="utf-8")
    py_compile.compile(str(path), doraise=True)


def main() -> None:
    rewrite_agent_loop()
    rewrite_characterization_test(ROOT / "tests" / "test_agent_replay_golden.py")
    rewrite_characterization_test(ROOT / "tests" / "test_agent_runtime_contract.py")
    py_compile.compile(
        str(ROOT / "tests" / "helpers" / "agent_kernel.py"),
        doraise=True,
    )


if __name__ == "__main__":
    main()
