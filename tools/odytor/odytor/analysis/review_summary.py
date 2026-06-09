"""Build a local review-prep summary for a PR, issue, or discussion.

This is a reviewer assistant, not an automatic verdict. It never posts,
labels, approves, or runs anything; it only prints what a human reviewer
should look at and which local checks would be worth running.
"""

from __future__ import annotations

import shlex
from typing import Any

from odytor.analysis.changed_paths import is_test, review_flags
from odytor.formatters import (
    compact_discussion,
    compact_issue,
    render_json,
    section,
    subsection,
)
from odytor.models import DiscussionData, IssueData

# gh pr view --json fields needed for a PR review summary.
PR_REVIEW_FIELDS = [
    "number", "title", "url", "state", "author", "baseRefName", "headRefName",
    "isDraft", "mergeable", "mergeStateStatus", "reviewDecision", "labels",
    "files", "additions", "deletions", "changedFiles", "commits",
    "statusCheckRollup", "body", "comments", "reviews", "createdAt", "updatedAt",
]

FAILED_CHECKS = {"ACTION_REQUIRED", "CANCELLED", "FAILURE", "STARTUP_FAILURE", "TIMED_OUT"}
PENDING_CHECKS = {"EXPECTED", "IN_PROGRESS", "PENDING", "QUEUED", "REQUESTED", "WAITING"}

BODY_PREVIEW_LIMIT = 60
COMMAND_PATH_CAP = 12


def format_pr_review(number: int, view: dict[str, Any]) -> str:
    paths = [str(item.get("path") or "") for item in view.get("files") or []]
    chunks = [
        section(f"REVIEW PREP - PR #{number}"),
        _pr_overview(view),
        _risk_block(paths, _label_names(view)),
        _checks_block(view),
        _files_block(view, paths),
        _validation_block(paths),
        _conversation_block(view),
        _footer(),
    ]
    return "".join(chunks).rstrip() + "\n"


def format_issue_review(number: int, data: IssueData) -> str:
    meta = data.metadata
    chunks = [
        section(f"REVIEW PREP - ISSUE #{number}"),
        render_json(compact_issue(meta)),
        "\n\n",
        _risk_block([], _names(meta.get("labels"))),
        f"comments: {len(data.comments)}\n",
        _body_preview(meta.get("body")),
        _footer(),
    ]
    return "".join(chunks).rstrip() + "\n"


def format_discussion_review(number: int, data: DiscussionData) -> str:
    meta = data.metadata
    reply_count = sum(len(c.get("replies") or []) for c in data.comments)
    chunks = [
        section(f"REVIEW PREP - DISCUSSION #{number}"),
        render_json(compact_discussion(meta)),
        "\n\n",
        f"comments: {len(data.comments)}  replies: {reply_count}\n",
        _body_preview(meta.get("body")),
        _footer(),
    ]
    return "".join(chunks).rstrip() + "\n"


def _pr_overview(view: dict[str, Any]) -> str:
    return (
        f"title: {view.get('title')}\n"
        f"author: @{_login(view.get('author'))}\n"
        f"url: {view.get('url')}\n"
        f"state: {view.get('state')}  draft: {view.get('isDraft')}\n"
        f"base: {view.get('baseRefName')}  head: {view.get('headRefName')}\n"
        f"mergeable: {view.get('mergeable')}  "
        f"mergeStateStatus: {view.get('mergeStateStatus')}  "
        f"reviewDecision: {view.get('reviewDecision') or 'NONE'}\n"
        f"labels: {', '.join(_label_names(view)) or 'none'}\n"
        f"scope: {view.get('changedFiles')} files, "
        f"+{view.get('additions')}/-{view.get('deletions')}, "
        f"{len(view.get('commits') or [])} commits\n"
        f"created: {view.get('createdAt')}  updated: {view.get('updatedAt')}\n"
    )


def _risk_block(paths: list[str], labels: list[str]) -> str:
    flags = review_flags(paths)
    label_flags = _label_risk_flags(labels)
    combined = flags + [flag for flag in label_flags if flag not in flags]
    body = "\n".join(f"- {flag}" for flag in combined) if combined else "- none detected"
    return subsection("REVIEW-RISK FLAGS") + body + "\n"


def _checks_block(view: dict[str, Any]) -> str:
    failed, pending, total = _summarize_checks(view.get("statusCheckRollup"))
    lines = [f"total checks: {total}"]
    if failed:
        lines.append(f"FAILING: {', '.join(failed)}")
    if pending:
        lines.append(f"pending: {', '.join(pending)}")
    if total and not failed and not pending:
        lines.append("all reported checks passed")
    if not total:
        lines.append("no checks reported")
    return subsection("CHECKS") + "\n".join(lines) + "\n"


def _files_block(view: dict[str, Any], paths: list[str]) -> str:
    files = view.get("files") or []
    if not files:
        return subsection("CHANGED FILES") + "No changed files reported.\n"
    lines = [
        f"  +{item.get('additions', 0):<5}-{item.get('deletions', 0):<5} {item.get('path')}"
        for item in files
    ]
    return subsection(f"CHANGED FILES ({len(files)})") + "\n".join(lines) + "\n"


def _validation_block(paths: list[str]) -> str:
    suggestions = suggested_validations(paths)
    body = "\n".join(f"- {item}" for item in suggestions)
    note = "(suggestions only - odytor does not run these)\n"
    return subsection("SUGGESTED LOCAL VALIDATION") + note + body + "\n"


def _conversation_block(view: dict[str, Any]) -> str:
    comments = view.get("comments") or []
    reviews = view.get("reviews") or []
    lines = [f"conversation comments: {len(comments)}", f"reviews: {len(reviews)}"]
    for review in reviews:
        lines.append(
            f"- review by @{_login(review.get('author'))}: {review.get('state')}"
        )
    return subsection("EXISTING COMMENTS AND REVIEWS") + "\n".join(lines) + "\n"


def suggested_validations(paths: list[str]) -> list[str]:
    if not paths:
        return ["Inspect the diff manually; no changed files were reported."]

    py_files = [p for p in paths if p.endswith(".py")]
    js_files = [p for p in paths if p.endswith((".js", ".mjs", ".cjs"))]
    test_files = [p for p in paths if is_test(p)]
    non_test_code = [p for p in py_files if not is_test(p)]

    suggestions: list[str] = []
    if py_files:
        suggestions.append(
            f"Python syntax check:\n  python3 -m py_compile -- {_quote_paths(py_files)}"
        )
        _append_omission_note(suggestions, py_files, "Python")
    if non_test_code:
        suggestions.append(
            "Run the focused test suite for the touched modules "
            "(e.g. python3 -m pytest tests/ -q)."
        )
    if test_files:
        suggestions.append(
            f"Focused Python tests:\n  python3 -m pytest -- {_quote_paths(test_files)}"
        )
        _append_omission_note(suggestions, test_files, "Python test")
    for js in js_files[:COMMAND_PATH_CAP]:
        suggestions.append(
            f"JavaScript syntax check:\n  node --check -- {shlex.quote(js)}"
        )
    _append_omission_note(suggestions, js_files, "JavaScript")
    if not py_files and not js_files:
        suggestions.append("No Python/JS changes; review content and formatting directly.")
    return suggestions


def _summarize_checks(rollup: Any) -> tuple[list[str], list[str], int]:
    if not isinstance(rollup, list):
        return [], [], 0
    failed: list[str] = []
    pending: list[str] = []
    for item in rollup:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("context") or item.get("workflowName") or "unknown"
        conclusion = str(item.get("conclusion") or "").upper()
        status = str(item.get("status") or item.get("state") or "").upper()
        if conclusion in FAILED_CHECKS:
            failed.append(name)
        elif status in PENDING_CHECKS:
            pending.append(name)
    return failed, pending, len(rollup)


def _label_risk_flags(labels: list[str]) -> list[str]:
    lowered = {label.lower() for label in labels}
    flags: list[str] = []
    for label in labels:
        if label.lower() in {"stale pr", "stale"}:
            flags.append(f"review-state label: {label}")
    if lowered & {"needs work", "needs-work"}:
        flags.append("labeled needs-work")
    if lowered & {"blocked", "duplicate"}:
        flags.append("labeled blocked/duplicate")
    if lowered & {"security", "high-risk", "breaking"}:
        flags.append("labeled security/high-risk")
    return flags


def _body_preview(body: Any) -> str:
    text = (body or "").strip()
    if not text:
        return subsection("BODY PREVIEW") + "[empty]\n"
    lines = text.splitlines()
    preview = "\n".join(lines[:BODY_PREVIEW_LIMIT])
    if len(lines) > BODY_PREVIEW_LIMIT:
        preview += f"\n[truncated: {len(lines)} lines total]"
    return subsection("BODY PREVIEW") + preview + "\n"


def _footer() -> str:
    return subsection("NOTE") + (
        "This is review-prep context, not a verdict. odytor performs no GitHub\n"
        "mutations and runs none of the suggested commands.\n"
    )


def _quote_paths(paths: list[str]) -> str:
    return " ".join(shlex.quote(path) for path in paths[:COMMAND_PATH_CAP])


def _append_omission_note(
    suggestions: list[str], paths: list[str], label: str
) -> None:
    omitted = len(paths) - COMMAND_PATH_CAP
    if omitted > 0:
        suggestions.append(
            f"Additional {label} files omitted from the copyable command: {omitted}"
        )


def _login(user: Any) -> str:
    if isinstance(user, dict):
        return user.get("login") or "unknown"
    return "unknown"


def _label_names(view: dict[str, Any]) -> list[str]:
    return _names(view.get("labels"))


def _names(labels: Any) -> list[str]:
    if not isinstance(labels, list):
        return []
    return [label.get("name", "") for label in labels if isinstance(label, dict) and label.get("name")]
