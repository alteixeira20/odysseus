"""Plain-text formatters for fetched GitHub data."""

from __future__ import annotations

import json
from typing import Any

from odytor.models import CommentWindow, DiscussionData, IssueData, PullRequestData


RULE = "=" * 100
SUBRULE = "-" * 100


def section(title: str) -> str:
    return f"{RULE}\n{title}\n{RULE}\n"


def subsection(title: str) -> str:
    return f"\n{SUBRULE}\n{title}\n{SUBRULE}\n"


def render_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def apply_comment_window(
    comments: list[dict[str, Any]], window: CommentWindow
) -> tuple[list[dict[str, Any]], int]:
    """Order and limit comments. Returns (shown, total).

    API responses are chronological (oldest first). 'latest' reverses so the
    newest come first; a limit then keeps the first N of the chosen order.
    """
    total = len(comments)
    ordered = list(reversed(comments)) if window.order == "latest" else list(comments)
    if window.limit is not None:
        ordered = ordered[: window.limit]
    return ordered, total


def comment_window_header(shown: int, total: int, window: CommentWindow) -> str:
    if not window.customized:
        return ""
    return f"Comments displayed: {shown} of {total}\nOrder: {window.order}\n\n"


def _windowed_comments(
    comments: list[dict[str, Any]], label: str, window: CommentWindow
) -> str:
    shown, total = apply_comment_window(comments, window)
    return comment_window_header(len(shown), total, window) + render_comments(shown, label)


def format_issue(
    number: int, data: IssueData, window: CommentWindow = CommentWindow()
) -> str:
    chunks = [
        section(f"ISSUE #{number} - METADATA"),
        render_json(compact_issue(data.metadata)),
        "\n\n",
        section(f"ISSUE #{number} - BODY"),
        data.metadata.get("body") or "",
        "\n\n",
        section(f"ISSUE #{number} - COMMENTS"),
        _windowed_comments(data.comments, "ISSUE COMMENT", window),
    ]
    return "".join(chunks).rstrip() + "\n"


def format_pull_request(
    number: int, data: PullRequestData, window: CommentWindow = CommentWindow()
) -> str:
    chunks = [
        section(f"PR #{number} - CONVERSATION METADATA"),
        render_json(compact_issue(data.issue)),
        "\n\n",
        section(f"PR #{number} - BODY"),
        data.issue.get("body") or "",
        "\n\n",
        section(f"PR #{number} - CONVERSATION COMMENTS"),
        _windowed_comments(data.issue_comments, "CONVERSATION COMMENT", window),
        "\n",
        section(f"PR #{number} - PULL REQUEST METADATA"),
        render_json(compact_pr(data.pull_request)),
        "\n\n",
        section(f"PR #{number} - REVIEWS"),
        render_reviews(data.reviews),
        "\n",
        section(f"PR #{number} - REVIEW COMMENTS"),
        render_review_comments(data.review_comments),
        "\n",
        section(f"PR #{number} - CHANGED FILE METADATA"),
        render_file_metadata(data.files),
        "\n",
        section(f"PR #{number} - CHANGED FILES"),
        render_changed_files(data.files),
        "\n",
        section(f"PR #{number} - DIFF SUMMARY"),
        render_diff_summary(data.files),
        "\n",
        section(f"PR #{number} - CHECKS"),
        data.checks,
        "\n",
    ]
    return "".join(chunks).rstrip() + "\n"


def format_discussion(
    number: int, data: DiscussionData, window: CommentWindow = CommentWindow()
) -> str:
    metadata = data.metadata
    shown, total = apply_comment_window(data.comments, window)
    chunks = [
        section(f"DISCUSSION #{number} - METADATA"),
        render_json(compact_discussion(metadata)),
        "\n\n",
        section(f"DISCUSSION #{number} - BODY"),
        metadata.get("body") or "",
        "\n\n",
        section(f"DISCUSSION #{number} - COMMENTS AND REPLIES"),
        comment_window_header(len(shown), total, window),
    ]
    if not shown:
        chunks.append("No discussion comments.\n")

    for index, comment in enumerate(shown, start=1):
        chunks.extend(_render_discussion_comment(index, comment))
    return "".join(chunks).rstrip() + "\n"


def compact_issue(item: dict[str, Any]) -> dict[str, Any]:
    milestone = item.get("milestone")
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "type": "PR" if item.get("pull_request") else "Issue",
        "state": item.get("state"),
        "author": user_login(item.get("user")),
        "labels": _names(item.get("labels")),
        "assignees": [user_login(value) for value in item.get("assignees", [])],
        "milestone": milestone.get("title") if isinstance(milestone, dict) else None,
        "comments": item.get("comments"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "closed_at": item.get("closed_at"),
        "url": item.get("html_url"),
    }


def compact_pr(item: dict[str, Any]) -> dict[str, Any]:
    base = item.get("base") or {}
    head = item.get("head") or {}
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "draft": item.get("draft"),
        "author": user_login(item.get("user")),
        "base": base.get("ref"),
        "head": head.get("ref"),
        "mergeable": item.get("mergeable"),
        "mergeable_state": item.get("mergeable_state"),
        "commits": item.get("commits"),
        "additions": item.get("additions"),
        "deletions": item.get("deletions"),
        "changed_files": item.get("changed_files"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "closed_at": item.get("closed_at"),
        "merged_at": item.get("merged_at"),
        "url": item.get("html_url"),
    }


def compact_discussion(item: dict[str, Any]) -> dict[str, Any]:
    category = item.get("category") or {}
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "category": category.get("name"),
        "category_slug": category.get("slug"),
        "author": graphql_login(item),
        "upvotes": item.get("upvoteCount"),
        "closed": item.get("closed"),
        "locked": item.get("locked"),
        "created_at": item.get("createdAt"),
        "updated_at": item.get("updatedAt"),
        "url": item.get("url"),
    }


def render_comments(comments: list[dict[str, Any]], label: str) -> str:
    if not comments:
        return "No comments.\n"

    chunks: list[str] = []
    for index, comment in enumerate(comments, start=1):
        chunks.append(subsection(f"{label} {index} by @{user_login(comment.get('user'))}"))
        chunks.append(_rest_comment_details(comment))
        chunks.append(comment.get("body") or "")
        chunks.append("\n")
    return "".join(chunks)


def render_reviews(reviews: list[dict[str, Any]]) -> str:
    if not reviews:
        return "No PR reviews.\n"

    chunks: list[str] = []
    for index, review in enumerate(reviews, start=1):
        title = f"PR REVIEW {index} by @{user_login(review.get('user'))}"
        chunks.append(subsection(title))
        chunks.append(f"state: {review.get('state')}\n")
        chunks.append(f"submitted: {review.get('submitted_at')}\n")
        chunks.append(f"url: {review.get('html_url')}\n\n")
        chunks.append(review.get("body") or "")
        chunks.append("\n")
    return "".join(chunks)


def render_review_comments(comments: list[dict[str, Any]]) -> str:
    if not comments:
        return "No PR review comments.\n"

    chunks: list[str] = []
    for index, comment in enumerate(comments, start=1):
        title = f"PR REVIEW COMMENT {index} by @{user_login(comment.get('user'))}"
        chunks.append(subsection(title))
        chunks.append(f"path: {comment.get('path')}\n")
        chunks.append(f"line: {comment.get('line') or comment.get('original_line')}\n")
        chunks.append(f"side: {comment.get('side')}\n")
        chunks.append(_rest_comment_details(comment))
        chunks.append(comment.get("body") or "")
        chunks.append("\n")
    return "".join(chunks)


def render_file_metadata(files: list[dict[str, Any]]) -> str:
    rows = [_compact_file(item) for item in files]
    return render_json(rows) + "\n"


def render_changed_files(files: list[dict[str, Any]]) -> str:
    if not files:
        return "No changed files.\n"
    return "\n".join(str(item.get("filename")) for item in files) + "\n"


def render_diff_summary(files: list[dict[str, Any]]) -> str:
    additions = sum(_integer(item.get("additions")) for item in files)
    deletions = sum(_integer(item.get("deletions")) for item in files)
    chunks = [
        f"Files changed: {len(files)}\n",
        f"Additions: {additions}\n",
        f"Deletions: {deletions}\n",
    ]
    for item in files:
        chunks.append(f"\n{item.get('filename')}\n")
        chunks.append(f"  status: {item.get('status')}\n")
        chunks.append(f"  additions: {_integer(item.get('additions'))}\n")
        chunks.append(f"  deletions: {_integer(item.get('deletions'))}\n")
        chunks.append(f"  changes: {_integer(item.get('changes'))}\n")
    return "".join(chunks)


def user_login(user: Any) -> str:
    if not isinstance(user, dict):
        return "unknown"
    return user.get("login") or "unknown"


def graphql_login(item: dict[str, Any]) -> str:
    author = item.get("author")
    if not isinstance(author, dict):
        return "unknown"
    return author.get("login") or "unknown"


def _render_discussion_comment(
    index: int, comment: dict[str, Any]
) -> list[str]:
    chunks = [
        subsection(f"COMMENT {index} by @{graphql_login(comment)}"),
        _graphql_comment_details(comment, include_answer=True),
        comment.get("body") or "",
        "\n",
    ]
    for reply_index, reply in enumerate(comment.get("replies") or [], start=1):
        title = f"REPLY {reply_index} to comment {index} by @{graphql_login(reply)}"
        chunks.append(subsection(title))
        chunks.append(_graphql_comment_details(reply, include_answer=False))
        chunks.append(reply.get("body") or "")
        chunks.append("\n")
    return chunks


def _rest_comment_details(comment: dict[str, Any]) -> str:
    return (
        f"created: {comment.get('created_at')}\n"
        f"updated: {comment.get('updated_at')}\n"
        f"url: {comment.get('html_url')}\n\n"
    )


def _graphql_comment_details(
    comment: dict[str, Any], include_answer: bool
) -> str:
    details = (
        f"url: {comment.get('url')}\n"
        f"created: {comment.get('createdAt')}\n"
        f"updated: {comment.get('updatedAt')}\n"
        f"upvotes: {comment.get('upvoteCount')}\n"
    )
    if include_answer:
        details += f"is_answer: {comment.get('isAnswer')}\n"
    return details + "\n"


def _compact_file(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": item.get("filename"),
        "status": item.get("status"),
        "additions": item.get("additions"),
        "deletions": item.get("deletions"),
        "changes": item.get("changes"),
    }


def _names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [
        value.get("name")
        for value in values
        if isinstance(value, dict) and value.get("name")
    ]


def _integer(value: Any) -> int:
    return value if isinstance(value, int) else 0
