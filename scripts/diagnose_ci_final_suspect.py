#!/usr/bin/env python3
"""Disposable diagnostic: isolate final suspect subset to tiny parts."""
from __future__ import annotations
import argparse
import sys
from ci_pytest_shard import ROOT, build_shards, discover_test_modules, run_pytest

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument('--part',type=int,required=True)
    args, pytest_args=p.parse_known_args()
    if args.part not in range(4): raise SystemExit('part must be 0..3')
    original=build_shards(discover_test_modules(),16)[8]
    child=build_shards(original.paths,4)[1]
    grandchild=build_shards(child.paths,4)[2]
    suspect=build_shards(grandchild.paths,4)[1]
    parts=build_shards(suspect.paths,4)
    selected=parts[args.part]
    assigned=sorted(x for s in parts for x in s.paths)
    if assigned != sorted(suspect.paths): raise SystemExit('subdivision is not exhaustive and unique')
    relative=[x.relative_to(ROOT).as_posix() for x in selected.paths]
    print(f'final suspect part {args.part+1}/4: {len(relative)} modules',flush=True)
    for path in relative: print(f'  {path}',flush=True)
    if not relative:
        print('empty diagnostic part; PASS',flush=True); return 0
    return run_pytest([sys.executable,'-m','pytest',*pytest_args,*relative],180)
if __name__ == '__main__': raise SystemExit(main())
