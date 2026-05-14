"""File upload REST API (CTR-0022, CTR-0078).

Provides endpoints for uploading images and PDFs, and serving uploaded files.
Uploaded files are stored in {UPLOAD_DIR}/{thread_id}/{filename}.
Validation policy is shared with CLI preflight checks via app.upload.validation.
"""

import logging
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import verify_api_key
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

    # Save to {UPLOAD_DIR}/{thread_id}/{filename}
    dest_dir = _upload_dir() / thread_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / validated.safe_filename
    dest_path.write_bytes(data)

    uri = f"/api/uploads/{thread_id}/{validated.safe_filename}"
    logger.info("Uploaded %s (%d bytes) to %s", validated.safe_filename, len(data), dest_path)

    return UploadResponse(
        uri=uri,
        media_type=validated.content_type,
        filename=validated.safe_filename,
    )


@router.get("/api/uploads/{thread_id}/{filename}")
async def serve_upload(thread_id: str, filename: str) -> FileResponse:
    """Serve an uploaded file."""
    safe_name = Path(filename).name
    file_path = (_upload_dir() / thread_id / safe_name).resolve()
    upload_root = _upload_dir().resolve()

    if not file_path.is_relative_to(upload_root):
        raise HTTPException(status_code=403, detail="Access denied")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    media_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    return FileResponse(file_path, media_type=media_type)
