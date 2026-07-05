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
    GET    /api/workspace/raw?path=<rel>     -- stream one file's raw bytes (download/preview).
    GET    /api/workspace/archive?path=<rel> -- stream a directory as an on-the-fly ZIP.
    PUT    /api/workspace/file               -- save content (optional mtime guard).
    POST   /api/workspace/entry              -- create a file or a directory.
    POST   /api/workspace/move               -- rename / move (also drag-move).
    DELETE /api/workspace/entry?path=<rel>   -- delete a file or a directory.

CTR-0136 v2 (PRP-0093, UDR-0071) adds the two GET download/preview reads (``/raw`` and
``/archive``); both stay inside the same CTR-0031 jail and double gate. ``/raw`` powers the
PDF/image preview tabs and single-file download; ``/archive`` powers folder download. They are
bounded by FILE_EXPLORER_MAX_DOWNLOAD_BYTES / _ENTRIES (separate from the editor open/save cap).
"""

from collections.abc import Iterator
import logging
import mimetypes
import os
from pathlib import Path
import shutil
import zipfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
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


def _ascii_filename(name: str, fallback: str = "download") -> str:
    """ASCII-safe Content-Disposition filename (non-ASCII / quotes stripped)."""
    cleaned = "".join(c for c in name if c.isascii() and c not in '"\\\r\n').strip()
    return cleaned or fallback


class _ZipSink:
    """Write-only sink for streaming a ZIP (CTR-0136 v2, UDR-0071 D2).

    Having no seek()/tell() forces ``zipfile`` into non-seekable mode (data descriptors,
    no seek-back to patch local headers), so the bytes yielded to the client form a valid
    archive. ``take()`` drains the buffer between entries to keep memory bounded.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def write(self, data: bytes) -> int:
        self._buf += data
        return len(data)

    def flush(self) -> None:
        # File-like protocol no-op (zipfile may call flush()).
        pass

    def take(self) -> bytes:
        chunk = bytes(self._buf)
        self._buf.clear()
        return chunk


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


# ---- Download / preview reads (CTR-0136 v2, PRP-0093, UDR-0071 D2) ----


@router.get("/raw", dependencies=[Depends(verify_api_key)])
async def read_raw(path: str) -> FileResponse:
    """Stream a single file's raw bytes (download + PDF/image preview).

    Used by the PDF/image viewer tabs (fetched as a blob URL) and by single-file
    download. Streamed via FileResponse (no full in-memory read); the Content-Type is
    guessed from the extension. Bounded by FILE_EXPLORER_MAX_DOWNLOAD_BYTES (UDR-0071 D2).
    """
    base = _explorer_base()
    safe = _safe(base, path)
    p = Path(safe)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        size = p.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=500, detail="failed to stat file") from exc

    cap = settings.file_explorer_max_download_bytes
    if size > cap:
        raise HTTPException(
            status_code=400,
            detail=f"File too large to download ({size} bytes; limit {cap}).",
        )

    media_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    # inline so the SPA can render it in a viewer (it still saves via <a download> for download)
    return FileResponse(
        safe,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{_ascii_filename(p.name)}"'},
    )


@router.get("/archive", dependencies=[Depends(verify_api_key)])
async def read_archive(path: str = "") -> StreamingResponse:
    """Stream a directory as an on-the-fly ZIP (folder download).

    The directory subtree is walked within the jail, bounded by
    FILE_EXPLORER_MAX_DOWNLOAD_ENTRIES (file count) and FILE_EXPLORER_MAX_DOWNLOAD_BYTES
    (total uncompressed); an over-cap folder is refused with 400. The ZIP is streamed
    (no full in-memory buffer) (UDR-0071 D2). ``path=""`` archives the workspace root.
    """
    base = _explorer_base()
    safe = _safe(base, path)
    p = Path(safe)
    if not p.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    max_entries = max(1, settings.file_explorer_max_download_entries)
    max_bytes = settings.file_explorer_max_download_bytes

    # Collect the file list up front so caps are enforced BEFORE streaming any bytes.
    members: list[tuple[str, str]] = []  # (absolute path, arcname)
    total = 0
    root_name = p.name or "workspace"
    for fp in p.rglob("*"):
        # Skip symlinks defensively (do not follow out of the jail) and non-files.
        if fp.is_symlink() or not fp.is_file():
            continue
        try:
            total += fp.stat().st_size
        except OSError:
            continue
        if len(members) >= max_entries:
            raise HTTPException(
                status_code=400,
                detail=f"Folder has too many files to download (limit {max_entries}).",
            )
        if total > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"Folder too large to download (over {max_bytes} bytes uncompressed).",
            )
        arc = f"{root_name}/{fp.relative_to(p).as_posix()}"
        members.append((str(fp), arc))

    def _stream() -> Iterator[bytes]:
        # A write-only sink (no seek/tell) so zipfile streams in non-seekable mode --
        # it writes data descriptors and never seeks back, keeping entry offsets
        # consistent in the concatenated output. (A seekable BytesIO that is truncated
        # between entries corrupts the central-directory offsets: namelist() still works
        # but EXTRACTION fails, e.g. Windows "cannot open the folder".)
        sink = _ZipSink()
        with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for abs_path, arc in members:
                try:
                    zf.write(abs_path, arc)
                except OSError:
                    continue
                chunk = sink.take()
                if chunk:
                    yield chunk
        # Closing the ZipFile (end of the with block) writes the central directory.
        tail = sink.take()
        if tail:
            yield tail

    logger.info("File Explorer archiving %d files from %s", len(members), _rel_of(base, safe))
    return StreamingResponse(
        _stream(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{_ascii_filename(root_name)}.zip"'},
    )


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


# ---- Upload (CTR-0136 v3, PRP-0104, UDR-0083) ----

_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB streamed copy chunk


@router.post("/upload", dependencies=[Depends(verify_api_key)], status_code=201)
async def upload_files(
    dir: str = Form(""),
    files: list[UploadFile] = File(...),
    paths: list[str] = Form(default=[]),
) -> dict:
    """Upload one or more local files (and, via ``paths``, whole folders).

    Multipart form:
      - ``dir``   -- target directory (workspace-relative; "" = root).
      - ``files`` -- one or more uploaded files.
      - ``paths`` -- OPTIONAL, parallel to ``files``: each file's relative path
        (the browser's ``webkitRelativePath`` for a folder upload). When present
        for a file, it is written to ``dir/<paths[i]>`` (subdirectories created);
        otherwise the file lands directly in ``dir`` under its base name.

    Every destination resolves THROUGH the CTR-0031 realpath jail (UDR-0069 D2), so
    a ``..`` in a filename / relative path is rejected with 400. Bounded by
    FILE_EXPLORER_MAX_UPLOAD_FILES (count) and FILE_EXPLORER_MAX_UPLOAD_BYTES (total
    bytes, enforced while streaming). Existing files are overwritten (last-write-wins,
    like PUT /file). Same double gate (FILE_EXPLORER_ENABLED + CODING_ENABLED) and
    CTR-0083 auth as every other mutating endpoint (UDR-0083 D1/D2).
    """
    base = _explorer_base()
    target_dir = _safe(base, dir)
    if not Path(target_dir).is_dir():
        raise HTTPException(status_code=404, detail="Target directory not found")

    max_files = max(1, settings.file_explorer_max_upload_files)
    if len(files) > max_files:
        raise HTTPException(status_code=400, detail=f"Too many files in one upload (limit {max_files})")

    max_bytes = settings.file_explorer_max_upload_bytes
    dir_prefix = dir.strip().strip("/")
    written: list[dict] = []
    total = 0
    for idx, uf in enumerate(files):
        # A folder upload sends the browser's webkitRelativePath in paths[idx];
        # a plain multi-file upload sends only the base filename.
        rel_name = (paths[idx] if idx < len(paths) and paths[idx] else (uf.filename or f"upload_{idx}")).strip()
        rel_name = rel_name.replace("\\", "/").lstrip("/")
        if not rel_name:
            raise HTTPException(status_code=400, detail="An uploaded file has no name")
        dest_rel = f"{dir_prefix}/{rel_name}" if dir_prefix else rel_name
        dest = _safe(base, dest_rel)  # jail rejects any traversal
        p = Path(dest)
        if p.is_dir():
            raise HTTPException(status_code=400, detail=f"Upload target '{rel_name}' is an existing directory")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with p.open("wb") as out:
                while True:
                    chunk = await uf.read(_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    size += len(chunk)
                    if total > max_bytes:
                        out.close()
                        p.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=400,
                            detail=f"Upload too large (over {max_bytes} bytes total).",
                        )
                    out.write(chunk)
        except HTTPException:
            raise
        except OSError as exc:
            raise HTTPException(status_code=500, detail="failed to write uploaded file") from exc
        finally:
            await uf.close()
        written.append({"path": _rel_of(base, dest), "size": size})

    logger.info("File Explorer uploaded %d files (%d bytes) to %s", len(written), total, _rel_of(base, target_dir))
    return {"dir": _rel_of(base, target_dir), "uploaded": written, "count": len(written), "total_bytes": total}


__all__ = ["router"]
