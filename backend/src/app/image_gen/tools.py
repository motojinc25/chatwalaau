"""Image generation tools for the main agent (CTR-0049, PRP-0027; renamed by PRP-0187).

Provides ``image_generate`` and ``image_edit`` as MAF function tools (named category +
verb since PRP-0187 / UDR-0169 D6; they were ``generate_image`` / ``edit_image``).
Uses the Azure OpenAI Images API with the deployment declared by the single ``image``
offering in the Model Offering Catalog (PRP-0114, UDR-0095 D1): the tools are only
registered when such an offering exists (or DEMO_MODE), so a non-demo call always
has one to resolve. Generated images are saved to the session upload directory
(.uploads/{thread_id}/generated_{uuid}.png).

Image output options (size / quality / background) resolve by precedence
(PRP-0187, UDR-0169 D2/D4, superseding UDR-0167 D11/D12)::

    explicit LLM argument > offering.image_defaults > PRODUCT_DEFAULTS

An edit adds one tier between the argument and the catalog for SIZE: the first input
image's own shape (``normalize_size``), so editing a square image does not come back
16:9. Every call sends explicit values -- there is no "API default" tier any more --
and the output format is fixed to png (UDR-0169 D3), so ``output_format`` and
``compression`` are not tool arguments at all.

The Built-in agent image tier PRP-0185 introduced (``core_agent_image_*``, delivered
through a contextvar) is removed: the catalog is the ONE place image defaults live.

Tools execute in a thread pool via asyncio.to_thread() to prevent blocking the
FastAPI async event loop during API calls.
"""

import asyncio
import base64
import contextvars
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Annotated
import uuid

from openai import AzureOpenAI
from pydantic import Field

from app import models_catalog
from app.azure_credential import get_azure_openai_kwargs
from app.core import provider_errors
from app.core.config import settings
from app.image_gen import capabilities
from app.image_gen.imageinfo import read_image_info
from app.image_gen.names import IMAGE_EDIT_TOOL, IMAGE_GENERATE_TOOL

logger = logging.getLogger(__name__)

# Context variable for thread_id -- set by endpoint.py before agent.run()
current_thread_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_thread_id")

# The Images API default when a retry has dropped options. Output is always png.
DEFAULT_OUTPUT_FORMAT = capabilities.OUTPUT_FORMAT


def _option_help(option: str) -> str:
    """Tool-argument description for ``option``, generated from the offered surface.

    Generated rather than hand-written so it cannot drift from OPTION_VALUES
    (v0.117.6 (k)). Every description tells the model to OMIT the argument unless the
    user asked, because under "LLM argument > catalog" an unrequested argument
    overrides the operator's default (UDR-0169 D4).
    """
    omit = (
        "OMIT THIS unless the user asked for a specific one in this request -- omitting it uses the configured default."
    )
    if option == "size":
        presets = ", ".join(capabilities.SIZE_PRESETS)
        return (
            f"Size as WIDTHxHEIGHT (e.g. {presets}). Both edges multiples of 16, aspect ratio "
            f"1:3 to 3:1, longest edge <= 3840, above 2560x1440 is experimental. {omit}"
        )
    allowed = ", ".join(capabilities.OPTION_VALUES[option])
    extra = " xhigh and max take noticeably longer and cost more." if option == "quality" else ""
    return f"{option.capitalize()}: {allowed}.{extra} {omit}"


def _uses_v1_surface(api_version: str) -> bool:
    """True when ``api_version`` selects Azure's v1 API surface (v0.117.6).

    The rolling ``preview`` alias is the v1 lane; a dated version keeps the legacy
    deployment-scoped paths. Matched exactly rather than by substring, because
    "2025-04-01-preview" also contains "preview" and is NOT the v1 surface.
    """
    return api_version.strip().lower() == "preview"


def _stored_default(value: object) -> str:
    """A tier value from STORED configuration, or "" when it is unset.

    ``auto`` was the old "let the API decide" sentinel; UDR-0169 D2 removes it, and a
    stored ``auto`` (an older catalog file) is read as unset rather than an error.
    """
    text = str(value).strip() if value is not None else ""
    return "" if text.lower() == "auto" else text


def _resolve_option(arg: str | None, key: str, *fallbacks: object) -> str:
    """Resolve one image output option by precedence (UDR-0169 D4).

    ``arg`` is the explicit LLM argument and always wins when given -- an invalid one
    is reported by validation, never silently replaced. The fallbacks are tried in
    order (for an edit's size: the source-derived size, then the catalog), and the
    product default closes the chain, so the result is never empty.
    """
    if arg is not None and str(arg).strip():
        return str(arg).strip()
    for fallback in fallbacks:
        value = _stored_default(fallback)
        if value:
            return value
    return capabilities.PRODUCT_DEFAULTS[key]


def _get_client():
    """Build the Images API client for the configured image offering.

    Credential resolution centralised in app.azure_credential (PRP-0058, UDR-0034).
    Model Offering Catalog (PRP-0114, UDR-0095 D1): the single ``image`` offering
    supplies the deployment / endpoint / api_version / api_key (a ``base_url``
    offering builds a plain OpenAI client). The offering may omit ``endpoint``
    (falls back to the shared AZURE_OPENAI_ENDPOINT) and ``api_version`` (falls back
    to DEFAULT_IMAGE_API_VERSION). Image generation stays on the dedicated Images
    API (UDR-0045 D7).

    v0.117.6 -- the AZURE SURFACE IS CHOSEN BY api_version:

    * ``preview`` (the rolling alias, now the default) selects the **v1 API**:
      ``{endpoint}/openai/v1/images/generations?api-version=preview``, with the
      deployment passed in the BODY as ``model``. It is built on the PLAIN ``OpenAI``
      client, because ``AzureOpenAI`` unconditionally rewrites ``/images/generations``
      into ``/deployments/{model}/images/generations``
      (``openai/lib/azure.py`` ``_build_request``), which does not exist on the v1
      surface and returned ``404 Resource Not Found``. Setting ``base_url`` on
      ``AzureOpenAI`` does NOT avoid that rewrite -- only the plain client does.
    * A DATED api_version (an offering pinning e.g. ``2025-04-01-preview``) keeps the
      legacy deployment-scoped surface via ``AzureOpenAI``, unchanged.

    Not cached: the client is rebuilt per call. An Entra token has a lifetime, and a
    cached client would also survive a catalog edit that changes the endpoint or the
    deployment. Image calls take seconds, so construction cost is irrelevant next to
    the request itself.
    """
    config = models_catalog.image_config()
    if config is not None and config.base_url:
        from openai import OpenAI

        return OpenAI(api_key=config.api_key or settings.openai_api_key or "", base_url=config.base_url)

    endpoint = config.endpoint if (config is not None and config.endpoint) else settings.azure_openai_endpoint
    api_version = (
        config.api_version if (config is not None and config.api_version) else models_catalog.DEFAULT_IMAGE_API_VERSION
    )
    cred_kwargs = {"api_key": config.api_key} if (config is not None and config.api_key) else get_azure_openai_kwargs()

    if _uses_v1_surface(api_version):
        from openai import OpenAI

        # The v1 surface authenticates with a plain bearer token, so an Entra token
        # provider is resolved HERE (per call, hence always fresh) and handed over as
        # the api_key -- which the SDK sends as `Authorization: Bearer <token>`.
        token = cred_kwargs.get("api_key")
        if not token:
            provider = cred_kwargs.get("azure_ad_token_provider")
            token = provider() if callable(provider) else ""
        return OpenAI(
            api_key=token or "",
            base_url=f"{str(endpoint).rstrip('/')}/openai/v1",
            default_query={"api-version": api_version},
        )

    return AzureOpenAI(azure_endpoint=endpoint, api_version=api_version, **cred_kwargs)


def _image_deployment() -> str:
    """Resolve the image deployment name from the catalog image offering (PRP-0114).

    Returns "" when no image offering is configured; in a non-demo deployment the
    tools are not registered in that case (CTR-0050), so this is defensive.
    """
    config = models_catalog.image_config()
    return config.deployment if config is not None else ""


def resolve_image_params(
    size: str | None,
    quality: str | None,
    background: str | None,
    *,
    source_size: str | None = None,
) -> dict:
    """Resolve the effective Images API parameters (UDR-0169 D2/D4).

    Returns explicit ``size`` / ``quality`` / ``background`` plus the fixed
    ``output_format="png"``. ``source_size`` is the edit tier: the first input image's
    shape, already normalized to a rule-valid size.
    """
    defaults = models_catalog.image_output_defaults()
    return {
        "size": _resolve_option(size, "size", source_size, defaults.get("size")),
        "quality": _resolve_option(quality, "quality", defaults.get("quality")),
        "background": _resolve_option(background, "background", defaults.get("background")),
        "output_format": capabilities.OUTPUT_FORMAT,
    }


def validate_params(params: dict) -> list[str]:
    """Problems with the RESOLVED option values, or an empty list (v0.117.6 (e)).

    Checked against the documented value surface only; whether a given model honors a
    supported value is the separate, learned question handled by the retry path.
    """
    problems: list[str] = []
    for param, option in capabilities.PARAM_TO_OPTION.items():
        problem = capabilities.validate_option(option, str(params.get(param) or ""))
        if problem:
            problems.append(problem)
    return problems


def _drop_unsupported_option(params: dict, option: str) -> str:
    """Remove the API parameter for ``option`` from ``params``; return what it held.

    Dropping the key (rather than substituting a value of our own) makes the retry
    fall back to whatever that model's own default is -- we do not know what a model
    we have just learned about accepts, so we must not pick for it.
    """
    param = {option_key: p for p, option_key in capabilities.PARAM_TO_OPTION.items()}.get(option, option)
    return str(params.pop(param, "") or "")


def call_with_capability_retry(call, params: dict, deployment: str, action: str) -> tuple[object, list[str]]:
    """Invoke the Images API, retrying once without an option the model rejected.

    v0.117.6. When the API rejects one of our options (HTTP 400 naming the parameter),
    the value is recorded as unsupported for this deployment -- so ``GET /api/model``
    can stop offering it -- the option is dropped, and the call is retried ONCE. The
    user still gets their image, and the result carries a warning saying which
    preference could not be honored.

    Returns ``(result, warnings)``. Raises the original error when the rejection is
    not about one of our options, or when the retry fails too.
    """
    warnings: list[str] = []
    try:
        return call(**params), warnings
    except Exception as exc:
        rejection = capabilities.rejected_option(exc)
        if rejection is None:
            raise
        option, provider_message = rejection
        dropped = _drop_unsupported_option(params, option)
        capabilities.record_unsupported(deployment, option, dropped)
        logger.warning(
            "Image %s rejected %s=%r for deployment %r; retrying without it. Provider said: %s",
            action,
            option,
            dropped,
            deployment,
            provider_message,
        )
        warnings.append(
            f"The image model does not support {option}={dropped!r}"
            f"{f' ({provider_message})' if provider_message else ''}. "
            f"The image was produced with that model's default {option} instead. "
            f"This option is now disabled in the catalog image settings."
        )
        return call(**params), warnings


def image_error(action: str, exc: Exception) -> str:
    """Tool result for an image call that could not be completed.

    v0.117.6: the raw provider exception used to be interpolated straight into the
    result, and a model reading "Error code: 400 - {...}" started hunting the
    filesystem for an image that was never created. The message states plainly that
    nothing was written and that there is no file to look for.
    """
    detail = provider_errors.error_message(exc) or type(exc).__name__
    return json.dumps(
        {
            "error": f"Image {action} failed: {detail}",
            "no_file_created": True,
            "guidance": (
                "No image file was written, so there is nothing to locate on disk. "
                "Do not search the filesystem or run shell commands. Report this "
                "message to the user."
            ),
        }
    )


def _invalid_result(action: str, problems: list[str]) -> str:
    """Tool result for a call rejected before it reached the provider."""
    logger.warning("Image %s called with invalid options: %s", action, "; ".join(problems))
    return json.dumps(
        {
            "error": f"Image {action} failed: " + "; ".join(problems),
            "no_file_created": True,
            "guidance": (
                "No image file was written. Do not search the filesystem or run shell "
                "commands. Correct the argument (or omit it to use the configured default) "
                "and try again, or tell the user which value is invalid."
            ),
        }
    )


# PRP-0177 / UDR-0159 D3: the success path says delivery is DONE, mirroring the failure
# path's guidance above. The image is written under UPLOAD_DIR and rendered by the chat
# (CTR-0051), so copying it into the coding workspace and linking it there (which the
# CTR-0032 tool-guide's file rule used to invite) is wasted work.
DELIVERED_GUIDANCE = (
    "The image is already saved and shown to the user in the chat. Do not copy it into the "
    "workspace, do not offer a download link, and do not run shell commands for it."
)


def _save_image(thread_id: str, image_b64: str) -> tuple[str, str]:
    """Decode base64 image data and save it as png in the upload directory.

    Returns (filename, uri) tuple. Output is always png (UDR-0169 D3).
    """
    filename = f"generated_{uuid.uuid4().hex[:12]}.{capabilities.OUTPUT_FORMAT}"
    save_dir = Path(settings.upload_dir) / thread_id
    save_dir.mkdir(parents=True, exist_ok=True)
    file_path = save_dir / filename
    file_path.write_bytes(base64.b64decode(image_b64))
    uri = f"/api/uploads/{thread_id}/{filename}"
    logger.info("Saved generated image: %s", uri)
    return filename, uri


def _used_parameters(params: dict) -> dict:
    """The options actually USED (v0.117.6 (i)): what survived a capability retry."""
    used = {option: params.get(param) for param, option in capabilities.PARAM_TO_OPTION.items()}
    used["format"] = capabilities.OUTPUT_FORMAT
    return {k: v for k, v in used.items() if v not in (None, "")}


def _collect_images(thread_id: str, result: object, params: dict, fallback_prompt: str) -> list[dict]:
    images = []
    for item in getattr(result, "data", None) or []:
        b64 = getattr(item, "b64_json", None)
        if not b64:
            continue
        filename, uri = _save_image(thread_id, b64)
        images.append(
            {
                "url": uri,
                "filename": filename,
                "revised_prompt": getattr(item, "revised_prompt", None) or fallback_prompt,
                "size": params.get("size") or "",
            }
        )
    return images


def clamp_n(n: int | None) -> int:
    """Images per request, clamped into the documented 1-10 (PRP-0187 Q3)."""
    try:
        value = int(n or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(value, capabilities.MAX_IMAGES_PER_REQUEST))


def _generate_image_sync(
    prompt: str,
    size: str | None,
    quality: str | None,
    background: str | None,
    n: int,
) -> str:
    """Synchronous image generation implementation."""
    thread_id = current_thread_id.get("")
    if not thread_id:
        return json.dumps({"error": "No active session (thread_id not set)"})

    n = clamp_n(n)
    params = resolve_image_params(size, quality, background)
    # v0.117.6 / PRP-0187 F1: reject a value the API cannot accept BEFORE calling out.
    invalid = validate_params(params)
    if invalid:
        return _invalid_result("generation", invalid)

    client = _get_client()
    deployment = _image_deployment()
    logger.info("Image generation: deployment=%s n=%d params=%s", deployment, n, params)
    try:
        result, warnings = call_with_capability_retry(
            lambda **kw: client.images.generate(model=deployment, prompt=prompt, n=n, **kw),
            params,
            deployment,
            "generation",
        )
    except Exception as exc:
        logger.exception("Image generation API error")
        return image_error("generation", exc)

    images = _collect_images(thread_id, result, params, prompt)
    payload: dict = {
        "images": images,
        "count": len(images),
        "tool": IMAGE_GENERATE_TOOL,
        "parameters": _used_parameters(params),
        "guidance": DELIVERED_GUIDANCE,
    }
    if warnings:
        payload["warnings"] = warnings
    return json.dumps(payload)


@dataclass(frozen=True)
class EditInputs:
    """Resolved files for one edit: ordered inputs plus an optional mask."""

    images: list[Path]
    mask: Path | None
    source_size: str  # the first input's shape, normalized (UDR-0169 D4)


class EditInputError(ValueError):
    """An edit input is missing, unreadable, or breaks an Images API requirement."""


def _session_file(thread_id: str, name: str) -> Path:
    """Resolve a single-segment filename inside the session upload directory."""
    if not name or Path(name).name != name or name.startswith("."):
        raise EditInputError(f"Invalid file name: {name!r}")
    path = Path(settings.upload_dir) / thread_id / name
    if not path.is_file():
        raise EditInputError(f"Image not found: {name}")
    return path


def resolve_edit_inputs(thread_id: str, image_filenames: list[str], mask_filename: str | None) -> EditInputs:
    """Validate and resolve the files of an edit (UDR-0169 D7/D9).

    Every input must be PNG or JPEG under 50 MB; there may be 1-16 of them; the mask
    (if any) must be a PNG with an alpha channel and the FIRST image's dimensions --
    the documented requirements, checked here so a violation is a clear message
    rather than a provider 400.
    """
    if not image_filenames:
        raise EditInputError("At least one input image is required.")
    if len(image_filenames) > capabilities.MAX_INPUT_IMAGES:
        raise EditInputError(f"At most {capabilities.MAX_INPUT_IMAGES} input images are allowed.")
    paths = [_session_file(thread_id, name) for name in image_filenames]
    first_info = None
    for index, path in enumerate(paths):
        if path.stat().st_size >= capabilities.MAX_INPUT_BYTES:
            raise EditInputError(f"{path.name} is 50 MB or larger.")
        info = read_image_info(path.read_bytes())
        if info is None or info.format not in ("png", "jpeg"):
            raise EditInputError(f"{path.name} must be a PNG or JPEG image.")
        if index == 0:
            first_info = info
    assert first_info is not None
    mask_path = None
    if mask_filename:
        mask_path = _session_file(thread_id, mask_filename)
        mask_info = read_image_info(mask_path.read_bytes())
        if mask_info is None or mask_info.format != "png":
            raise EditInputError("The mask must be a PNG file.")
        if not mask_info.has_alpha:
            raise EditInputError("The mask must have an alpha channel (transparent pixels mark the edit area).")
        if (mask_info.width, mask_info.height) != (first_info.width, first_info.height):
            raise EditInputError(
                f"The mask is {mask_info.width}x{mask_info.height} but the image being edited is "
                f"{first_info.width}x{first_info.height}; they must match."
            )
    source_size = capabilities.normalize_size(first_info.width, first_info.height, stable=True)
    return EditInputs(images=paths, mask=mask_path, source_size=source_size)


def run_edit(
    thread_id: str,
    inputs: EditInputs,
    prompt: str,
    params: dict,
    n: int,
) -> tuple[dict | None, str | None]:
    """Call the Images edit API and save the results.

    Shared by the ``image_edit`` tool and the editor's direct endpoint (CTR-0053 v2,
    UDR-0169 D8). Returns ``(payload, None)`` on success or ``(None, error_json)``.
    """
    client = _get_client()
    deployment = _image_deployment()

    def _edit(**kw):
        # Files are reopened per attempt: the retry re-sends the body, and a consumed
        # handle would upload zero bytes.
        handles = [path.open("rb") for path in inputs.images]
        mask_handle = inputs.mask.open("rb") if inputs.mask else None
        try:
            extra = {"mask": mask_handle} if mask_handle else {}
            image_arg = handles if len(handles) > 1 else handles[0]
            return client.images.edit(model=deployment, image=image_arg, prompt=prompt, n=n, **extra, **kw)
        finally:
            for handle in handles:
                handle.close()
            if mask_handle:
                mask_handle.close()

    logger.info(
        "Image editing: deployment=%s inputs=%s mask=%s n=%d params=%s",
        deployment,
        [p.name for p in inputs.images],
        inputs.mask.name if inputs.mask else None,
        n,
        params,
    )
    try:
        result, warnings = call_with_capability_retry(_edit, params, deployment, "editing")
    except Exception as exc:
        logger.exception("Image edit API error")
        return None, image_error("editing", exc)

    images = _collect_images(thread_id, result, params, prompt)
    payload: dict = {
        "images": images,
        "count": len(images),
        "tool": IMAGE_EDIT_TOOL,
        "inputs": [p.name for p in inputs.images],
        "parameters": _used_parameters(params),
        "guidance": DELIVERED_GUIDANCE,
    }
    if inputs.mask:
        payload["mask"] = inputs.mask.name
    if warnings:
        payload["warnings"] = warnings
    return payload, None


def _edit_image_sync(
    prompt: str,
    image_filenames: list[str],
    mask_filename: str | None,
    size: str | None,
    quality: str | None,
    background: str | None,
    n: int,
) -> str:
    """Synchronous image editing implementation (the chat-path tool)."""
    thread_id = current_thread_id.get("")
    if not thread_id:
        return json.dumps({"error": "No active session (thread_id not set)"})

    try:
        inputs = resolve_edit_inputs(thread_id, list(image_filenames or []), mask_filename)
    except EditInputError as exc:
        session_dir = Path(settings.upload_dir) / thread_id
        available = sorted(f.name for f in session_dir.iterdir() if f.is_file()) if session_dir.is_dir() else []
        return json.dumps({"error": str(exc), "no_file_created": True, "available_files": available})

    params = resolve_image_params(size, quality, background, source_size=inputs.source_size)
    # PRP-0187 F2: the edit path validates too.
    invalid = validate_params(params)
    if invalid:
        return _invalid_result("editing", invalid)

    payload, error = run_edit(thread_id, inputs, prompt, params, clamp_n(n))
    return error if error is not None else json.dumps(payload)


# ---- Public async tool functions (registered on MAF agent) ----


async def image_generate(
    prompt: Annotated[str, Field(description="Detailed description of the image to generate")],
    size: Annotated[str | None, Field(description=_option_help("size"))] = None,
    quality: Annotated[str | None, Field(description=_option_help("quality"))] = None,
    background: Annotated[str | None, Field(description=_option_help("background"))] = None,
    n: Annotated[
        int,
        Field(
            description=(
                "How many images to produce from THIS prompt (1-10). To make several, call ONCE "
                "with n set -- do not call the tool repeatedly."
            )
        ),
    ] = 1,
) -> str:
    """Generate an image from a text description using AI. Output is always PNG.

    Omit size/quality/background to use the configured defaults; pass one only when the
    user asked for a specific value.

    For SEVERAL images of the same subject, make ONE call with n=<count>. Calling this
    tool repeatedly produces separate requests, is slower, and shows the user one
    indicator per call.
    """
    # PRP-0066 / UDR-0041 D3: demo lane returns bundled placeholder PNGs.
    from app.demo import is_demo_mode

    if is_demo_mode():
        from app.demo.image_gen import demo_generate_image

        thread_id = current_thread_id.get("")
        if not thread_id:
            return json.dumps({"error": "No active session (thread_id not set)"})
        return await demo_generate_image(prompt=prompt, n=clamp_n(n), thread_id=thread_id)

    return await asyncio.to_thread(_generate_image_sync, prompt, size, quality, background, n)


async def image_edit(
    prompt: Annotated[
        str,
        Field(
            description=(
                "The edit, written as two labelled parts: 'Change:' (what must be different) and "
                "'Preserve:' (what must stay exactly as it is). Example: 'Change: the background to "
                "a beach at sunset. Preserve: the product's shape, logo and colors.'"
            )
        ),
    ],
    image_filenames: Annotated[
        list[str],
        Field(
            description=(
                "Session image filenames (1-16, e.g. photo.jpg, generated_abc123.png). The FIRST is "
                "the image being edited; any others are references. PNG or JPEG only."
            )
        ),
    ],
    mask_filename: Annotated[
        str | None,
        Field(
            description=(
                "Optional PNG mask with the SAME size as the first image; its fully transparent "
                "pixels mark the area that may change. Omit for a whole-image edit."
            )
        ),
    ] = None,
    size: Annotated[
        str | None,
        Field(description=_option_help("size") + " By default an edit keeps the first image's shape."),
    ] = None,
    quality: Annotated[str | None, Field(description=_option_help("quality"))] = None,
    background: Annotated[str | None, Field(description=_option_help("background"))] = None,
    n: Annotated[int, Field(description="Number of edited images to produce (1-10).")] = 1,
) -> str:
    """Edit an existing image, optionally using reference images and a mask. Output is PNG.

    Write the prompt as 'Change: ... Preserve: ...'. Omit size/quality/background to
    use the defaults (an edit keeps its source's shape).
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
            n=clamp_n(n),
            thread_id=thread_id,
            image_filenames=list(image_filenames or []),
        )

    return await asyncio.to_thread(
        _edit_image_sync,
        prompt,
        list(image_filenames or []),
        mask_filename,
        size,
        quality,
        background,
        n,
    )
