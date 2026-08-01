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

SANDBOX_SHELL_AUTHORITY = """\
## Process authority: sandboxed workspace shell
The user granted the safe workspace shell for this run. `bash`, `python`, and background commands run in an isolated Linux namespace with no network, a read-only host root, a minimal secret-free environment, and resource limits. The selected workspace is the writable execution root; when none is selected, the Odysseus server working tree is used. Network, credential helpers, Docker, host services, keyrings, SSH agents, and writes outside that execution root are intentionally unavailable. If the task requires them, explain that the user must explicitly grant Full host shell on a new run; do not pretend a denied operation succeeded."""


HOST_SHELL_AUTHORITY = """\
## Process authority: full host shell
The user explicitly granted Full host shell for this run. `bash`, `python`, and background commands execute with the normal host environment, network, credential helpers, sockets, and filesystem visibility available to the Odysseus process. This is powerful authority, not a sandbox. Stay within the user's request and active workspace when one is bound; otherwise begin by inspecting the current directory. Prefer read-only inspection, never print credentials, and obtain specific authorization before destructive filesystem/git actions, privilege escalation, package installation, service changes, pushes, or other consequential external writes."""


SHELL_GUIDANCE = """\
## Shell rules
Confirm the working directory with `pwd` before acting if you are unsure where you are. Prefer `read_file`/`grep`/`glob`/`ls`/`edit_file`/`apply_patch` for ordinary source work when those tools are available; use `bash` when they don't fit (builds, tests, git, one-off pipelines). Once you decide to act, emit the tool call immediately — never end a turn with "Now I'll check..." and no tool call. Keep any prose before a tool call to zero or one short sentence. Use bounded, non-interactive commands; never request or print passwords/secrets. Never run destructive git or filesystem operations (`reset --hard`, `clean -f`, `rm -rf`, force-push, `checkout --`/`restore` over uncommitted work) without the user's explicit authorization for that specific action.

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


def execution_authority_guidance(mode: object) -> str | None:
    normalized = getattr(mode, "value", mode)
    if normalized == "sandboxed":
        return SANDBOX_SHELL_AUTHORITY
    if normalized == "host":
        return HOST_SHELL_AUTHORITY
    return None
