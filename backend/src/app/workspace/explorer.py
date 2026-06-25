"""File Explorer API (CTR-0136, PRP-0091, UDR-0069).

Human-facing file browse/edit over the coding workspace -- a sibling of CTR-0127 in
``app.workspace``. Every path resolves THROUGH the CTR-0031 realpath jail
(``resolve_safe_path``) and stays inside CODING_WORKSPACE_DIR (UDR-0069 D2); a second
jail is never introduced. The whole surface is inert (404) unless FILE_EXPLORER_ENABLED
AND CODING_ENABLED AND the workspace is a configured directory (UDR-0069 D3). Mutating
endpoints (PUT/POST/DELETE) consume CTR-0083 (``verify_api_key``); loopback bypass keeps
localhost-first dev zero-config (UDR-0069 D4).

Endpoints (all under the existing ``/api/workspace`` prefix):
    GET    /api/workspace/tree?dir=<rel>     -- one directory level (lazy).
    GET    /api/workspace/file?path=<rel>    -- read a text file + metadata.
    PUT    /api/workspace/file               -- save content (optional mtime guard).
    POST   /api/workspace/entry              -- create a file or a directory.
    POST   /api/workspace/move               -- rename / move (also drag-move).
    DELETE /api/workspace/entry?path=<rel>   -- delete a file or a directory.
"""

import logging
import os
from pathlib import Path
import shutil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import verify_api_key
from app.coding.security import resolve_safe_path
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspace", tags=["File Explorer"])

# A file's leading bytes are sniffed for a NUL to classify it as binary. Binary
# files are opened READ-ONLY (no monaco binary editing, UDR-0069 D6).
_BINARY_SNIFF_BYTES = 8192


def _explorer_base() -> str:
    """Return the workspace base dir, or raise 404 when the surface is inert.

    Inert unless FILE_EXPLORER_ENABLED AND CODING_ENABLED AND CODING_WORKSPACE_DIR is a
    real directory (UDR-0069 D3). Raising 404 lets the SPA gate its launcher icon by
    probing GET /api/workspace/tree (mirrors the Cron useCronAvailable pattern).
    """
    base = (settings.coding_workspace_dir or "").strip()
    if not settings.file_explorer_enabled or not settings.coding_enabled or not base or not Path(base).is_dir():
        raise HTTPException(status_code=404, detail="File Explorer is disabled")
    return base


def _safe(base: str, rel: str) -> str:
    """Resolve ``rel`` inside the jail or raise 400 (UDR-0069 D2)."""
    try:
        return resolve_safe_path(base, rel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _rel_of(base: str, abs_path: str) -> str:
    """Workspace-relative POSIX path for an absolute path inside the jail."""
    rel = os.path.relpath(abs_path, base)
    return "" if rel == "." else rel.replace("\\", "/")


# ---- Read endpoints (auth-gated; loopback bypass) ----


@router.get("/tree", dependencies=[Depends(verify_api_key)])
async def list_tree(dir: str = "") -> dict:
    """List one directory level (dirs first, then files), lazily and capped."""
    base = _explorer_base()
    safe_dir = _safe(base, dir)
    if not Path(safe_dir).is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    cap = max(1, settings.file_explorer_max_tree_entries)
    rows: list[dict] = []
    try:
        with os.scandir(safe_dir) as it:
            for entry in it:
                is_dir = entry.is_dir()
                rel = _rel_of(base, entry.path)
                row: dict = {"name": entry.name, "path": rel, "is_dir": is_dir}
                try:
                    st = entry.stat()
                    if not is_dir:
                        row["size"] = st.st_size
                    row["mtime"] = st.st_mtime
                except OSError:
                    pass
                rows.append(row)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="failed to list directory") from exc

    rows.sort(key=lambda r: (not r["is_dir"], r["name"].lower()))
    truncated = len(rows) > cap
    return {"dir": _rel_of(base, safe_dir), "entries": rows[:cap], "truncated": truncated}


@router.get("/file", dependencies=[Depends(verify_api_key)])
async def read_file(path: str) -> dict:
    """Read a text file with metadata; refuse oversize, flag binary read-only."""
    base = _explorer_base()
    safe = _safe(base, path)
    p = Path(safe)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        size = p.stat().st_size
        mtime = p.stat().st_mtime
    except OSError as exc:
        raise HTTPException(status_code=500, detail="failed to stat file") from exc

    cap = settings.file_explorer_max_file_bytes
    if size > cap:
        raise HTTPException(
            status_code=400,
            detail=f"File too large to open ({size} bytes; limit {cap}). Edit it another way.",
        )

    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="failed to read file") from exc

    is_binary = b"\x00" in raw[:_BINARY_SNIFF_BYTES]
    rel = _rel_of(base, safe)
    if is_binary:
        # Binary files are read-only in the editor (UDR-0069 D6): no content shipped.
        return {"path": rel, "content": None, "size": size, "mtime": mtime, "encoding": "binary", "is_binary": True}

    content = raw.decode("utf-8", errors="replace")
    return {"path": rel, "content": content, "size": size, "mtime": mtime, "encoding": "utf-8", "is_binary": False}


# ---- Mutating endpoints (consume CTR-0083) ----


class SaveBody(BaseModel):
    path: str
    content: str
    # Optional precondition: when set and the on-disk mtime drifted, save fails 409.
    expected_mtime: float | None = None


class EntryBody(BaseModel):
    path: str
    kind: str  # "file" | "dir"


@router.put("/file", dependencies=[Depends(verify_api_key)])
async def save_file(body: SaveBody) -> dict:
    """Save text content (last-write-wins; optional mtime precondition -> 409)."""
    base = _explorer_base()
    safe = _safe(base, body.path)
    p = Path(safe)
    if p.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory")

    encoded = body.content.encode("utf-8")
    cap = settings.file_explorer_max_file_bytes
    if len(encoded) > cap:
        raise HTTPException(status_code=400, detail=f"Content too large to save ({len(encoded)} bytes; limit {cap})")

    if body.expected_mtime is not None and p.is_file():
        try:
            current = p.stat().st_mtime
        except OSError:
            current = None
        # 1s tolerance for filesystem mtime granularity.
        if current is not None and abs(current - body.expected_mtime) > 1.0:
            raise HTTPException(status_code=409, detail="File changed on disk since it was opened")

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(encoded)
        mtime = p.stat().st_mtime
    except OSError as exc:
        raise HTTPException(status_code=500, detail="failed to write file") from exc

    logger.info("File Explorer saved %d bytes to %s", len(encoded), body.path)
    return {"path": _rel_of(base, safe), "size": len(encoded), "mtime": mtime}


@router.post("/entry", dependencies=[Depends(verify_api_key)], status_code=201)
async def create_entry(body: EntryBody) -> dict:
    """Create an empty file or a directory."""
    base = _explorer_base()
    if body.kind not in ("file", "dir"):
        raise HTTPException(status_code=400, detail="kind must be 'file' or 'dir'")
    safe = _safe(base, body.path)
    p = Path(safe)
    if p.exists():
        raise HTTPException(status_code=400, detail="Path already exists")

    try:
        if body.kind == "dir":
            p.mkdir(parents=True, exist_ok=False)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch(exist_ok=False)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="failed to create entry") from exc

    return {"path": _rel_of(base, safe), "is_dir": body.kind == "dir"}


@router.post("/move", dependencies=[Depends(verify_api_key)])
async def move_entry(body: dict) -> dict:
    """Rename or move a file/directory (also drag-move; UDR-0069 D9).

    The body is ``{"from": <rel>, "to": <rel>}``; a plain dict is used because ``from``
    is a Python keyword and cannot be a pydantic field name.
    """
    base = _explorer_base()
    src_rel = str(body.get("from", "")).strip()
    dst_rel = str(body.get("to", "")).strip()
    if not src_rel or not dst_rel:
        raise HTTPException(status_code=400, detail="'from' and 'to' are required")
    src = _safe(base, src_rel)
    dst = _safe(base, dst_rel)
    sp, dp = Path(src), Path(dst)
    if not sp.exists():
        raise HTTPException(status_code=404, detail="Source not found")
    if os.path.realpath(src) == os.path.realpath(base):
        raise HTTPException(status_code=400, detail="Cannot move the workspace root")
    if dp.exists():
        raise HTTPException(status_code=400, detail="Target already exists")

    try:
        dp.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dst)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="failed to move entry") from exc

    return {"from": _rel_of(base, src), "to": _rel_of(base, dst)}


@router.delete("/entry", dependencies=[Depends(verify_api_key)])
async def delete_entry(path: str) -> dict:
    """Delete a file or a directory (recursive). Never the workspace root."""
    base = _explorer_base()
    safe = _safe(base, path)
    p = Path(safe)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if os.path.realpath(safe) == os.path.realpath(base):
        raise HTTPException(status_code=400, detail="Cannot delete the workspace root")

    try:
        if p.is_dir():
            shutil.rmtree(safe)
        else:
            p.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="failed to delete entry") from exc

    logger.info("File Explorer deleted %s", _rel_of(base, safe))
    return {"path": _rel_of(base, safe), "deleted": True}


__all__ = ["router"]
