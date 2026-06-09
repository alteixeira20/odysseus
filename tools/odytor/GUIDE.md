# odytor Guide

`odytor` is a proposed repo-local, read-only reviewer helper for Odysseus.
Version: 0.1.0. It is a convenience tool, not project policy.

## Table of Contents

- [Safety](#safety)
- [Requirements](#requirements)
- [Running odytor](#running-odytor)
- [Repository detection](#repository-detection)
- [Command reference](#command-reference)
- [Comment windows](#comment-windows)
- [Saving output](#saving-output)
- [Progress and quiet mode](#progress-and-quiet-mode)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Not included in this first slice](#not-included-in-this-first-slice)

## Safety

This first PR only reads GitHub data through authenticated `gh`. It does not:

- write GitHub state;
- modify tracked repository files, branches, worktrees, or commits;
- run suggested validation commands.

`--labels` only lists labels. `--review` produces preparation context for a
human reviewer, not an automated verdict. `--save` is the only intentional
local write: it creates a report file in the selected output directory.

## Requirements

- Python 3.10 or newer
- `git`, for repository detection
- GitHub CLI (`gh`), authenticated with `gh auth login`
- a POSIX shell for the wrapper and helper scripts

No Python package installation or additional dependency is required. The tool
uses the Python standard library only.

The wrapper and scripts use portable POSIX shell syntax. The implementation is
designed to work on Linux and macOS when the requirements above are available.
Current validation has been performed locally on Linux; this is not a claim of
testing on every operating system.

## Running odytor

From the Odysseus repository:

```bash
tools/odytor/bin/odytor --help
tools/odytor/bin/odytor --version
```

The wrapper resolves the Python package relative to its own location, so it also
works from another directory:

```bash
cd "${TMPDIR:-/tmp}"
"$HOME/odysseus/tools/odytor/bin/odytor" --labels \
  --repo pewdiepie-archdaemon/odysseus
```

## Repository Detection

Repository selection follows this order:

1. Use `--repo OWNER/NAME` when supplied.
2. Ask `gh` for the repository associated with the current git checkout.
3. Fall back to parsing the checkout's GitHub `origin` URL.

Use an explicit repository when running outside a clone:

```bash
tools/odytor/bin/odytor --labels \
  --repo pewdiepie-archdaemon/odysseus
```

## Command Reference

### Print a pull request

```bash
tools/odytor/bin/odytor --print --pr 3128
```

Prints PR metadata, body, comments, reviews, review comments, changed files,
diff summary, and checks.

### Print an issue

```bash
tools/odytor/bin/odytor --print --issue 2523
```

Prints issue metadata, body, and comments.

### Print a discussion

```bash
tools/odytor/bin/odytor --print --discussion 2528
```

Prints discussion metadata, body, comments, and replies.

### Review a pull request

```bash
tools/odytor/bin/odytor --review --pr 3128
```

Prints local review context including scope, changed-path risk signals, status
checks, and suggested validation areas. It does not submit a GitHub review.

### Review an issue

```bash
tools/odytor/bin/odytor --review --issue 2523
```

Prints a concise issue review-preparation summary.

### Review a discussion

```bash
tools/odytor/bin/odytor --review --discussion 2528
```

Prints a concise discussion review-preparation summary.

### List labels

```bash
tools/odytor/bin/odytor --labels
```

Lists repository label names, colors, and descriptions. This command does not
add, remove, or edit labels.

## Comment Windows

`--comments N` limits conversation comments for `--print`. It is rejected for
`--review` and `--labels`.

`--order oldest` is the default and displays comments chronologically.
`--order latest` displays newest comments first. An explicitly supplied
`--order` is rejected outside `--print`.

```bash
tools/odytor/bin/odytor --print --pr 3128 --comments 10 --order latest
tools/odytor/bin/odytor --print --issue 2523 --comments 20 --order oldest
```

The target metadata and body remain present. For pull requests, reviews and
inline review comments are not limited by the conversation comment window.

## Saving Output

`--save` writes the same report sent to stdout to a timestamped UTF-8 text file.
The default output directory comes from Python's platform-aware system temporary
directory. Use `--output-dir` to choose another location. If that location is
inside a checkout, odytor creates the report there; it still does not alter
tracked files, branches, worktrees, or commits.

```bash
tools/odytor/bin/odytor --print --pr 3128 --save
tools/odytor/bin/odytor --review --pr 3128 \
  --save --output-dir ~/review-exports
```

## Progress and Quiet Mode

Report content goes to stdout. Progress, status, and save-location messages go
to stderr, so report output remains clean when piped:

```bash
tools/odytor/bin/odytor --print --pr 3128 > pr-3128.txt
```

Use `--quiet` or `-q` to suppress every stderr message, including the
save-location line. Combined with `--save`, stderr stays empty on success while
the report still prints to stdout and the file is still written:

```bash
tools/odytor/bin/odytor --print --discussion 2528 --quiet
tools/odytor/bin/odytor --review --pr 3128 --save --quiet
```

## Testing

Run the isolated offline unit suite from the repository:

```bash
tools/odytor/scripts/run-tests
```

Tests live under `tools/odytor/tests/`. The script resolves the tool relative to
itself, so it also works from another directory:

```bash
cd "${TMPDIR:-/tmp}"
"$HOME/odysseus/tools/odytor/scripts/run-tests"
```

The tests use standard-library `unittest`, fixtures, and mocked subprocess
boundaries. They do not contact GitHub.

Manual live smoke checks are separate from the offline suite and remain
read-only:

```bash
tools/odytor/scripts/smoke-test
```

They require network access and an authenticated `gh` session. They print and
save reports but do not write GitHub state.

## Troubleshooting

### `gh` is missing

Install GitHub CLI from <https://cli.github.com/>. `odytor` reports a clear
missing-command error and does not guess a distribution-specific command.

### `gh` is not authenticated

```bash
gh auth login
gh auth status
```

`odytor` checks `gh auth status` before fetching data. If authentication is
missing, it exits with a message that directs you to `gh auth login`.

### Repository detection fails

Run inside a GitHub clone or pass the repository explicitly:

```bash
tools/odytor/bin/odytor --print --pr 3128 --repo owner/name
```

### A discussion request fails

Discussion data uses GitHub's GraphQL API. Confirm the discussion exists and
that the authenticated account can access it, then retry transient API errors.

## Limitations

- Live commands require network access and an authenticated `gh` session.
- Python 3.10 or newer is required.
- Repository auto-detection recognizes GitHub HTTPS and SSH origin URLs.
- GitHub permissions determine which data is visible.
- One target can be printed or reviewed per invocation.
- `--comments` and `--order` apply only to `--print`.
- `--comments` limits conversation comments, not PR reviews or inline comments.
- Review summaries are heuristics for human review preparation, not verdicts.
- Suggested validation commands are displayed only; odytor does not run them.

## Not Included in This First Slice

This first slice does not include:

- follow-up detection;
- stale PR analysis;
- milestone summaries;
- local audit worktrees;
- approval or merge workflows;
- GitHub label, issue, pull request, comment, or review mutation;
- CI, root Makefile, or root dependency integration.
