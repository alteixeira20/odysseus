"""Small data models shared across modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class Target:
    kind: str
    number: int


@dataclass(frozen=True)
class IssueData:
    metadata: dict[str, Any]
    comments: list[dict[str, Any]]


@dataclass(frozen=True)
class PullRequestData:
    issue: dict[str, Any]
    issue_comments: list[dict[str, Any]]
    pull_request: dict[str, Any]
    reviews: list[dict[str, Any]]
    review_comments: list[dict[str, Any]]
    files: list[dict[str, Any]]
    checks: str


@dataclass(frozen=True)
class DiscussionData:
    metadata: dict[str, Any]
    comments: list[dict[str, Any]]


@dataclass(frozen=True)
class CommentWindow:
    """How many comments to show and in which order.

    limit None means "all". order is "oldest" (chronological, default) or
    "latest" (newest first). The defaults reproduce the unfiltered output.
    """
    limit: int | None = None
    order: str = "oldest"

    @property
    def customized(self) -> bool:
        return self.limit is not None or self.order != "oldest"
