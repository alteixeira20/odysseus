"""odytor test suite (standard-library unittest only).

Ensures the odytor package is importable no matter how the suite is launched,
by putting the package root (the parent of this tests/ directory) on sys.path.
"""

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
