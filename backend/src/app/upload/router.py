"""File upload REST API (CTR-0022, CTR-0078).

Provides endpoints for uploading images and PDFs, and serving uploaded files.
Uploaded files are stored in {UPLOAD_DIR}/{thread_id}/{filename}; an upload never
replaces an existing file -- a taken name is stored as {stem}-N{suffix} (UDR-0211 D5).
Validation policy is shared with CLI preflight checks via app.upload.validation.
"""

from collections.abc import Iterator
import logging
import mimetypes
from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import verify_api_key, verify_api_key_no_loopback
from app.core.config import settings
from app.upload.validation import UploadValidationError, validate_upload_metadata

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Upload"])


def _upload_dir() -> Path:
    return Path(settings.upload_dir)


class UploadResponse(BaseModel):
    uri: str
    media_type: str
    filename: str


@router.post("/api/upload/{thread_id}", dependencies=[Depends(verify_api_key)])
async def upload_file(thread_id: str, file: UploadFile) -> UploadResponse:
    """Upload a supported file for a session."""
    data = await file.read()
    try:
        validated = validate_upload_metadata(file.filename, file.content_type or "", len(data))
    except UploadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Save to {UPLOAD_DIR}/{thread_id}/{filename}; a taken name gets a -N suffix
    # (UDR-0211 D5), so an upload never replaces an earlier file.
    dest_dir = _upload_dir() / thread_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _store_unique(dest_dir, validated.safe_filename, data)
    stored_name = dest_path.name

    uri = f"/api/uploads/{thread_id}/{stored_name}"
    logger.info("Uploaded %s (%d bytes) to %s", stored_name, len(data), dest_path)

    return UploadResponse(
        uri=uri,
        media_type=validated.content_type,
        filename=stored_name,
    )


_MAX_NAME_BYTES = 255
_MAX_NUMBERED_SUFFIX = 999


def _fit_name(stem: str, tag: str, suffix: str) -> str:
    """Return ``{stem}{tag}{suffix}``, shortening the stem to stay within 255 UTF-8 bytes."""
    budget = _MAX_NAME_BYTES - len(tag.encode("utf-8")) - len(suffix.encode("utf-8"))
    encoded = stem.encode("utf-8")
    if len(encoded) > budget:
        stem = encoded[: max(budget, 1)].decode("utf-8", errors="ignore") or "file"
    return f"{stem}{tag}{suffix}"


def _candidate_names(safe_filename: str) -> Iterator[str]:
    """Yield the validated name, then name-1 .. name-999, then name-{8 hex} (UDR-0211 D5)."""
    yield safe_filename
    path = Path(safe_filename)
    stem, suffix = path.stem, path.suffix
    for n in range(1, _MAX_NUMBERED_SUFFIX + 1):
        yield _fit_name(stem, f"-{n}", suffix)
    while True:
        yield _fit_name(stem, f"-{uuid.uuid4().hex[:8]}", suffix)


def _store_unique(dest_dir: Path, safe_filename: str, data: bytes) -> Path:
    """Write ``data`` under the first free candidate name with an exclusive create.

    ``open(path, "xb")`` is atomic, so two concurrent uploads of one name get two
    files, and it follows the file system's own case rules (on NTFS ``Photo.png``
    and ``photo.png`` collide).
    """
    for name in _candidate_names(safe_filename):
        dest_path = dest_dir / name
        try:
            with dest_path.open("xb") as f:
                f.write(data)
        except FileExistsError:
            continue
        return dest_path
    raise AssertionError("unreachable: candidate names are unbounded")


@router.get("/api/uploads/{thread_id}/{filename}")
async def serve_upload(thread_id: str, filename: str, request: Request) -> FileResponse:
    """Serve an uploaded file (CTR-0022 v4, UDR-0097).

    Gated by the unified auth model. A same-origin browser request -- an ``<img>``,
    an ``<a>`` navigation, or a ``fetch`` -- automatically carries the session
    cookie, so a logged-in SPA displays and opens images while an unauthenticated
    caller gets 401. When web login (``AUTH_USERNAME``) is enabled the loopback
    bypass does NOT apply to this route (UDR-0097 D5), so a directly-opened image
    URL cannot be viewed without a session even on ``localhost`` / behind a proxy;
    with no web login configured, zero-config localhost stays open. The agent reads
    image bytes from DISK, not through this route.
    """
    await verify_api_key_no_loopback(request)

    safe_name = Path(filename).name
    file_path = (_upload_dir() / thread_id / safe_name).resolve()
    upload_root = _upload_dir().resolve()

    if not file_path.is_relative_to(upload_root):
        raise HTTPException(status_code=403, detail="Access denied")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    media_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    return FileResponse(file_path, media_type=media_type)
