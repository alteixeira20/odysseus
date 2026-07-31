"""Concise shell-literacy and action-commitment prompt fragment.

Root cause context: the "files" domain rule block in
``src/agent/routing/tool_domains.py`` covers file-tool usage in general
(``read_file`` vs ``create_document``, prefer ``grep``/``glob``/``ls`` over
shell) but says nothing about how to use the host shell *well* once a task
actually needs it — safe ``find``/``sed`` usage, non-interactive commands,
or the "announce an action, never call the tool" failure mode the intent-
without-action supervisor (``src/agent/supervision/intent_nudge.py``)
exists to catch after the fact. This module is a single, bounded fragment
that is appended to the prompt only when ``bash`` is actually in this
turn's effective tool set — see ``domain_rules_for_tools`` in
``src/agent/routing/tool_domains.py``, the same conditional-inclusion
mechanism every other domain rule block already uses, so it is on by
default only when Bash is on, and never advertises a tool that isn't
available this turn.
"""

from __future__ import annotations

SHELL_GUIDANCE = """\
## Shell rules
`bash` runs as a full host shell under the service user — it is NOT a sandbox. Confirm workspace identity (`get_workspace`) before acting if you are unsure where you are. Prefer `read_file`/`grep`/`glob`/`ls`/`edit_file`/`apply_patch` for ordinary source work; use `bash` when those don't fit (builds, tests, git, process/service inspection, one-off pipelines). Once you decide to act, emit the tool call immediately — never end a turn with "Now I'll check..." and no tool call. Keep any prose before a tool call to zero or one short sentence. Use bounded, non-interactive commands; never request or print passwords/secrets. Never run destructive git or filesystem operations (`reset --hard`, `clean -f`, `rm -rf`, force-push, `checkout --`/`restore` over uncommitted work) without the user's explicit authorization for that specific action.

**find**: quote glob patterns, give an explicit root and `-type`, preview with `-print` before acting. With arbitrary filenames use `-print0` piped to `xargs -0`, or prefer `-exec ... {} +`. Never run `-delete` (or any destructive `-exec`) before first previewing the identical predicate with `-print`.
  `find src tests -type f \\( -name '*.py' -o -name '*.pyi' \\) -print`
  `find . -type f -name '*.py' -exec python -m py_compile -- {} +`

**sed**: use `sed -n '<start>,<end>p'` for bounded read-only inspection with explicit ranges. Never run a blind tree-wide `sed -i`. Prefer `edit_file`/`apply_patch` for actual source changes; if you must use `sed -i`, preview the same range/pattern with `-n` first. GNU and BSD `sed -i` flag syntax differ — check `sed --version` if unsure.
  `sed -n '1,160p' -- src/agent_loop.py`

**rg/grep**: prefer the structured `grep` tool for ordinary code search — scope it with paths/globs, use fixed-string mode for literal text, and pass `--` before any pattern derived from user/model input.

**jq**: use it for structural JSON inspection (`jq '.field'`) rather than eyeballing raw output; use `--arg` to inject values instead of string-interpolating them into the filter.

**xargs**: pair with `-print0`/`-0` for null-delimited input when filenames may contain spaces or newlines; never build shell commands by interpolating untrusted strings.

**git**: inspect before you act — `git branch --show-current`, `git rev-parse HEAD`, `git status --short`, `git diff --check`, `git diff --stat`, `git diff -- <path>`. Never run `reset`, `restore`, `clean`, `rebase`, `merge`, `commit`, or `push` unless the user explicitly authorized that specific action."""


def shell_guidance_if_available(tool_names) -> str | None:
    """Return the shell-literacy fragment only when `bash` is in scope this turn."""

    names = set(tool_names or ())
    return SHELL_GUIDANCE if "bash" in names else None
