"""Workspace File Completion API (CTR-0127, PRP-0088, UDR-0066 D6).

    GET /api/workspace/files?prefix=<partial>  -- bounded, workspace-relative
        file/directory listing for the SPA's ``@file`` completion.

Every returned path resolves INSIDE CODING_WORKSPACE_DIR (CTR-0031 realpath jail).
The endpoint is inert when CODING_ENABLED is false (CTR-0032). Auth-gated by
CTR-0083 (``verify_api_key``); loopback bypass keeps localhost-first dev zero-config.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException

from app.auth import verify_api_key
from app.coding.security import resolve_safe_path
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspace", tags=["Workspace"])

# Bound the response so a large directory never floods completion.
_MAX_ENTRIES = 100


def _split_prefix(prefix: str) -> tuple[str, str]:
    """Split a workspace-relative partial into (dir_part, leaf).

    A trailing slash means "list this directory" (empty leaf). Backslashes are
    normalized to forward slashes so Windows-style input still works.
    """
    norm = prefix.replace("\\", "/")
    if norm.endswith("/"):
        return norm.rstrip("/"), ""
    pure = PurePosixPath(norm)
    parent = str(pure.parent)
    if parent == ".":
        parent = ""
    return parent, pure.name


@router.get("/files", dependencies=[Depends(verify_api_key)])
async def list_workspace_files(prefix: str = "") -> dict:
    """Return workspace-relative file/dir entries matching ``prefix`` for completion."""
    base = (settings.coding_workspace_dir or "").strip()
    if not settings.coding_enabled or not base or not Path(base).is_dir():
        # Inert when coding is disabled / no workspace configured (UDR-0066 D6).
        return {"entries": [], "disabled": True}

    dir_part, leaf = _split_prefix(prefix)
    try:
        safe_dir = resolve_safe_path(base, dir_part)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    if not Path(safe_dir).is_dir():
        return {"entries": [], "truncated": False}

    leaf_lower = leaf.lower()
    rows: list[dict] = []
    try:
        with os.scandir(safe_dir) as it:
            for entry in it:
                if leaf and not entry.name.lower().startswith(leaf_lower):
                    continue
                rel = str(PurePosixPath(dir_part) / entry.name) if dir_part else entry.name
                is_dir = entry.is_dir()
                row: dict = {"path": rel, "is_dir": is_dir}
                if not is_dir:
                    with contextlib.suppress(OSError):
                        row["size"] = entry.stat().st_size
                rows.append(row)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="failed to list workspace") from exc

    # Directories first, then files; each alphabetical (case-insensitive).
    rows.sort(key=lambda r: (not r["is_dir"], r["path"].lower()))
    truncated = len(rows) > _MAX_ENTRIES
    return {"entries": rows[:_MAX_ENTRIES], "truncated": truncated}


__all__ = ["router"]
