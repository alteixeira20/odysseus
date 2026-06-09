"""Pull request fetching."""

from __future__ import annotations

from odytor.errors import FetchError
from odytor.gh_client import GitHubClient
from odytor.models import PullRequestData
from odytor.progress import Progress, disabled


def fetch_pull_request(
    client: GitHubClient, number: int, progress: Progress | None = None
) -> PullRequestData:
    progress = progress or disabled()
    repo = client.repo

    progress.step(f"Fetching PR #{number} metadata...")
    issue = client.api_object(
        f"repos/{repo}/issues/{number}",
        f"issue metadata for PR #{number} in {repo}",
    )
    if "pull_request" not in issue:
        raise FetchError(f"#{number} in {repo} is an issue, not a pull request.")

    pull_request = client.api_object(
        f"repos/{repo}/pulls/{number}",
        f"pull request #{number} in {repo}",
    )
    progress.step("Fetching comments...")
    issue_comments = client.api_paginated(
        f"repos/{repo}/issues/{number}/comments?per_page=100",
        f"issue comments for PR #{number} in {repo}",
    )
    progress.step("Fetching reviews...")
    reviews = client.api_paginated(
        f"repos/{repo}/pulls/{number}/reviews?per_page=100",
        f"reviews for PR #{number} in {repo}",
    )
    review_comments = client.api_paginated(
        f"repos/{repo}/pulls/{number}/comments?per_page=100",
        f"review comments for PR #{number} in {repo}",
    )
    progress.step("Fetching changed files...")
    files = client.api_paginated(
        f"repos/{repo}/pulls/{number}/files?per_page=100",
        f"changed files for PR #{number} in {repo}",
    )
    progress.step("Fetching checks...")
    checks = client.pr_checks(number)

    return PullRequestData(
        issue=issue,
        issue_comments=issue_comments,
        pull_request=pull_request,
        reviews=reviews,
        review_comments=review_comments,
        files=files,
        checks=checks,
    )
