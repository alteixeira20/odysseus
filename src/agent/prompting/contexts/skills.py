"""Untrusted skill-index context for agent prompt assembly."""

from collections.abc import Iterable, Sequence
from typing import Any, Optional


def format_skill_index(skills: Sequence[dict[str, Any]]) -> str:
    """Format the legacy one-line-per-skill catalogue byte-for-byte."""

    if not skills:
        return ""
    lines = [
        "## Available skills",
        "Procedures the assistant should consult before doing domain work. "
        "Fetch the full procedure with `manage_skills` action=view name=<name> "
        "when one looks relevant. Entries tagged `(draft)` were written by the "
        "teacher-escalation loop after a prior failure — treat them as authoritative "
        "guidance; if you follow one and it works, that's a good signal the procedure "
        "is correct.",
    ]
    by_category: dict[str, list[dict[str, Any]]] = {}
    for skill in skills:
        by_category.setdefault(skill["category"], []).append(skill)
    for category in sorted(by_category):
        lines.append(f"\n**{category}**")
        for skill in by_category[category]:
            badge = " *(draft)*" if skill.get("status") == "draft" else ""
            lines.append(
                f"- `{skill['name']}` — {skill['description']}{badge}"
            )
    return "\n\n" + "\n".join(lines)


def skill_index_context(
    *,
    owner: Optional[str],
    active_tools: Iterable[str],
) -> str:
    """Load and format skills available to this owner and tool selection."""

    from services.memory.skills import SkillsManager
    from src.constants import DATA_DIR

    manager = SkillsManager(DATA_DIR)
    skills = manager.index_for(
        owner=owner,
        active_toolsets=list(active_tools),
    )
    return format_skill_index(skills)


__all__ = ["format_skill_index", "skill_index_context"]
