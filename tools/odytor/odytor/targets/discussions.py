"""GitHub discussion fetching with comment and reply pagination."""

from __future__ import annotations

from typing import Any

from odytor.errors import FetchError
from odytor.gh_client import GitHubClient
from odytor.models import DiscussionData
from odytor.progress import Progress, disabled


DISCUSSION_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    discussion(number: $number) {
      number
      title
      url
      closed
      locked
      createdAt
      updatedAt
      upvoteCount
      category { name slug }
      author { login }
      body
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          url
          createdAt
          updatedAt
          upvoteCount
          isAnswer
          author { login }
          body
          replies(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              url
              createdAt
              updatedAt
              upvoteCount
              author { login }
              body
            }
          }
        }
      }
    }
  }
}
"""


REPLIES_QUERY = """
query($commentId: ID!, $cursor: String) {
  node(id: $commentId) {
    ... on DiscussionComment {
      replies(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          url
          createdAt
          updatedAt
          upvoteCount
          author { login }
          body
        }
      }
    }
  }
}
"""


def fetch_discussion(
    client: GitHubClient, number: int, progress: Progress | None = None
) -> DiscussionData:
    progress = progress or disabled()
    owner, name = client.repo.split("/", 1)
    cursor: str | None = None
    comments: list[dict[str, Any]] = []
    discussion: dict[str, Any] | None = None

    progress.step(f"Fetching discussion #{number} metadata...")
    page_number = 0
    while True:
        page_number += 1
        progress.page("Fetching discussion comments", page_number)
        payload = client.graphql(
            DISCUSSION_QUERY,
            {
                "owner": owner,
                "repo": name,
                "number": number,
                "cursor": cursor,
            },
        )
        discussion = _discussion_from(payload, client.repo, number)
        page = discussion.get("comments") or {}
        comments.extend(page.get("nodes") or [])
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    total = len(comments)
    for index, comment in enumerate(comments, start=1):
        comment["replies"] = _fetch_replies(client, comment, progress, index, total)

    metadata = dict(discussion or {})
    metadata.pop("comments", None)
    return DiscussionData(metadata=metadata, comments=comments)


def _discussion_from(
    payload: dict[str, Any], repo: str, number: int
) -> dict[str, Any]:
    data = payload.get("data") or {}
    repository = data.get("repository")
    discussion = repository.get("discussion") if isinstance(repository, dict) else None
    if not isinstance(discussion, dict):
        raise FetchError(f"Could not find discussion #{number} in {repo}.")
    return discussion


def _fetch_replies(
    client: GitHubClient,
    comment: dict[str, Any],
    progress: Progress,
    index: int,
    total: int,
) -> list[dict[str, Any]]:
    reply_data = comment.get("replies") or {}
    replies = list(reply_data.get("nodes") or [])
    page_info = reply_data.get("pageInfo") or {}
    cursor = page_info.get("endCursor")

    if page_info.get("hasNextPage"):
        progress.item(index, total, "Fetching replies for comment")

    while page_info.get("hasNextPage"):
        payload = client.graphql(
            REPLIES_QUERY,
            {"commentId": comment.get("id"), "cursor": cursor},
        )
        data = payload.get("data") or {}
        node = data.get("node") or {}
        reply_data = node.get("replies") or {}
        replies.extend(reply_data.get("nodes") or [])
        page_info = reply_data.get("pageInfo") or {}
        cursor = page_info.get("endCursor")

    return replies
