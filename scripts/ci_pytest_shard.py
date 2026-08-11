#!/usr/bin/env python3
"""Run one deterministic file shard of the full pytest suite.

The repository's test standard requires tests to be order-independent. CI uses
this helper to distribute discovered Python test modules across fresh runners
without changing which modules pytest discovers.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "tests"


@dataclass
class Shard:
    paths: list[Path]
    weight: int = 0


def _is_test_module(path: Path) -> bool:
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def discover_test_modules() -> list[Path]:
    return sorted(
        path
        for path in TEST_ROOT.rglob("*.py")
        if path.is_file() and _is_test_module(path)
    )


def build_shards(paths: list[Path], count: int) -> list[Shard]:
    if count < 1:
        raise ValueError("shard count must be at least 1")

    shards = [Shard(paths=[]) for _ in range(count)]
    # Approximate runtime balance without historical timing state. Largest
    # modules are assigned first to the currently lightest shard; path is a
    # deterministic tie-breaker so every runner computes the same partition.
    weighted = sorted(
        ((path.stat().st_size, path) for path in paths),
        key=lambda item: (-item[0], item[1].as_posix()),
    )
    for size, path in weighted:
        target_index = min(
            range(count),
            key=lambda index: (shards[index].weight, index),
        )
        shards[target_index].paths.append(path)
        shards[target_index].weight += size

    for shard in shards:
        shard.paths.sort()
    return shards


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--wall-timeout-seconds", type=int, default=0)
    parser.add_argument("--list", action="store_true")
    return parser.parse_known_args()


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_pytest(command: list[str], wall_timeout_seconds: int) -> int:
    process = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
    try:
        if wall_timeout_seconds > 0:
            return process.wait(timeout=wall_timeout_seconds)
        return process.wait()
    except subprocess.TimeoutExpired:
        print(
            f"pytest shard exceeded {wall_timeout_seconds}s; "
            "terminating its entire process group",
            flush=True,
        )
        _terminate_process_group(process)
        return 124
    except BaseException:
        _terminate_process_group(process)
        raise


def main() -> int:
    args, pytest_args = parse_args()
    if args.index < 0 or args.index >= args.count:
        raise SystemExit(
            f"shard index {args.index} is outside [0, {args.count - 1}]"
        )
    if args.wall_timeout_seconds < 0:
        raise SystemExit("wall timeout must be zero or positive")

    paths = discover_test_modules()
    if not paths:
        raise SystemExit("no pytest test modules were discovered")

    shards = build_shards(paths, args.count)
    selected = shards[args.index]
    relative_paths = [path.relative_to(ROOT).as_posix() for path in selected.paths]

    assigned = sorted(path for shard in shards for path in shard.paths)
    if assigned != sorted(paths):
        raise SystemExit("pytest shard partition is not exhaustive and unique")

    print(
        f"pytest shard {args.index + 1}/{args.count}: "
        f"{len(relative_paths)} modules, approximate weight {selected.weight} bytes",
        flush=True,
    )
    for path in relative_paths:
        print(f"  {path}", flush=True)

    if args.list:
        return 0

    command = [
        sys.executable,
        "-m",
        "pytest",
        *pytest_args,
        *relative_paths,
    ]
    return run_pytest(command, args.wall_timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
