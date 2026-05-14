"""Mask-based image editing API (CTR-0053, PRP-0028).

REST endpoint for user-initiated mask-based image editing (inpainting).
Accepts a composited image (source with transparent edit areas) and prompt.
Calls Azure OpenAI Images Edit API. The transparent regions in the image
indicate areas to regenerate.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.auth import verify_api_key
from app.core.config import settings
from app.image_gen.tools import _get_client, _save_image

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/images", tags=["Image Edit"])


def _edit_with_mask_sync(
    image_bytes: bytes,
    prompt: str,
    thread_id: str,
    size: str,
    quality: str,
) -> dict:
    """Synchronous mask-based image editing.

    The image parameter is a PNG with transparent areas indicating regions
    to regenerate. No separate mask file is sent to the API.
    """
    client = _get_client()

    result = client.images.edit(
        model=settings.image_deployment_name,
        image=("image.png", image_bytes, "image/png"),
        prompt=prompt,
        size=size,
        quality=quality,
        n=1,
    )

    images = []
    for item in result.data:
        b64 = getattr(item, "b64_json", None)
        if not b64:
            continue
        filename, uri = _save_image(thread_id, b64, "png")
        images.append(
            {
                "url": uri,
                "filename": filename,
                "revised_prompt": getattr(item, "revised_prompt", None) or prompt,
            }
        )

    return {
        "images": images,
        "count": len(images),
        "tool": "mask_edit",
    }


@router.post("/edit", dependencies=[Depends(verify_api_key)])
async def edit_image_with_mask(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    thread_id: str = Form(...),
    size: str = Form("auto"),
    quality: str = Form("auto"),
):
    """Edit an image with transparent regions indicating areas to regenerate (CTR-0053).

    The frontend composites the source image with the user-drawn mask:
    painted areas become transparent (alpha=0) in the PNG. The Azure
    OpenAI Images Edit API treats transparent pixels as areas to regenerate.
    """
    if not settings.image_deployment_name:
        raise HTTPException(status_code=503, detail="Image generation not configured (IMAGE_DEPLOYMENT_NAME)")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Image file is empty")

    try:
        result = await asyncio.to_thread(
            _edit_with_mask_sync,
            image_bytes,
            prompt,
            thread_id,
            size,
            quality,
        )
    except Exception as exc:
        exc_str = str(exc)
        if "moderation_blocked" in exc_str or "safety system" in exc_str:
            logger.warning("Mask edit blocked by content moderation: %s", exc_str)
            raise HTTPException(
                status_code=422,
                detail="Request blocked by content safety filter. Try a different prompt or image.",
            ) from exc
        logger.exception("Mask edit API error")
        raise HTTPException(status_code=502, detail=f"Image editing failed: {exc}") from exc

    if not result.get("images"):
        raise HTTPException(status_code=502, detail="No image returned from API")

    return result
