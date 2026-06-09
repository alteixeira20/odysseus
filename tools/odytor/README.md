# odytor

`odytor` is a proposed repo-local reviewer helper for Odysseus. It reads GitHub
pull requests, issues, discussions, and labels through the authenticated GitHub
CLI and renders practical plain-text review context. It is a convenience tool,
not project policy or an automated review verdict.

This first slice is intentionally read-only with respect to GitHub and git. It
does not modify tracked repository files, branches, worktrees, commits, or
GitHub state, and it does not run suggested validation commands. `--save`
intentionally creates a report file in the selected output directory.

## Quick Start

Run it directly from the Odysseus checkout; installation is not required:

```bash
./tools/odytor/bin/odytor --version
./tools/odytor/bin/odytor --help
./tools/odytor/bin/odytor --print --pr 3128
./tools/odytor/bin/odytor --review --pr 3128
./tools/odytor/bin/odytor --labels
```

Pass `--repo owner/name` when running outside a GitHub clone or when you want to
override repository detection:

```bash
./tools/odytor/bin/odytor --print --issue 2523 \
  --repo pewdiepie-archdaemon/odysseus
```

## Requirements

- Python 3.10 or newer
- `git`, for repository auto-detection
- GitHub CLI (`gh`), authenticated with `gh auth login`
- a POSIX shell for the wrapper and helper scripts

The implementation uses only the Python standard library and adds no project
dependencies. It is designed to work on Linux and macOS when these requirements
are available; current validation has been performed locally on Linux.

## Output

Report content goes to stdout. Progress and status messages go to stderr, and
`--quiet` suppresses those messages. `--save` writes the report to a timestamped
file in the system temporary directory unless `--output-dir` is supplied. If
the selected output directory is inside a checkout, the report file is created
there without changing tracked files or git state.

`--comments` and `--order` apply only to `--print`.

## Tests

The isolated offline suite under `tools/odytor/tests/` resolves the repo-local
tool relative to its script:

```bash
tools/odytor/scripts/run-tests
```

Manual live smoke checks are read-only and require authenticated `gh` access:

```bash
tools/odytor/scripts/smoke-test
```

See [GUIDE.md](GUIDE.md) for the command reference, output behavior,
troubleshooting, limitations, and work not included in this first slice.
