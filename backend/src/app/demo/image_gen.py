"""Demo image generation / edit / mask-edit (PRP-0066, UDR-0041 D3).

Replaces the live Azure OpenAI Images calls in
``app.image_gen.tools`` and ``app.image_gen.router`` when
``DEMO_MODE=true``. Returns one of two bundled placeholder PNGs
deterministically rotating per call so multi-image (n>1) flows render
distinct images for the reviewer.

The returned JSON has the same shape as the live tool output so the
SPA ImageGenerationResult component (CTR-0051) renders without any
client-side change.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import shutil
from typing import Any
import uuid

from app.core.config import settings

logger = logging.getLogger(__name__)


_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_PLACEHOLDER_FILENAMES = ("placeholder_image_1.png", "placeholder_image_2.png")
_PLACEHOLDER_EDIT_FILENAME = "placeholder_image_edit.png"

_call_counter = 0  # module-local rotation so successive calls return distinct images


def _next_placeholder(generate: bool) -> Path:
    """Pick the next bundled placeholder PNG, rotating deterministically."""
    global _call_counter
    if not generate:
        edit = _ASSET_DIR / _PLACEHOLDER_EDIT_FILENAME
        if edit.is_file():
            return edit
        # Fall back to the generate set if the edit asset is missing.
    candidates = [_ASSET_DIR / name for name in _PLACEHOLDER_FILENAMES]
    existing = [p for p in candidates if p.is_file()]
    if not existing:
        # No assets at all -- emit a tiny synthesised 1x1 PNG so the
        # boundary still returns valid image bytes. (Brand-empty install /
        # pre-asset test.)
        return _ASSET_DIR / "missing.png"
    pick = existing[_call_counter % len(existing)]
    _call_counter += 1
    return pick


_TINY_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cf00000003000100018ddd0000000049454e44ae426082"
)


def _copy_into_session(thread_id: str, source: Path, output_format: str = "png") -> tuple[str, str]:
    """Copy a placeholder PNG into the session upload directory."""
    ext = output_format if output_format in ("png", "jpeg", "webp") else "png"
    filename = f"generated_{uuid.uuid4().hex[:12]}.{ext}"
    save_dir = Path(settings.upload_dir) / thread_id
    save_dir.mkdir(parents=True, exist_ok=True)
    target = save_dir / filename
    if source.is_file():
        shutil.copyfile(source, target)
    else:
        target.write_bytes(_TINY_PNG_BYTES)
    uri = f"/api/uploads/{thread_id}/{filename}"
    logger.info("DemoImageProvider saved placeholder: %s", uri)
    return filename, uri


def _build_payload(thread_id: str, prompt: str, n: int, *, tool: str, generate: bool) -> str:
    """Return the JSON payload expected by the SPA ImageGenerationResult."""
    n = max(1, min(n, 4))
    images: list[dict[str, Any]] = []
    for _ in range(n):
        source = _next_placeholder(generate=generate)
        filename, uri = _copy_into_session(thread_id, source)
        images.append(
            {
                "url": uri,
                "filename": filename,
                "revised_prompt": f"[DEMO] {prompt}",
                "size": "1024x1024",
            }
        )
    return json.dumps(
        {
            "images": images,
            "count": len(images),
            "tool": tool,
            "demo": True,
        }
    )


async def demo_generate_image(*, prompt: str, n: int, thread_id: str) -> str:
    """Return a JSON payload with bundled placeholder PNG URIs."""
    await asyncio.sleep(0)  # cooperative yield
    return await asyncio.to_thread(_build_payload, thread_id, prompt, n, tool="generate_image", generate=True)


async def demo_edit_image(*, prompt: str, n: int, thread_id: str, image_filename: str) -> str:
    """Return a JSON payload with the demo 'edited' placeholder PNG.

    ``image_filename`` is preserved in the response for parity with the
    live tool, but the actual image content is the bundled
    ``placeholder_image_edit.png``.
    """
    await asyncio.sleep(0)
    payload_json = await asyncio.to_thread(
        _build_payload,
        thread_id,
        prompt,
        n,
        tool="edit_image",
        generate=False,
    )
    payload = json.loads(payload_json)
    payload["source_image"] = image_filename
    return json.dumps(payload)


def demo_mask_edit_sync(*, prompt: str, thread_id: str) -> dict[str, Any]:
    """Return the dict expected by CTR-0053 POST /api/images/edit.

    Synchronous so the existing router can call it in the same
    ``asyncio.to_thread`` slot used for the live API call.
    """
    source = _next_placeholder(generate=False)
    filename, uri = _copy_into_session(thread_id, source)
    return {
        "images": [
            {
                "url": uri,
                "filename": filename,
                "revised_prompt": f"[DEMO mask edit] {prompt}",
            }
        ],
        "count": 1,
        "tool": "mask_edit",
        "demo": True,
    }


__all__ = ["demo_edit_image", "demo_generate_image", "demo_mask_edit_sync"]
