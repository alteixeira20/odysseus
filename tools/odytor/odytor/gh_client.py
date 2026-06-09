"""Read-only GitHub CLI client."""

from __future__ import annotations

from typing import Any

from odytor.errors import FetchError
from odytor.utils import (
    command_failure,
    decode_json,
    decode_json_stream,
    run_command,
)


class GitHubClient:
    def __init__(self, repo: str) -> None:
        self.repo = repo

    def api_object(self, path: str, description: str) -> dict[str, Any]:
        result = run_command(["gh", "api", path])
        if result.returncode != 0:
            raise command_failure(result, f"Could not fetch {description}")

        data = decode_json(result.stdout, description)
        if not isinstance(data, dict):
            raise FetchError(f"Expected an object when fetching {description}.")
        return data

    def api_paginated(self, path: str, description: str) -> list[dict[str, Any]]:
        result = run_command(["gh", "api", path, "--paginate"])
        if result.returncode != 0:
            raise command_failure(result, f"Could not fetch {description}")

        pages = decode_json_stream(result.stdout, description)
        return self._flatten_pages(pages, description)

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        args = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is not None:
                args.extend(["-F", f"{key}={value}"])

        result = run_command(args)
        if result.returncode != 0:
            raise command_failure(result, "GitHub GraphQL request failed")

        payload = decode_json(result.stdout, "GitHub GraphQL response")
        if not isinstance(payload, dict):
            raise FetchError("GitHub GraphQL returned an unexpected response.")
        if payload.get("errors"):
            raise FetchError(f"GitHub GraphQL error: {payload['errors']}")
        return payload

    def pr_checks(self, number: int) -> str:
        result = run_command(
            ["gh", "pr", "checks", str(number), "--repo", self.repo]
        )
        if result.returncode != 0:
            raise command_failure(result, f"Could not fetch checks for PR #{number}")
        output = result.stdout.strip()
        return output or "No checks reported."

    def pr_view(self, number: int, fields: list[str]) -> dict[str, Any]:
        result = run_command(
            ["gh", "pr", "view", str(number), "--repo", self.repo,
             "--json", ",".join(fields)]
        )
        if result.returncode != 0:
            raise command_failure(result, f"Could not view PR #{number}")

        data = decode_json(result.stdout, f"PR #{number} view")
        if not isinstance(data, dict):
            raise FetchError(f"Expected an object when viewing PR #{number}.")
        return data

    @staticmethod
    def _flatten_pages(
        pages: list[Any], description: str
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, list):
                raise FetchError(
                    f"Expected paginated arrays when fetching {description}."
                )
            for item in page:
                if not isinstance(item, dict):
                    raise FetchError(
                        f"Expected objects when fetching {description}."
                    )
                items.append(item)
        return items
