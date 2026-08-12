#!/usr/bin/env python3
"""Disposable diagnostic: split original 16-way shard 8 into four children."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ci_pytest_shard import ROOT, build_shards, discover_test_modules, run_pytest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=int, required=True)
    args, pytest_args = parser.parse_known_args()
    if args.child not in range(4):
        raise SystemExit("child must be 0..3")

    original = build_shards(discover_test_modules(), 16)[8]
    children = build_shards(original.paths, 4)
    selected = children[args.child]
    relative = [p.relative_to(ROOT).as_posix() for p in selected.paths]

    assigned = sorted(p for shard in children for p in shard.paths)
    if assigned != sorted(original.paths):
        raise SystemExit("diagnostic subdivision is not exhaustive and unique")

    print(
        f"original shard 9/16 -> diagnostic child {args.child + 1}/4: "
        f"{len(relative)} modules, approximate weight {selected.weight} bytes",
        flush=True,
    )
    for path in relative:
        print(f"  {path}", flush=True)

    command = [sys.executable, "-m", "pytest", *pytest_args, *relative]
    return run_pytest(command, 420)


if __name__ == "__main__":
    raise SystemExit(main())
