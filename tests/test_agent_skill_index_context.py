from src.agent.prompting.contexts.skills import format_skill_index


def test_skill_index_groups_categories_and_marks_drafts():
    rendered = format_skill_index(
        [
            {
                "category": "writing",
                "name": "brief",
                "description": "Write a concise brief",
                "status": "published",
            },
            {
                "category": "coding",
                "name": "repair",
                "description": "Repair a regression",
                "status": "draft",
            },
        ]
    )

    assert rendered.startswith("\n\n## Available skills")
    assert rendered.index("**coding**") < rendered.index("**writing**")
    assert "- `repair` — Repair a regression *(draft)*" in rendered
    assert "- `brief` — Write a concise brief" in rendered


def test_empty_skill_index_contributes_no_context():
    assert format_skill_index([]) == ""
