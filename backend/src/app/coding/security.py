"""Path security for coding tools (CTR-0031, PRP-0019).

Provides path traversal prevention by resolving paths and validating
they remain within the configured workspace directory boundary.
"""

import os
from pathlib import Path


def resolve_safe_path(base_dir: str, relative_path: str) -> str:
    """Resolve a relative path and validate it is within base_dir.

    Uses os.path.realpath() to resolve symlinks before boundary check.
    Raises ValueError if the resolved path escapes the base directory.
    """
    base = os.path.realpath(base_dir)
    target = os.path.realpath(str(Path(base) / relative_path))
    if target != base and not target.startswith(base + os.sep):
        msg = f"Access denied: path outside workspace directory: {relative_path}"
        raise ValueError(msg)
    return target
