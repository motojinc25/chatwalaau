"""Image generation tools for the main agent (CTR-0049, PRP-0027).

Provides generate_image and edit_image as MAF function tools.
Uses Azure OpenAI Images API with the deployment declared by the single ``image``
offering in the Model Offering Catalog (PRP-0114, UDR-0095 D1): the tools are only
registered when such an offering exists (or DEMO_MODE), so a non-demo call always
has one to resolve. Generated images are saved to the session upload directory
(.uploads/{thread_id}/generated_{uuid}.{ext}).

Image output options (size / quality / format / compression / background) follow a
precedence (PRP-0085 / PRP-0114, FEAT-0044, UDR-0095 D3, reordered in v0.117.6):
  state.image_options (an EXPLICIT per-session UI selection)
  > explicit LLM argument > offering.image_defaults > API default.
A field the user pinned in the control wins over the model's guess; a field left on
"Default" is not sent at all, so the model's argument applies there.
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
from app.core import provider_errors
from app.core.config import settings
from app.image_gen import capabilities

logger = logging.getLogger(__name__)

# Context variable for thread_id -- set by endpoint.py before agent.run()
current_thread_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_thread_id")

# Per-session image output options (PRP-0085, CTR-0120/CTR-0009). Set by the AG-UI
# endpoint before agent.run() from state.image_options; unset / empty means the
# user made no selection (fall through to settings / API defaults).
current_image_options: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "current_image_options", default=None
)

# The Images API default when no output_format is sent (and what a model falls back
# to when the option is dropped after a rejection).
DEFAULT_OUTPUT_FORMAT = "png"


def _option_help(option: str) -> str:
    """Tool-argument description for ``option``, generated from the offered surface.

    v0.117.6: these were hand-written and had gone stale -- they still advertised
    `webp` and `transparent` (withdrawn) and omitted the 2K / 4K sizes, so the model
    was being told to pass values the API now rejects. Generated from OPTION_VALUES so
    they cannot drift again.
    """
    allowed = ", ".join(capabilities.OPTION_VALUES[option])
    return (
        f"{option.capitalize()}: {allowed}. OMIT THIS unless the user asked for a "
        f"specific one in this request -- omitting it honors their saved preference."
    )


def _uses_v1_surface(api_version: str) -> bool:
    """True when ``api_version`` selects Azure's v1 API surface (v0.117.6).

    The rolling ``preview`` alias is the v1 lane; a dated version keeps the legacy
    deployment-scoped paths. Matched exactly rather than by substring, because
    "2025-04-01-preview" also contains "preview" and is NOT the v1 surface.
    """
    return api_version.strip().lower() == "preview"


def _resolve_option(arg: str | None, key: str, default_value: object) -> str:
    """Resolve one image output option by precedence (PRP-0114, UDR-0095 D3).

    v0.117.6 -- the ORDER CHANGED. It was::

        explicit LLM argument > state.image_options > offering.image_defaults > API default

    which meant a size the user had picked in the Image Output Options control was
    silently overridden whenever the model decided a different one suited the prompt.
    The user's report -- "I set 1024x1024 and it generated 1536x1024" -- is exactly
    that, and it looked intermittent because it depended on whether the model chose to
    pass the argument at all. It is now::

        state.image_options > explicit LLM argument > offering.image_defaults > API default

    The per-session block only ever contains fields the user EXPLICITLY chose (the SPA
    sends nothing for a field left on "Default"), so it is a deliberate instruction and
    outranks the model's guess. The OPERATOR tier (offering.image_defaults) stays BELOW
    the model argument: that one is a fallback, not a per-request choice.

    Trade-off, deliberately taken: while a field is pinned in the control, asking for a
    different value in chat will not change it -- clear the field to let the model
    choose again. Predictable beats occasionally-convenient here, because the pinned
    value is visible in the UI and the override was not.

    A blank / None value at any tier is treated as "unspecified" and falls through.
    ``default_value`` comes from models_catalog.image_output_defaults() and may be a
    str (size/quality/...) or an int (compression); it is coerced to str.
    """
    session = current_image_options.get() or {}
    session_value = session.get(key)
    if session_value:
        if arg and str(arg) != str(session_value):
            logger.info(
                "Image option %s: keeping the user's selection %r; ignoring the model's %r",
                key,
                str(session_value),
                arg,
            )
        return str(session_value)
    if arg:
        return arg
    if default_value is not None and str(default_value) != "":
        return str(default_value)
    return ""


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
        config.api_version
        if (config is not None and config.api_version)
        else models_catalog.DEFAULT_IMAGE_API_VERSION
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
    if fmt in capabilities.COMPRESSION_FORMATS:
        comp = _resolve_option(compression, "compression", defaults.get("compression"))
        if comp:
            with contextlib.suppress(TypeError, ValueError):
                params["output_compression"] = max(0, min(100, int(comp)))
    return params


def _validate_params(params: dict) -> list[str]:
    """Problems with the RESOLVED option values, or an empty list (v0.117.6).

    The resolved values used to go to the Images API unchecked, so a typo in an
    ``image_defaults`` block, a stale localStorage entry, or an LLM-invented argument
    became an opaque provider 400. Checked against the documented value surface only
    (CTR-0049); whether a given model honors a supported value is the separate,
    learned question handled by the retry path.
    """
    problems: list[str] = []
    for param, option in capabilities.PARAM_TO_OPTION.items():
        problem = capabilities.validate_option(option, str(params.get(param) or ""))
        if problem:
            problems.append(problem)
    if "output_compression" in params:
        problem = capabilities.validate_option("compression", str(params["output_compression"]))
        if problem:
            problems.append(problem)
    return problems


# Option keys whose value is sent under a different Images API parameter name.
_OPTION_TO_PARAM = {option: param for param, option in capabilities.PARAM_TO_OPTION.items()}


def _drop_unsupported_option(params: dict, option: str) -> str:
    """Remove the API parameter for ``option`` from ``params``; return what it held.

    Dropping the key (rather than substituting a value of our own) makes the retry
    fall back to whatever that model's own default is -- we do not know what a model
    we have just learned about accepts, so we must not pick for it. Compression goes
    with the format: it is only meaningful for jpeg / webp.
    """
    param = _OPTION_TO_PARAM.get(option, option)
    previous = str(params.pop(param, "") or "")
    if option == "format":
        params.pop("output_compression", None)
    return previous


def _call_with_capability_retry(call, params: dict, deployment: str, action: str) -> tuple[object, list[str]]:
    """Invoke the Images API, retrying once without an option the model rejected.

    v0.117.6. The output options are not uniformly supported across image models,
    and the SPA used to offer all of them unconditionally. When the API rejects one
    (HTTP 400 naming the parameter), the value is recorded as unsupported for this
    deployment -- so ``GET /api/model`` can stop offering it -- the option is dropped,
    and the call is retried ONCE. The user still gets their image, and the tool result
    carries a warning saying which preference could not be honored.

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
            f"This option is now disabled in the image options control."
        )
        return call(**params), warnings


def _image_error(action: str, exc: Exception) -> str:
    """Tool result for an image call that could not be completed.

    v0.117.6: the raw provider exception used to be interpolated straight into the
    result. A model reading "Error code: 400 - {...}" has no idea what to do, and in
    practice started hunting the filesystem with shell commands for an image that was
    never created. The message now states plainly that nothing was written and that
    there is no file to look for.
    """
    # v0.117.6: read through provider_errors. This used to look for
    # body["error"]["message"], but the OpenAI SDK unwraps that key, so the lookup
    # always missed and the operator got a bare "BadRequestError" with no reason.
    detail = provider_errors.error_message(exc) or type(exc).__name__
    return json.dumps(
        {
            "error": f"Image {action} failed: {detail}",
            "no_file_created": True,
            "guidance": (
                "No image file was written, so there is nothing to locate on disk. "
                "Do not search the filesystem or run shell commands. Report this "
                "message to the user and, if it names an unsupported option, suggest "
                "changing it in the image options control next to the message box."
            ),
        }
    )


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
    deployment = _image_deployment()

    # v0.117.6: reject a value the API cannot accept BEFORE calling out, so the
    # operator sees which option is wrong instead of an opaque provider 400.
    invalid = _validate_params(params)
    if invalid:
        logger.warning("Image %s called with invalid options: %s", "editing", "; ".join(invalid))
        return json.dumps(
            {
                "error": "Image editing failed: " + "; ".join(invalid),
                "no_file_created": True,
                "guidance": (
                    "No image file was written. Do not search the filesystem or run shell "
                    "commands. Tell the user which option is invalid so they can correct it "
                    "in the image options control next to the message box."
                ),
            }
        )

    # v0.117.6: reject a value the API cannot accept BEFORE calling out, so the
    # operator sees which option is wrong instead of an opaque provider 400.
    invalid = _validate_params(params)
    if invalid:
        logger.warning("Image %s called with invalid options: %s", "generation", "; ".join(invalid))
        return json.dumps(
            {
                "error": "Image generation failed: " + "; ".join(invalid),
                "no_file_created": True,
                "guidance": (
                    "No image file was written. Do not search the filesystem or run shell "
                    "commands. Tell the user which option is invalid so they can correct it "
                    "in the image options control next to the message box."
                ),
            }
        )

    logger.info(
        "Image generation: deployment=%s n=%d params=%s",
        deployment,
        n,
        params,
    )
    try:
        result, warnings = _call_with_capability_retry(
            lambda **kw: client.images.generate(model=deployment, prompt=prompt, n=n, **kw),
            params,
            deployment,
            "generation",
        )
    except Exception as exc:
        logger.exception("Image generation API error")
        return _image_error("generation", exc)

    # v0.117.6: the retry DROPS the rejected option, so `params` may no longer
    # carry output_format / size. Indexing them here raised KeyError('output_format')
    # AFTER a successful retry -- the image was generated and then thrown away. A
    # dropped format means the model used its own default, which is png.
    effective_format = params.get("output_format") or DEFAULT_OUTPUT_FORMAT
    # v0.117.6: report the parameters actually USED. Without this there was no way to
    # tell a display problem from a resolution problem when the result did not match
    # what the user had selected. `requested` is what we sent; `used` is what survived
    # a capability retry, which may have dropped an option.
    used = {option: params.get(param) for param, option in capabilities.PARAM_TO_OPTION.items()}
    used["format"] = effective_format
    if "output_compression" in params:
        used["compression"] = params["output_compression"]
    used = {k: v for k, v in used.items() if v not in (None, "")}
    images = []
    for item in result.data:
        b64 = item.b64_json
        if not b64:
            continue
        filename, uri = _save_image(thread_id, b64, effective_format)
        images.append(
            {
                "url": uri,
                "filename": filename,
                "revised_prompt": getattr(item, "revised_prompt", None) or prompt,
                "size": params.get("size") or "auto",
            }
        )

    payload: dict = {
        "images": images,
        "count": len(images),
        "tool": "generate_image",
        "parameters": used,
    }
    if warnings:
        payload["warnings"] = warnings
    return json.dumps(payload)


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
    deployment = _image_deployment()

    def _edit(**kw):
        # Reopened per attempt: the retry re-sends the body, and a consumed file
        # handle would upload zero bytes.
        with image_path.open("rb") as f:
            return client.images.edit(model=deployment, image=f, prompt=prompt, n=n, **kw)

    logger.info(
        "Image editing: deployment=%s source=%s n=%d params=%s",
        deployment,
        image_filename,
        n,
        params,
    )
    try:
        result, warnings = _call_with_capability_retry(_edit, params, deployment, "editing")
    except Exception as exc:
        logger.exception("Image edit API error")
        return _image_error("editing", exc)

    # v0.117.6: the retry DROPS the rejected option, so `params` may no longer
    # carry output_format / size. Indexing them here raised KeyError('output_format')
    # AFTER a successful retry -- the image was generated and then thrown away. A
    # dropped format means the model used its own default, which is png.
    effective_format = params.get("output_format") or DEFAULT_OUTPUT_FORMAT
    # v0.117.6: report the parameters actually USED. Without this there was no way to
    # tell a display problem from a resolution problem when the result did not match
    # what the user had selected. `requested` is what we sent; `used` is what survived
    # a capability retry, which may have dropped an option.
    used = {option: params.get(param) for param, option in capabilities.PARAM_TO_OPTION.items()}
    used["format"] = effective_format
    if "output_compression" in params:
        used["compression"] = params["output_compression"]
    used = {k: v for k, v in used.items() if v not in (None, "")}
    images = []
    for item in result.data:
        b64 = getattr(item, "b64_json", None)
        if not b64:
            continue
        filename, uri = _save_image(thread_id, b64, effective_format)
        images.append(
            {
                "url": uri,
                "filename": filename,
                "revised_prompt": getattr(item, "revised_prompt", None) or prompt,
                "size": params.get("size") or "auto",
            }
        )

    payload: dict = {
        "images": images,
        "count": len(images),
        "tool": "edit_image",
        "source_image": image_filename,
        "parameters": used,
    }
    if warnings:
        payload["warnings"] = warnings
    return json.dumps(payload)


# ---- Public async tool functions (registered on MAF agent) ----


async def generate_image(
    prompt: Annotated[str, Field(description="Detailed description of the image to generate")],
    size: Annotated[
        str | None,
        Field(description=_option_help("size")),
    ] = None,
    quality: Annotated[
        str | None, Field(description=_option_help("quality"))
    ] = None,
    output_format: Annotated[
        str | None, Field(description=_option_help("format"))
    ] = None,
    background: Annotated[
        str | None, Field(description=_option_help("background"))
    ] = None,
    compression: Annotated[
        str | None, Field(description="Output compression 0-100 (jpeg only). OMIT THIS unless the user asked for a specific one.")
    ] = None,
    n: Annotated[int, Field(description="How many images to produce from THIS prompt (1-4). To make several, call ONCE with n set -- do not call the tool repeatedly.")] = 1,
) -> str:
    """Generate an image from a text description using AI.

    Omit size/quality/output_format/background/compression to honor the user's
    per-session Image Output Options. A field the user has pinned in that control is
    used even if you pass a different value, so passing one has no effect there.

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
        str | None, Field(description=_option_help("quality"))
    ] = None,
    output_format: Annotated[
        str | None, Field(description=_option_help("format"))
    ] = None,
    background: Annotated[
        str | None, Field(description=_option_help("background"))
    ] = None,
    compression: Annotated[
        str | None, Field(description="Output compression 0-100 (jpeg only). OMIT THIS unless the user asked for a specific one.")
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
