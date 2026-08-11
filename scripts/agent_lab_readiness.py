#!/usr/bin/env python3
"""Run the Agent Lab correctness contract locally.

Default mode is a high-signal quick regression suite. ``--full`` mirrors the
blocking Python/JavaScript selection used by the Agent Runtime Gate so local and
CI evidence cannot drift silently.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]

COMPILE_TARGETS = (
    "src/agent",
    "src/agent_loop.py",
    "src/agent_tools",
    "src/llm_core.py",
    "src/mcp_manager.py",
    "routes/agent_runtime_routes.py",
    "routes/chat_routes.py",
    "tests/runtime_v3",
)

QUICK_PYTEST_TARGETS = (
    "tests/runtime_v3/test_effect_bridge.py",
    "tests/runtime_v3/test_executor_bridge.py",
    "tests/runtime_v3/test_request_identity.py",
    "tests/test_agent_completion_contract.py",
    "tests/test_agent_unknown_tool_recovery.py",
    "tests/test_agent_truncation_continuation.py",
    "tests/test_agent_verifier_truthfulness.py",
    "tests/test_runtime_v2_execution_integrity.py",
    "tests/test_runtime_v2_dependability.py",
)

FULL_PYTEST_FIXED = (
    "tests/runtime_v3",
    "tests/test_context_budget_manager.py",
    "tests/test_execution_authority.py",
    "tests/test_filesystem_transaction_safety.py",
    "tests/test_manage_plan_tool.py",
    "tests/test_mcp_server_activation.py",
    "tests/test_native_tool_result_threading.py",
    "tests/test_observation_ledger.py",
    "tests/test_process_sandbox.py",
    "tests/test_provider_finish_reason.py",
    "tests/test_stream_termination.py",
    "tests/test_tool_policy.py",
    "tests/test_tool_registry.py",
)

FULL_PYTEST_GLOBS = (
    "test_agent_*.py",
    "test_runtime_v2_*.py",
)

PYTEST_FLAGS = (
    "-vv",
    "--tb=short",
    "--maxfail=20",
    "--timeout=90",
    "--timeout-method=thread",
)

JS_SYNTAX_TARGETS = (
    "static/js/agentRuntimeInspector.js",
    "static/js/agentToolLifecycle.js",
    "static/js/agentToolRequest.js",
    "static/js/runtimeEvents.js",
)

JS_TEST_TARGETS = (
    "tests/agentToolLifecycle.test.mjs",
    "tests/agentToolRequest.test.mjs",
    "tests/runtimeEvents.test.mjs",
)


class ReadinessError(RuntimeError):
    pass


def _repo_path(relative: str) -> Path:
    return ROOT / relative


def _require_paths(paths: Iterable[str]) -> None:
    missing = [path for path in paths if not _repo_path(path).exists()]
    if missing:
        raise ReadinessError("missing required repository paths: " + ", ".join(missing))


def full_pytest_targets() -> tuple[str, ...]:
    """Expand the exact Python target set used by the blocking CI gate."""
    expanded: list[str] = [FULL_PYTEST_FIXED[0]]
    tests_dir = ROOT / "tests"
    for pattern in FULL_PYTEST_GLOBS:
        expanded.extend(
            str(path.relative_to(ROOT))
            for path in sorted(tests_dir.glob(pattern))
            if path.is_file()
        )
    expanded.extend(FULL_PYTEST_FIXED[1:])
    # Preserve order while removing duplicate explicit/glob matches.
    return tuple(dict.fromkeys(expanded))


def python_command(*, full: bool) -> list[str]:
    targets = full_pytest_targets() if full else QUICK_PYTEST_TARGETS
    return [sys.executable, "-m", "pytest", *PYTEST_FLAGS, *targets]


def compile_command() -> list[str]:
    return [sys.executable, "-m", "compileall", "-q", *COMPILE_TARGETS]


def javascript_commands() -> tuple[list[str], list[str]]:
    return (
        ["node", *sum((["--check", target] for target in JS_SYNTAX_TARGETS), [])],
        ["node", "--test", *JS_TEST_TARGETS],
    )


def _run(command: Sequence[str], *, dry_run: bool) -> None:
    printable = " ".join(command)
    print(f"$ {printable}", flush=True)
    if dry_run:
        return
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        raise ReadinessError(f"command failed with exit code {completed.returncode}: {printable}")


def _probe_linux_sandbox(*, dry_run: bool) -> None:
    if not sys.platform.startswith("linux"):
        return
    missing = [name for name in ("bwrap", "tmux") if shutil.which(name) is None]
    if missing:
        raise ReadinessError(
            "missing Linux agent runtime dependency: "
            + ", ".join(missing)
            + ". Install bubblewrap and tmux before running the Agent Lab contract."
        )
    command = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--",
        "/bin/true",
    ]
    print("[preflight] probing Bubblewrap namespace support", flush=True)
    if dry_run:
        print("$ " + " ".join(command), flush=True)
        return
    completed = subprocess.run(
        command,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or "").strip()
        raise ReadinessError(
            "Bubblewrap namespace probe failed. On Ubuntu 24.04 test hosts, the "
            "Agent Runtime Gate enables unprivileged user namespaces for the "
            "ephemeral runner. If AppArmor is blocking your local test, inspect "
            "kernel.apparmor_restrict_unprivileged_userns and "
            "kernel.unprivileged_userns_clone before changing them."
            + (f"\nprobe: {detail}" if detail else "")
        )


def _validate_environment(*, include_js: bool) -> None:
    _require_paths(COMPILE_TARGETS)
    _require_paths(QUICK_PYTEST_TARGETS)
    _require_paths(FULL_PYTEST_FIXED)
    if include_js:
        _require_paths((*JS_SYNTAX_TARGETS, *JS_TEST_TARGETS))
        if shutil.which("node") is None:
            raise ReadinessError("Node.js is required for the Agent Runtime JavaScript contract suite")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="run the complete blocking Agent Runtime Gate target set",
    )
    parser.add_argument(
        "--no-js",
        action="store_true",
        help="skip JavaScript syntax and contract tests",
    )
    parser.add_argument(
        "--skip-sandbox-probe",
        action="store_true",
        help="skip the Linux Bubblewrap capability probe (not recommended)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print commands and validate target construction without executing them",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    include_js = not args.no_js
    try:
        _validate_environment(include_js=include_js)
        if not args.skip_sandbox_probe:
            _probe_linux_sandbox(dry_run=args.dry_run)
        mode = "full" if args.full else "quick"
        print(f"[agent-lab] running {mode} readiness contract", flush=True)
        _run(compile_command(), dry_run=args.dry_run)
        _run(python_command(full=args.full), dry_run=args.dry_run)
        if include_js:
            for command in javascript_commands():
                _run(command, dry_run=args.dry_run)
    except ReadinessError as exc:
        print(f"[agent-lab] BLOCKED: {exc}", file=sys.stderr)
        return 1
    print("[agent-lab] PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
