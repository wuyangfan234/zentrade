"""Immutable image revision, or the source checkout revision for local runs."""

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

_REVISION = re.compile(r"[0-9a-f]{7,40}")


def _validated(value: str) -> str:
    return value if _REVISION.fullmatch(value) else "unknown"


@lru_cache(maxsize=1)
def _source_revision() -> str:
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "HEAD"],
            cwd=root, capture_output=True, text=True, check=True, timeout=2,
        ).stdout.splitlines()
        if len(result) == 2 and Path(result[0]).resolve() == root:
            return _validated(result[1])
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def revision() -> str:
    """Do not infer a version or expose arbitrary environment values."""
    value = os.environ.get("ZENTRADE_COMMIT")
    return _validated(value.strip()) if value is not None else _source_revision()
