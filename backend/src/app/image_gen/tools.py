"""Image generation tools for the main agent (CTR-0049, PRP-0027).

Provides generate_image and edit_image as MAF function tools.
Uses Azure OpenAI Images API with gpt-image-1.5 (configurable via
IMAGE_DEPLOYMENT_NAME). Generated images are saved to the session
upload directory (.uploads/{thread_id}/generated_{uuid}.{ext}).

Tools execute in a thread pool via asyncio.to_thread() to prevent blocking
the FastAPI async event loop during API calls.
"""

import asyncio
import base64
import contextvars
import json
import logging
from pathlib import Path
from typing import Annotated
import uuid

from azure.identity import AzureCliCredential, get_bearer_token_provider
from openai import AzureOpenAI
from pydantic import Field

from app.core.config import settings

logger = logging.getLogger(__name__)

# Context variable for thread_id -- set by endpoint.py before agent.run()
current_thread_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_thread_id")

# Lazy-initialized Azure OpenAI client for image generation
_client: AzureOpenAI | None = None


def _get_client() -> AzureOpenAI:
    """Get or create the Azure OpenAI client for image generation."""
    global _client
    if _client is None:
        token_provider = get_bearer_token_provider(
            AzureCliCredential(),
            "https://cognitiveservices.azure.com/.default",
        )
        _client = AzureOpenAI(
            azure_ad_token_provider=token_provider,
            azure_endpoint=settings.azure_openai_endpoint,
            api_version="2025-04-01-preview",
        )
    return _client


def _save_image(thread_id: str, image_b64: str, output_format: str) -> tuple[str, str]:
    """Decode base64 image data and save to the upload directory.

    Returns (filename, uri) tuple.
    """
    ext = output_format if output_format in ("png", "jpeg", "webp") else "png"
    filename = f"generated_{uuid.uuid4().hex[:12]}.{ext}"
    save_dir = Path(settings.upload_dir) / thread_id
    save_dir.mkdir(parents=True, exist_ok=True)
    file_path = save_dir / filename
    file_path.write_bytes(base64.b64decode(image_b64))
    uri = f"/api/uploads/{thread_id}/{filename}"
    logger.info("Saved generated image: %s", uri)
    return filename, uri


def _generate_image_sync(
    prompt: str,
    size: str,
    quality: str,
    output_format: str,
    background: str,
    n: int,
) -> str:
    """Synchronous image generation implementation."""
    thread_id = current_thread_id.get("")
    if not thread_id:
        return json.dumps({"error": "No active session (thread_id not set)"})

    client = _get_client()
    n = max(1, min(n, 4))

    try:
        result = client.images.generate(
            model=settings.image_deployment_name,
            prompt=prompt,
            size=size,
            quality=quality,
            n=n,
            background=background,
            output_format=output_format,
        )
    except Exception as exc:
        logger.exception("Image generation API error")
        return json.dumps({"error": f"Image generation failed: {exc}"})

    images = []
    for item in result.data:
        b64 = item.b64_json
        if not b64:
            continue
        filename, uri = _save_image(thread_id, b64, output_format)
        images.append(
            {
                "url": uri,
                "filename": filename,
                "revised_prompt": getattr(item, "revised_prompt", None) or prompt,
                "size": size,
            }
        )

    return json.dumps(
        {
            "images": images,
            "count": len(images),
            "tool": "generate_image",
        }
    )


def _edit_image_sync(
    prompt: str,
    image_filename: str,
    size: str,
    quality: str,
    output_format: str,
    background: str,
    n: int,
) -> str:
    """Synchronous image editing implementation."""
    thread_id = current_thread_id.get("")
    if not thread_id:
        return json.dumps({"error": "No active session (thread_id not set)"})

    # Resolve source image path
    image_path = Path(settings.upload_dir) / thread_id / image_filename
    if not image_path.is_file():
        # Try to find the file by searching the session directory
        session_dir = Path(settings.upload_dir) / thread_id
        if session_dir.is_dir():
            candidates = list(session_dir.iterdir())
            file_names = [f.name for f in candidates if f.is_file()]
            return json.dumps(
                {
                    "error": f"Image not found: {image_filename}. Available files: {file_names}",
                }
            )
        return json.dumps({"error": f"Image not found: {image_filename}"})

    client = _get_client()
    n = max(1, min(n, 4))

    try:
        with image_path.open("rb") as f:
            result = client.images.edit(
                model=settings.image_deployment_name,
                image=f,
                prompt=prompt,
                size=size,
                quality=quality,
                n=n,
                background=background,
            )
    except Exception as exc:
        logger.exception("Image edit API error")
        return json.dumps({"error": f"Image editing failed: {exc}"})

    images = []
    for item in result.data:
        b64 = getattr(item, "b64_json", None)
        if not b64:
            continue
        filename, uri = _save_image(thread_id, b64, output_format)
        images.append(
            {
                "url": uri,
                "filename": filename,
                "revised_prompt": getattr(item, "revised_prompt", None) or prompt,
                "size": size,
            }
        )

    return json.dumps(
        {
            "images": images,
            "count": len(images),
            "tool": "edit_image",
            "source_image": image_filename,
        }
    )


# ---- Public async tool functions (registered on MAF agent) ----


async def generate_image(
    prompt: Annotated[str, Field(description="Detailed description of the image to generate")],
    size: Annotated[str, Field(description="Image size: auto, 1024x1024, 1024x1536, or 1536x1024")] = "auto",
    quality: Annotated[str, Field(description="Image quality: auto, low, medium, or high")] = "auto",
    output_format: Annotated[str, Field(description="Output format: png, jpeg, or webp")] = "png",
    background: Annotated[str, Field(description="Background: auto, transparent, or opaque")] = "auto",
    n: Annotated[int, Field(description="Number of images to generate (1-4)")] = 1,
) -> str:
    """Generate an image from a text description using AI."""
    return await asyncio.to_thread(
        _generate_image_sync,
        prompt,
        size,
        quality,
        output_format,
        background,
        n,
    )


async def edit_image(
    prompt: Annotated[str, Field(description="Description of the desired edit to the image")],
    image_filename: Annotated[
        str, Field(description="Filename of the source image in the session (e.g., photo.jpg, generated_abc123.png)")
    ],
    size: Annotated[str, Field(description="Output image size: auto, 1024x1024, 1024x1536, or 1536x1024")] = "auto",
    quality: Annotated[str, Field(description="Image quality: auto, low, medium, or high")] = "auto",
    output_format: Annotated[str, Field(description="Output format: png, jpeg, or webp")] = "png",
    background: Annotated[str, Field(description="Background: auto, transparent, or opaque")] = "auto",
    n: Annotated[int, Field(description="Number of edited images to generate (1-4)")] = 1,
) -> str:
    """Edit an existing image based on a text description (full image edit, no mask)."""
    return await asyncio.to_thread(
        _edit_image_sync,
        prompt,
        image_filename,
        size,
        quality,
        output_format,
        background,
        n,
    )
