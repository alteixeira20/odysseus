"""Action handlers. Each returns (content, save_slug) so the CLI can print
and optionally save uniformly. All handlers are read-only.

Granular progress (per fetch/page/item) lives in the target fetchers; the
single-step actions emit their one status line here.
"""

from __future__ import annotations

from odytor.analysis import review_summary
from odytor.errors import OdytorError
from odytor.formatters import format_discussion, format_issue, format_pull_request
from odytor.gh_client import GitHubClient
from odytor.git_repo import resolve_repo
from odytor.models import CommentWindow, Target
from odytor.progress import Progress, disabled
from odytor.targets.discussions import fetch_discussion
from odytor.targets.issues import fetch_issue
from odytor.targets.labels import fetch_labels, format_labels
from odytor.targets.prs import fetch_pull_request
from odytor.utils import require_command, require_gh_auth


def make_client(repo_override: str | None, progress: Progress | None = None) -> GitHubClient:
    progress = progress or disabled()
    repo = resolve_repo(repo_override) if repo_override is not None else None
    require_command("gh")
    require_gh_auth()
    if repo is None:
        progress.step("Detecting repository...")
        repo = resolve_repo(None)
    progress.step(f"Repo: {repo}")
    return GitHubClient(repo)


def run_print(
    client: GitHubClient, target: Target, window: CommentWindow, progress: Progress
) -> tuple[str, str]:
    return render_target(client, target, window, progress), f"{target.kind}-{target.number}"


def run_review(
    client: GitHubClient, target: Target, progress: Progress
) -> tuple[str, str]:
    number = target.number
    if target.kind == "pr":
        progress.step(f"Fetching PR #{number} review data...")
        view = client.pr_view(number, review_summary.PR_REVIEW_FIELDS)
        content = review_summary.format_pr_review(number, view)
    elif target.kind == "issue":
        content = review_summary.format_issue_review(number, fetch_issue(client, number, progress))
    else:
        content = review_summary.format_discussion_review(
            number, fetch_discussion(client, number, progress)
        )
    return content, f"review-{target.kind}-{number}"


def run_labels(client: GitHubClient, progress: Progress) -> tuple[str, str]:
    progress.step("Fetching labels...")
    return format_labels(client.repo, fetch_labels(client)), "labels"


def render_target(
    client: GitHubClient, target: Target, window: CommentWindow, progress: Progress
) -> str:
    number = target.number
    if target.kind == "pr":
        return format_pull_request(number, fetch_pull_request(client, number, progress), window)
    if target.kind == "issue":
        return format_issue(number, fetch_issue(client, number, progress), window)
    if target.kind == "discussion":
        return format_discussion(number, fetch_discussion(client, number, progress), window)
    raise OdytorError(f"Unsupported target type: {target.kind}")
