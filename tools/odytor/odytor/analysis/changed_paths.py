"""Classify changed file paths into review-risk categories.

Pure functions over path strings. No network, no side effects. The substring
sets are intentionally conservative; this produces *hints* for a human reviewer,
not an automatic verdict.
"""

from __future__ import annotations

import os

DOCS_EXTENSIONS = {".md", ".rst", ".txt", ".adoc", ".mdx", ".markdown"}
DOCS_DIRS = ("docs/", "doc/", "documentation/")

TEST_DIRS = ("/tests/", "/test/", "/spec/", "/specs/", "__tests__/", "fixtures/")
TEST_SUFFIXES = (".test.", "_test.", ".spec.", "_spec.")
TEST_PREFIXES = ("test_", "spec_")

# Each category maps to substrings matched (case-insensitively) against the path.
CATEGORY_SUBSTRINGS: dict[str, tuple[str, ...]] = {
    "auth": ("auth", "oauth", "jwt", "login", "permission", "privilege",
             "owner-scope", "owner_scope", "owner-scoped"),
    "security": ("secret", "password", "credential", "token", "crypt", "security", "ssrf"),
    "database": ("migration", "alembic", "schema", "sqlite", "database", "/db/", "models/", "storage"),
    "session": ("session", "cookie"),
    "provider": ("openai", "anthropic", "ollama", "bedrock", "vllm", "sglang",
                 "provider", "/llm", "endpoint"),
    "ui": ("frontend/", "templates/", "static/js/", "static/css/"),
    "ci": (".github/", "workflow", "dockerfile", "docker-compose", "compose.",
           ".gitlab-ci", "/ci/"),
    "packaging": ("requirements.txt", "requirements/", "pyproject.toml", "setup.py",
                  "setup.cfg", "pipfile", "poetry.lock", "package.json",
                  "package-lock.json", "go.mod", "cargo.toml"),
}

UI_EXTENSIONS = {".css", ".html", ".svg", ".scss"}


def is_docs(path: str) -> bool:
    lowered = path.lower()
    _, ext = os.path.splitext(lowered)
    if ext in DOCS_EXTENSIONS:
        return True
    return any(directory in lowered for directory in DOCS_DIRS)


def is_test(path: str) -> bool:
    lowered = path.lower()
    name = os.path.basename(lowered)
    if any(name.startswith(prefix) for prefix in TEST_PREFIXES):
        return True
    if any(suffix in lowered for suffix in TEST_SUFFIXES):
        return True
    return any(directory in lowered for directory in TEST_DIRS)


def path_categories(path: str) -> list[str]:
    """Return the risk categories a single path falls into (excluding docs/test)."""
    lowered = path.lower()
    _, ext = os.path.splitext(lowered)
    categories = [
        name
        for name, needles in CATEGORY_SUBSTRINGS.items()
        if any(needle in lowered for needle in needles)
    ]
    if ext in UI_EXTENSIONS and "ui" not in categories:
        categories.append("ui")
    return categories


def review_flags(paths: list[str]) -> list[str]:
    """Human-readable risk flags for a set of changed paths."""
    if not paths:
        return []

    flags: list[str] = []
    if all(is_docs(path) for path in paths):
        flags.append("docs-only")
    if all(is_test(path) for path in paths):
        flags.append("tests-only")

    present: set[str] = set()
    for path in paths:
        present.update(path_categories(path))

    labels = {
        "ci": "CI/tooling",
        "security": "security-sensitive",
        "auth": "auth paths",
        "database": "database paths",
        "session": "session paths",
        "provider": "provider/model paths",
        "ui": "UI paths",
        "packaging": "packaging/dependencies",
    }
    for category, label in labels.items():
        if category in present:
            flags.append(label)
    return flags
