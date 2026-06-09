"""Output file handling."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path

SAVE_ATTEMPTS = 10


def save_output(output_dir: Path, slug: str, content: str) -> Path:
    """Write content to a new, collision-resistant file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for _ in range(SAVE_ATTEMPTS):
        token = secrets.token_hex(6)
        path = output_dir / f"odytor-{slug}-{timestamp}-{token}.txt"
        try:
            with path.open("x", encoding="utf-8") as output_file:
                output_file.write(content)
        except FileExistsError:
            continue
        return path
    raise FileExistsError(
        f"Could not create a unique output file in {output_dir} "
        f"after {SAVE_ATTEMPTS} attempts."
    )
