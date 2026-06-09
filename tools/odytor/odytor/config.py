"""Runtime configuration."""

from __future__ import annotations

import tempfile
from pathlib import Path


# Use the platform temp dir so --save works on Linux, macOS, and Windows.
DEFAULT_OUTPUT_DIR = Path(tempfile.gettempdir())
