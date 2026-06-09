"""Git root and GitHub repository detection."""

from __future__ import annotations

import re
from pathlib import Path

from odytor.errors import RepoDetectionError
from odytor.utils import require_command, run_command


REPO_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
REMOTE_PATTERNS = (
    re.compile(r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$"),
    re.compile(r"^https?://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$"),
    re.compile(r"^ssh://git@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$"),
)


def validate_repo(repo: str) -> str:
    value = repo.strip()
    components = value.split("/")
    if len(components) != 2 or not all(_valid_component(item) for item in components):
        raise RepoDetectionError(
            f"Invalid repository '{repo}'. Expected GitHub owner/name."
        )
    return value


def _valid_component(value: str) -> bool:
    return (
        value not in {"", ".", ".."}
        and REPO_COMPONENT_PATTERN.fullmatch(value) is not None
        and any(character.isalnum() for character in value)
    )


def detect_git_root() -> Path:
    require_command("git")
    result = run_command(["git", "rev-parse", "--show-toplevel"])
    if result.returncode != 0 or not result.stdout.strip():
        raise RepoDetectionError(
            "Current directory is not inside a git repository. "
            "Run from a local clone or pass --repo owner/name."
        )
    return Path(result.stdout.strip())


def parse_github_remote(remote_url: str) -> str | None:
    value = remote_url.strip()
    for pattern in REMOTE_PATTERNS:
        match = pattern.fullmatch(value)
        if match:
            return validate_repo(match.group("repo"))
    return None


def detect_repo() -> str:
    root = detect_git_root()
    repo = _detect_with_gh(root)
    if repo:
        return validate_repo(repo)

    remote = _origin_remote(root)
    parsed = parse_github_remote(remote) if remote else None
    if parsed:
        return parsed

    raise RepoDetectionError(
        f"Git repository at {root} is not linked to a recognizable GitHub origin. "
        "Pass --repo owner/name."
    )


def _detect_with_gh(root: Path) -> str | None:
    result = run_command(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "nameWithOwner",
            "--jq",
            ".nameWithOwner",
        ],
        cwd=root,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _origin_remote(root: Path) -> str | None:
    result = run_command(["git", "remote", "get-url", "origin"], cwd=root)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def resolve_repo(repo_override: str | None) -> str:
    if repo_override:
        return validate_repo(repo_override)
    return detect_repo()
