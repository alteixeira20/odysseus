#!/usr/bin/env python3
"""Development-only source materializer for the kernel authority boundary."""

from __future__ import annotations

import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def rewrite_agent_loop() -> None:
    path = ROOT / "src" / "agent_loop.py"
    text = path.read_text(encoding="utf-8")

    original_execution_import = (
        "from src.execution_policy import ExecutionMode, normalize_execution_mode\n"
    )
    transitional_execution_import = "from src.execution_policy import ExecutionMode\n"
    if original_execution_import in text:
        text = text.replace(original_execution_import, "", 1)
    elif transitional_execution_import in text:
        text = text.replace(transitional_execution_import, "", 1)
    elif "normalize_execution_mode" in text or "ExecutionMode" in text:
        raise SystemExit("unexpected execution-policy authority residue")

    authority_import = (
        "from src.agent.runtime_v2.authority import prepare_execution_context\n"
    )
    if authority_import in text:
        if text.count(authority_import) != 1:
            raise SystemExit("unexpected Runtime V2 authority import count")
        text = text.replace(authority_import, "", 1)

    run_budgets_import = "    RunBudgets,\n"
    if run_budgets_import in text:
        if text.count(run_budgets_import) != 1:
            raise SystemExit("unexpected RunBudgets import count")
        text = text.replace(run_budgets_import, "", 1)

    start_anchor = (
        "    _settings = AgentSettingsSnapshot.capture(get_setting)\n"
        "    if execution_context is None:\n"
    )
    end_anchor = "    workspace = execution_context.execution_root.path\n"
    fail_closed = (
        "    _settings = AgentSettingsSnapshot.capture(get_setting)\n"
        "    if execution_context is None:\n"
        "        raise RuntimeError(\n"
        "            \"_legacy_stream_agent_kernel requires a prepared \"\n"
        "            \"AgentExecutionContext\"\n"
        "        )\n"
    )

    if fail_closed not in text:
        if text.count(start_anchor) != 1:
            raise SystemExit(
                "kernel compatibility preparation: expected one start anchor, "
                f"found {text.count(start_anchor)}"
            )
        start = text.index(start_anchor)
        end = text.index(end_anchor, start)
        text = text[:start] + fail_closed + text[end:]
    elif text.count(fail_closed) != 1:
        raise SystemExit("unexpected fail-closed kernel guard count")

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

    if "from src.execution_policy import" in text:
        raise SystemExit("agent_loop still imports execution-policy authority helpers")

    path.write_text(text, encoding="utf-8")
    py_compile.compile(str(path), doraise=True)


def rewrite_characterization_test(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    import_anchor = "from src import agent_loop"
    if path.name == "test_agent_runtime_contract.py":
        import_anchor = "from src import agent_loop, agent_runs, bg_jobs"
    import_line = import_anchor + "\n"
    helper_import = "from tests.helpers.agent_kernel import stream_legacy_kernel\n"

    if helper_import not in text:
        if text.count(import_line) != 1:
            raise SystemExit(
                f"{path.name}: expected exactly one import anchor, "
                f"found {text.count(import_line)}"
            )
        text = text.replace(import_line, import_line + helper_import, 1)
    elif text.count(helper_import) != 1:
        raise SystemExit(f"{path.name}: unexpected helper import count")

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
