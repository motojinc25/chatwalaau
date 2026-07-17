"""Image generation tools for the main agent (CTR-0049, PRP-0027).

Provides generate_image and edit_image as MAF function tools.
Uses Azure OpenAI Images API with the deployment declared by the single ``image``
offering in the Model Offering Catalog (PRP-0114, UDR-0095 D1): the tools are only
registered when such an offering exists (or DEMO_MODE), so a non-demo call always
has one to resolve. Generated images are saved to the session upload directory
(.uploads/{thread_id}/generated_{uuid}.{ext}).

Image output options (size / quality / format / compression / background) follow a
precedence (PRP-0085 / PRP-0114, FEAT-0044, UDR-0095 D3):
  explicit LLM argument > state.image_options (per-session UI)
  > offering.image_defaults > API default.
The per-session selection is delivered by the AG-UI endpoint (CTR-0009) into the
current_image_options contextvar before agent.run(); the LLM tool arguments default
to None so an omitted argument falls through to the session / offering default.

Tools execute in a thread pool via asyncio.to_thread() to prevent blocking
the FastAPI async event loop during API calls.
"""

import asyncio
import base64
import contextlib
import contextvars
import json
import logging
from pathlib import Path
from typing import Annotated
import uuid

from openai import AzureOpenAI
from pydantic import Field

from app import models_catalog
from app.azure_credential import get_azure_openai_kwargs
from app.core.config import settings

logger = logging.getLogger(__name__)

# Context variable for thread_id -- set by endpoint.py before agent.run()
current_thread_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_thread_id")

# Per-session image output options (PRP-0085, CTR-0120/CTR-0009). Set by the AG-UI
# endpoint before agent.run() from state.image_options; unset / empty means the
# user made no selection (fall through to settings / API defaults).
current_image_options: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "current_image_options", default=None
)

# Lazy-initialized Azure OpenAI client for image generation
_client: AzureOpenAI | None = None


def _resolve_option(arg: str | None, key: str, default_value: object) -> str:
    """Resolve one image output option by precedence (PRP-0114, UDR-0095 D3).

    explicit LLM argument > state.image_options[key] > offering.image_defaults[key]
    > "" (API default). A blank / None value at any tier is treated as "unspecified"
    and falls through. ``default_value`` comes from
    models_catalog.image_output_defaults() and may be a str (size/quality/...) or an
    int (compression); it is coerced to str.
    """
    if arg:
        return arg
    session = current_image_options.get() or {}
    session_value = session.get(key)
    if session_value:
        return str(session_value)
    if default_value is not None and str(default_value) != "":
        return str(default_value)
    return ""


def _get_client() -> AzureOpenAI:
    """Get or create the Azure OpenAI client for image generation.

    Credential resolution centralised in app.azure_credential (PRP-0058,
    UDR-0034). AZURE_OPENAI_API_KEY set -> api_key= shape; unset ->
    AzureCliCredential() via azure_ad_token_provider.

    Model Offering Catalog (PRP-0114, UDR-0095 D1): the single ``image`` offering
    supplies the deployment / endpoint / api_version / api_key (a base_url offering
    builds a plain OpenAI client). The offering may omit ``endpoint`` (falls back to
    the shared AZURE_OPENAI_ENDPOINT) and ``api_version`` (falls back to
    DEFAULT_IMAGE_API_VERSION). This function is only reached (non-demo) when an
    image offering exists -- the tools are otherwise not registered (CTR-0050) --
    so ``config`` is normally present; the defensive fallbacks keep it safe. Image
    generation stays on the dedicated Images API (UDR-0045 D7).
    """
    global _client
    if _client is None:
        config = models_catalog.image_config()
        if config is not None and config.base_url:
            from openai import OpenAI

            _client = OpenAI(api_key=config.api_key or settings.openai_api_key or "", base_url=config.base_url)
        else:
            endpoint = config.endpoint if (config is not None and config.endpoint) else settings.azure_openai_endpoint
            api_version = (
                config.api_version
                if (config is not None and config.api_version)
                else models_catalog.DEFAULT_IMAGE_API_VERSION
            )
            cred_kwargs = (
                {"api_key": config.api_key} if (config is not None and config.api_key) else get_azure_openai_kwargs()
            )
            _client = AzureOpenAI(
                azure_endpoint=endpoint,
                api_version=api_version,
                **cred_kwargs,
            )
    return _client


def _image_deployment() -> str:
    """Resolve the image deployment name from the catalog image offering (PRP-0114).

    Returns "" when no image offering is configured; in a non-demo deployment the
    tools are not registered in that case (CTR-0050), so this is defensive.
    """
    config = models_catalog.image_config()
    return config.deployment if config is not None else ""


def _resolve_image_params(
    size: str | None,
    quality: str | None,
    output_format: str | None,
    background: str | None,
    compression: str | None,
) -> dict:
    """Resolve the effective image output parameters by precedence (UDR-0095 D3).

    Returns a kwargs dict for the Azure Images API. The operator DEFAULT tier is the
    image offering's ``image_defaults`` block (PRP-0114), replacing the removed
    CTR-0006 IMAGE_* settings. output_compression is included only when the resolved
    format is jpeg or webp and a compression value exists.
    """
    defaults = models_catalog.image_output_defaults()
    fmt = _resolve_option(output_format, "format", defaults.get("format")) or "png"
    params: dict = {
        "size": _resolve_option(size, "size", defaults.get("size")) or "auto",
        "quality": _resolve_option(quality, "quality", defaults.get("quality")) or "auto",
        "output_format": fmt,
        "background": _resolve_option(background, "background", defaults.get("background")) or "auto",
    }
    if fmt in ("jpeg", "webp"):
        comp = _resolve_option(compression, "compression", defaults.get("compression"))
        if comp:
            with contextlib.suppress(TypeError, ValueError):
                params["output_compression"] = max(0, min(100, int(comp)))
    return params


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
    size: str | None,
    quality: str | None,
    output_format: str | None,
    background: str | None,
    compression: str | None,
    n: int,
) -> str:
    """Synchronous image generation implementation."""
    thread_id = current_thread_id.get("")
    if not thread_id:
        return json.dumps({"error": "No active session (thread_id not set)"})

    client = _get_client()
    n = max(1, min(n, 4))
    params = _resolve_image_params(size, quality, output_format, background, compression)

    try:
        result = client.images.generate(
            model=_image_deployment(),
            prompt=prompt,
            n=n,
            **params,
        )
    except Exception as exc:
        logger.exception("Image generation API error")
        return json.dumps({"error": f"Image generation failed: {exc}"})

    images = []
    for item in result.data:
        b64 = item.b64_json
        if not b64:
            continue
        filename, uri = _save_image(thread_id, b64, params["output_format"])
        images.append(
            {
                "url": uri,
                "filename": filename,
                "revised_prompt": getattr(item, "revised_prompt", None) or prompt,
                "size": params["size"],
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
    size: str | None,
    quality: str | None,
    output_format: str | None,
    background: str | None,
    compression: str | None,
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
    params = _resolve_image_params(size, quality, output_format, background, compression)

    try:
        with image_path.open("rb") as f:
            result = client.images.edit(
                model=_image_deployment(),
                image=f,
                prompt=prompt,
                n=n,
                **params,
            )
    except Exception as exc:
        logger.exception("Image edit API error")
        return json.dumps({"error": f"Image editing failed: {exc}"})

    images = []
    for item in result.data:
        b64 = getattr(item, "b64_json", None)
        if not b64:
            continue
        filename, uri = _save_image(thread_id, b64, params["output_format"])
        images.append(
            {
                "url": uri,
                "filename": filename,
                "revised_prompt": getattr(item, "revised_prompt", None) or prompt,
                "size": params["size"],
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
    size: Annotated[
        str | None,
        Field(description="Image size: auto, 1024x1024, 1024x1536, or 1536x1024 (omit to use the user's default)"),
    ] = None,
    quality: Annotated[
        str | None, Field(description="Image quality: auto, low, medium, or high (omit to use the user's default)")
    ] = None,
    output_format: Annotated[
        str | None, Field(description="Output format: png, jpeg, or webp (omit to use the user's default)")
    ] = None,
    background: Annotated[
        str | None, Field(description="Background: auto, transparent, or opaque (omit to use the user's default)")
    ] = None,
    compression: Annotated[
        str | None, Field(description="Output compression 0-100 (jpeg/webp only; omit to use the user's default)")
    ] = None,
    n: Annotated[int, Field(description="Number of images to generate (1-4)")] = 1,
) -> str:
    """Generate an image from a text description using AI.

    Omit size/quality/output_format/background/compression to honor the user's
    per-session Image Output Options; pass an explicit value only when the request
    requires a specific one (it overrides the user's default).
    """
    # PRP-0066 / UDR-0041 D3: demo lane returns bundled placeholder PNGs.
    from app.demo import is_demo_mode

    if is_demo_mode():
        from app.demo.image_gen import demo_generate_image

        thread_id = current_thread_id.get("")
        if not thread_id:
            return json.dumps({"error": "No active session (thread_id not set)"})
        return await demo_generate_image(prompt=prompt, n=n, thread_id=thread_id)

    return await asyncio.to_thread(
        _generate_image_sync,
        prompt,
        size,
        quality,
        output_format,
        background,
        compression,
        n,
    )


async def edit_image(
    prompt: Annotated[str, Field(description="Description of the desired edit to the image")],
    image_filename: Annotated[
        str, Field(description="Filename of the source image in the session (e.g., photo.jpg, generated_abc123.png)")
    ],
    size: Annotated[
        str | None,
        Field(
            description="Output image size: auto, 1024x1024, 1024x1536, or 1536x1024 (omit to use the user's default)"
        ),
    ] = None,
    quality: Annotated[
        str | None, Field(description="Image quality: auto, low, medium, or high (omit to use the user's default)")
    ] = None,
    output_format: Annotated[
        str | None, Field(description="Output format: png, jpeg, or webp (omit to use the user's default)")
    ] = None,
    background: Annotated[
        str | None, Field(description="Background: auto, transparent, or opaque (omit to use the user's default)")
    ] = None,
    compression: Annotated[
        str | None, Field(description="Output compression 0-100 (jpeg/webp only; omit to use the user's default)")
    ] = None,
    n: Annotated[int, Field(description="Number of edited images to generate (1-4)")] = 1,
) -> str:
    """Edit an existing image based on a text description (full image edit, no mask).

    Omit size/quality/output_format/background/compression to honor the user's
    per-session Image Output Options; pass an explicit value only when needed.
    """
    # PRP-0066 / UDR-0041 D3: demo lane returns bundled placeholder PNG.
    from app.demo import is_demo_mode

    if is_demo_mode():
        from app.demo.image_gen import demo_edit_image

        thread_id = current_thread_id.get("")
        if not thread_id:
            return json.dumps({"error": "No active session (thread_id not set)"})
        return await demo_edit_image(
            prompt=prompt,
            n=n,
            thread_id=thread_id,
            image_filename=image_filename,
        )

    return await asyncio.to_thread(
        _edit_image_sync,
        prompt,
        image_filename,
        size,
        quality,
        output_format,
        background,
        compression,
        n,
    )
