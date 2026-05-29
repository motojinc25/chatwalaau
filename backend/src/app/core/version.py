"""Shared application version helper (CTR-0094 v5, UDR-0044 D2, PRP-0068).

Single source for the running app version. Resolves the installed package
metadata first (pip / uv install), falls back to ``backend/pyproject.toml``
in a dev checkout, and finally to a ``"0.0.0"`` sentinel. The value is, by
construction, identical to ``backend/pyproject.toml`` ``[project].version``.

This consolidates the three previously duplicated copies of this logic
(``app.main``, ``app.cli.main``, ``app.cli``) per UDR-0044 D2.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
import tomllib

_FALLBACK = "0.0.0"
_PACKAGE_NAME = "chatwalaau"

# app/core/version.py -> parents[0]=core, [1]=app, [2]=src, [3]=backend
_PYPROJECT_PATH = Path(__file__).resolve().parents[3] / "pyproject.toml"


def get_app_version() -> str:
    """Return the running app version.

    metadata -> pyproject.toml -> "0.0.0". Never raises.
    """
    try:
        return importlib.metadata.version(_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        try:
            with _PYPROJECT_PATH.open("rb") as f:
                return tomllib.load(f).get("project", {}).get("version", _FALLBACK)
        except (OSError, tomllib.TOMLDecodeError):
            return _FALLBACK


__all__ = ["get_app_version"]
