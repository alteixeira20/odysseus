"""Issue fetching."""

from __future__ import annotations

from odytor.errors import FetchError
from odytor.gh_client import GitHubClient
from odytor.models import IssueData
from odytor.progress import Progress, disabled


def fetch_issue(
    client: GitHubClient, number: int, progress: Progress | None = None
) -> IssueData:
    progress = progress or disabled()
    repo = client.repo

    progress.step(f"Fetching issue #{number} metadata...")
    metadata = client.api_object(
        f"repos/{repo}/issues/{number}",
        f"issue #{number} in {repo}",
    )
    if "pull_request" in metadata:
        raise FetchError(
            f"#{number} in {repo} is a pull request, not an issue. Use --pr {number}."
        )
    progress.step("Fetching comments...")
    comments = client.api_paginated(
        f"repos/{repo}/issues/{number}/comments?per_page=100",
        f"comments for issue #{number} in {repo}",
    )
    return IssueData(metadata=metadata, comments=comments)
