"""Paint Scene Persistence REST API (CTR-0161, PRP-0099).

Stores the editable Fabric.js scene JSON for a painted image attachment as a
SIDECAR co-located with the CTR-0022 upload directory:

    {UPLOAD_DIR}/{thread_id}/paint_{uuid}.png          the attachment (CTR-0022, LLM input)
    {UPLOAD_DIR}/{thread_id}/paint_{uuid}.paint.json   the editable scene (this contract)

The sidecar is keyed 1:1 to the uploaded image filename, so presence of the
sidecar is the signal that an attachment is "paint-origin" and re-editable
(UDR-0078 D2/D3). Mutations (PUT/DELETE) consume CTR-0083 (verify_api_key, with
the loopback bypass preserved); GET is a read with the same dependency. The scene
body is bounded by a FIXED size cap (no new environment variable -- the CTR-0022
fixed-limit precedent, UDR-0078 D8). The backend stores the scene as opaque bytes
after a single JSON well-formedness check; it never executes or interprets it.
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

router = APIRouter(tags=["Paint"])

# Fixed scene-size cap. Imported raster images embed in the Fabric scene as data
# URLs, so a paint scene can be substantially larger than a flat vector scene.
# A constant (not an env var) mirrors the CTR-0022 fixed 20MB/50MB limits and
# keeps the CTR-0097 .env.template completeness invariant untouched (UDR-0078 D8).
MAX_SCENE_SIZE_BYTES = 25 * 1024 * 1024

_SIDECAR_SUFFIX = ".paint.json"


def _upload_dir() -> Path:
    return Path(settings.upload_dir)


def _resolve_sidecar(thread_id: str, name: str) -> Path:
    """Resolve the sidecar path for an uploaded image, with traversal protection.

    The sidecar is keyed by the uploaded image's filename stem
    (``paint_{uuid}.png`` -> ``paint_{uuid}.paint.json``). Both the thread id and
    the image name are reduced to a single path segment, and the resolved path is
    confined to the upload root (the CTR-0022 serve_upload precedent).
    """
    safe_thread = Path(thread_id).name
    safe_name = Path(name).name
    stem = Path(safe_name).stem
    if not safe_thread or not stem or stem.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid paint scene name")

    upload_root = _upload_dir().resolve()
    sidecar = (upload_root / safe_thread / f"{stem}{_SIDECAR_SUFFIX}").resolve()
    if not sidecar.is_relative_to(upload_root):
        raise HTTPException(status_code=403, detail="Access denied")
    return sidecar


class PaintSceneResponse(BaseModel):
    ok: bool
    bytes: int


@router.put("/api/paint/{thread_id}/{name}", dependencies=[Depends(verify_api_key)])
async def save_scene(thread_id: str, name: str, request: Request) -> PaintSceneResponse:
    """Save (or replace) the Fabric scene sidecar for an uploaded image."""
    body = await request.body()
    if len(body) > MAX_SCENE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Paint scene too large. Maximum size: {MAX_SCENE_SIZE_BYTES // (1024 * 1024)}MB",
        )
    try:
        json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Paint scene must be valid JSON") from exc

    sidecar = _resolve_sidecar(thread_id, name)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(body)
    logger.info("Saved paint scene %s (%d bytes)", sidecar.name, len(body))
    return PaintSceneResponse(ok=True, bytes=len(body))


@router.get("/api/paint/{thread_id}/{name}", dependencies=[Depends(verify_api_key)])
async def load_scene(thread_id: str, name: str) -> FileResponse:
    """Return the Fabric scene sidecar for an uploaded image (404 if none)."""
    sidecar = _resolve_sidecar(thread_id, name)
    if not sidecar.is_file():
        raise HTTPException(status_code=404, detail="Paint scene not found")
    return FileResponse(sidecar, media_type="application/json")


@router.delete("/api/paint/{thread_id}/{name}", dependencies=[Depends(verify_api_key)])
async def delete_scene(thread_id: str, name: str) -> Response:
    """Remove the Fabric scene sidecar for an uploaded image (idempotent)."""
    sidecar = _resolve_sidecar(thread_id, name)
    sidecar.unlink(missing_ok=True)
    return Response(status_code=204)
