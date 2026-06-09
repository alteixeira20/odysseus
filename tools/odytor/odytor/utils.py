"""General command and JSON helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from odytor.errors import CommandError, FetchError
from odytor.models import CommandResult


INSTALL_HINTS = {
    "gh": "Install the GitHub CLI: https://cli.github.com/",
    "git": "Install git: https://git-scm.com/downloads",
}


def require_command(name: str) -> None:
    """Fail early with a clear message when a required tool is missing."""
    if shutil.which(name) is None:
        hint = INSTALL_HINTS.get(name, "")
        suffix = f" {hint}" if hint else ""
        raise CommandError(f"required command '{name}' not found on PATH.{suffix}")


def require_gh_auth() -> None:
    """Fail before data fetching when GitHub CLI authentication is unavailable."""
    result = run_command(["gh", "auth", "status"])
    if result.returncode == 0:
        return
    detail = result.stderr.strip() or result.stdout.strip()
    suffix = f" Details: {detail}" if detail else ""
    raise CommandError(
        "GitHub CLI is not authenticated. Run 'gh auth login' and try again."
        f"{suffix}"
    )


def run_command(
    args: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise CommandError(f"Could not run {args[0]}: {error}") from error

    return CommandResult(
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )


def command_failure(result: CommandResult, description: str) -> CommandError:
    detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
    return CommandError(f"{description}: {detail}")


def decode_json(text: str, description: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise FetchError(f"Invalid JSON returned for {description}: {error}") from error


def decode_json_stream(text: str, description: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values: list[Any] = []
    position = 0

    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break

        try:
            value, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError as error:
            raise FetchError(
                f"Invalid paginated JSON returned for {description}: {error}"
            ) from error
        values.append(value)

    return values
