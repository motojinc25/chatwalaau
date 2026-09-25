"""Image Edit Draft API (CTR-0222, PRP-0187, UDR-0169 D11).

Stores the Canvas Image Editor's state for one image as a SIDECAR next to that image
in the CTR-0022 upload directory -- the same design as the paint scene sidecar
(CTR-0161), reused rather than reinvented:

    {UPLOAD_DIR}/{thread_id}/photo.png            the image being edited
    {UPLOAD_DIR}/{thread_id}/photo.imgedit.json   its editor draft (this contract)

"The draft belongs to the image you pressed Edit on": Edit on an image that has a
sidecar restores it; an image without one (a fresh generated result) opens a new
edit. The document is a CLIENT payload convention (mask layer bitmap, annotation
vectors, reference URLs, change / preserve text, editor options); the backend stores
it as opaque bytes after one JSON well-formedness check and never interprets it.
Mutations consume CTR-0083; the size cap is a fixed constant (no environment
variable), mirroring CTR-0161. Export/import and fork carry the sidecar (CTR-0015).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import verify_api_key
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Image Edit"])

# Fixed draft-size cap. The mask layer travels as a PNG data URL, so a draft can be a
# few MB for a large source; 25 MB mirrors the paint scene cap (CTR-0161).
MAX_DRAFT_SIZE_BYTES = 25 * 1024 * 1024

DRAFT_SIDECAR_SUFFIX = ".imgedit.json"


def _resolve_sidecar(thread_id: str, name: str) -> Path:
    """Resolve the draft path for an image, confined to the upload root.

    Keyed by the image's filename stem (``photo.png`` -> ``photo.imgedit.json``).
    Both the thread id and the name are reduced to a single path segment (the
    CTR-0161 / CTR-0022 confinement).
    """
    safe_thread = Path(thread_id).name
    safe_name = Path(name).name
    stem = Path(safe_name).stem
    if not safe_thread or not stem or stem.startswith(".") or safe_name != name:
        raise HTTPException(status_code=400, detail="Invalid image name")

    upload_root = Path(settings.upload_dir).resolve()
    sidecar = (upload_root / safe_thread / f"{stem}{DRAFT_SIDECAR_SUFFIX}").resolve()
    if not sidecar.is_relative_to(upload_root):
        raise HTTPException(status_code=403, detail="Access denied")
    return sidecar


class DraftSaveResponse(BaseModel):
    ok: bool
    bytes: int


@router.put("/api/images/edit-drafts/{thread_id}/{name}", dependencies=[Depends(verify_api_key)])
async def save_draft(thread_id: str, name: str, request: Request) -> DraftSaveResponse:
    """Save (or replace) the editor draft for an image."""
    body = await request.body()
    if len(body) > MAX_DRAFT_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image edit draft too large. Maximum size: {MAX_DRAFT_SIZE_BYTES // (1024 * 1024)}MB",
        )
    try:
        json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Image edit draft must be valid JSON") from exc

    sidecar = _resolve_sidecar(thread_id, name)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(body)
    logger.info("Saved image edit draft %s (%d bytes)", sidecar.name, len(body))
    return DraftSaveResponse(ok=True, bytes=len(body))


@router.get("/api/images/edit-drafts/{thread_id}/{name}", dependencies=[Depends(verify_api_key)])
async def load_draft(thread_id: str, name: str) -> FileResponse:
    """Return the editor draft for an image (404 when the image has none)."""
    sidecar = _resolve_sidecar(thread_id, name)
    if not sidecar.is_file():
        raise HTTPException(status_code=404, detail="Image edit draft not found")
    return FileResponse(sidecar, media_type="application/json")


@router.delete("/api/images/edit-drafts/{thread_id}/{name}", dependencies=[Depends(verify_api_key)])
async def delete_draft(thread_id: str, name: str) -> Response:
    """Remove the editor draft for an image (idempotent)."""
    sidecar = _resolve_sidecar(thread_id, name)
    sidecar.unlink(missing_ok=True)
    return Response(status_code=204)
