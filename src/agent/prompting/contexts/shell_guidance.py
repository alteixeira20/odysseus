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
The user granted the safe workspace shell for this run. `run_sandbox_command` and `run_python` execute in an isolated Linux namespace with no network, a read-only host root, a minimal secret-free environment, common workspace secrets masked, output redaction, and resource limits. The immutable ExecutionRoot is read-only unless runtime diagnostics separately say `process_workspace_write_granted: true`; ordinary `workspace_write_granted` authorizes only transactional `patch_workspace`, never process writes. Process workspace write is non-transactional direct access: partial changes may survive failure, timeout, or cancellation, and the runtime does not claim rollback. Selecting a repository never grants mutation. Without a selection the root is an explicitly configured default or an ephemeral workspace, never the server process directory or Odysseus checkout. Network, credential helpers, Docker, host services, keyrings, SSH agents, and writes outside an independently granted root are unavailable. There is no host fallback."""


HOST_SHELL_AUTHORITY = """\
## Process authority: full host shell
The user explicitly granted Full host shell for this run. `run_host_command` and `run_python` use the exact ExecutionRoot disclosed in runtime diagnostics. Generic shell approval binds exact opaque command text, the exact root, the disclosed sanitized environment, and known dependency snapshots; dynamic shell resolution is not a complete dependency seal. Host process authority does not grant workspace mutation: unless diagnostics separately say `process_workspace_write_granted: true`, the runtime mounts that root read-only. Ordinary `workspace_write_granted` authorizes only transactional `patch_workspace`. Process workspace write is non-transactional direct access: partial changes may survive failure, timeout, or cancellation, and the runtime does not claim rollback. This is powerful host authority, not a confidentiality sandbox, but it is not automatic approval for destructive filesystem actions, Git remote writes, privilege escalation, package installation, service/container mutation, credential access, or consequential network writes; those effects require a separate approval decision. Never print credentials, and never infer a command is read-only when its effects are opaque."""


SHELL_GUIDANCE = """\
## Shell rules
The runtime has already bound and disclosed the exact execution root. Prefer `read_files`, `search_text`, `find_files`, and `patch_workspace` for ordinary source work; use the bound command tool for builds, tests, Git reads, and one-off pipelines. Once you decide to act, emit the tool call immediately. Use bounded, non-interactive commands; never request or print passwords/secrets. Sensitive effects are classified before execution and may require approval.

**find**: quote glob patterns, give an explicit root and `-type`, preview with `-print` before acting. With arbitrary filenames use `-print0` piped to `xargs -0`, or prefer `-exec ... {} +`. Never run `-delete` (or any destructive `-exec`) before first previewing the identical predicate with `-print`.
  `find src tests -type f \\( -name '*.py' -o -name '*.pyi' \\) -print`
  `find . -type f -name '*.py' -exec python -m py_compile -- {} +`

**sed**: use `sed -n '<start>,<end>p'` for bounded read-only inspection with explicit ranges. Never run `sed -i`; use `patch_workspace` for actual source changes.
  `sed -n '1,160p' -- src/agent_loop.py`

**rg/grep**: prefer `search_text` for ordinary code search; scope it with canonical paths/globs and use fixed-string mode for literal text.

**jq**: use it for structural JSON inspection (`jq '.field'`) rather than eyeballing raw output; use `--arg` to inject values instead of string-interpolating them into the filter.

**xargs**: pair with `-print0`/`-0` for null-delimited input when filenames may contain spaces or newlines; never build shell commands by interpolating untrusted strings.

**git**: inspect before you act — `git branch --show-current`, `git rev-parse HEAD`, `git status --short`, `git diff --check`, `git diff --stat`, `git diff -- <path>`. Never run `reset`, `restore`, `clean`, `rebase`, `merge`, `commit`, or `push` unless the user explicitly authorized that specific action."""


def shell_guidance_if_available(tool_names) -> str | None:
    """Return shell guidance only when a canonical process tool is exposed."""

    names = set(tool_names or ())
    process_tools = {"run_sandbox_command", "run_host_command", "run_python"}
    return SHELL_GUIDANCE if names.intersection(process_tools) else None


def execution_authority_guidance(mode: object) -> str | None:
    normalized = getattr(mode, "value", mode)
    if normalized == "sandboxed":
        return SANDBOX_SHELL_AUTHORITY
    if normalized == "host":
        return HOST_SHELL_AUTHORITY
    return None
