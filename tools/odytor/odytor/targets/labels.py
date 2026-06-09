"""Read-only repository label listing."""

from __future__ import annotations

from typing import Any

from odytor.gh_client import GitHubClient


def fetch_labels(client: GitHubClient) -> list[dict[str, Any]]:
    return client.api_paginated(
        f"repos/{client.repo}/labels?per_page=100",
        f"labels for {client.repo}",
    )


def format_labels(repo: str, labels: list[dict[str, Any]]) -> str:
    lines = [
        "=" * 100,
        f"LABELS - {repo}",
        "=" * 100,
        f"total labels: {len(labels)}",
        "",
    ]
    if not labels:
        lines.append("No labels found.")
        return "\n".join(lines) + "\n"

    width = max(len(str(label.get("name", ""))) for label in labels)
    for label in sorted(labels, key=lambda item: str(item.get("name", "")).lower()):
        name = str(label.get("name", ""))
        color = str(label.get("color", "") or "")
        description = str(label.get("description", "") or "")
        line = f"  {name:<{width}}  #{color}"
        if description:
            line += f"  {description}"
        lines.append(line)
    return "\n".join(lines) + "\n"
