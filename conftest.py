"""Make the repository sources win before pytest imports any test module."""
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
LOCAL_SOURCES = (
    ROOT / "src",
    ROOT / "packages" / "agent-protocol-core" / "src",
)

# Root conftest loads before test collection.  Per-test path setup is too late:
# an earlier module can bind installed packages in ``sys.modules`` first.
for source in reversed(LOCAL_SOURCES):
    value = str(source)
    if value not in sys.path:
        sys.path.insert(0, value)
