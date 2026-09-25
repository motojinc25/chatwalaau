"""Image output option surface and learned capabilities (v0.117.6; surface PRP-0187).

CTR-0049 / CTR-0069. The image output options -- size / quality / background -- are
NOT uniformly supported across image models. The Images API rejects an unsupported
one with an HTTP 400 naming the parameter::

    {'message': 'Transparent background is not supported for this model.',
     'type': 'image_generation_user_error', 'param': 'background', ...}

Capabilities are LEARNED FROM THE PROVIDER rather than guessed. There is no
hard-coded table of which model supports what: such a table is wrong the moment a
new image model ships, and this project cannot verify every deployment. Instead the
first rejection of a value is recorded here, the call is retried without it, and
``GET /api/model`` reports the value as unsupported so the SPA disables it from
then on.

The registry is process-local and NOT persisted: it is a cache of observations
about the currently configured deployment, and a restart (or a catalog change that
swaps the image model) must start from a clean slate rather than carry a stale
restriction forward. Keyed by deployment so switching the image offering does not
inherit the previous model's limits.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.core import provider_errors

logger = logging.getLogger(__name__)

# The full option surface advertised to the SPA. Single source of truth: the catalog
# card renders these and the backend validates against them, so a value cannot exist
# in one and not the other.
#
# PRP-0187 / UDR-0169 D1/D2/D3/D5, per the GPT-Image-2.5-Sunburst specification
# (Azure AI Foundry "How to use image generation models", read 2026-09-22):
#  - `auto` is gone from every option. Every call sends an explicit value, so the UI
#    can show `Default (<value>)` truthfully instead of an unknowable "API default";
#  - quality gains `xhigh` and `max`;
#  - `transparent` returns to background. The documentation still calls transparency
#    "GPT-image-1 only", so whether a deployment honors it stays a LEARNED fact (the
#    path below), never assumed in either direction;
#  - size is a RULE (validate_size), not a list -- SIZE_PRESETS is only what the UI
#    offers;
#  - the output format is FIXED to png: it is no longer an option at all, so neither
#    the catalog nor the model can ask for another one, and compression (jpeg only)
#    is gone with it.
OPTION_VALUES: dict[str, tuple[str, ...]] = {
    "quality": ("low", "medium", "high", "xhigh", "max"),
    "background": ("opaque", "transparent"),
}

# The option keys a caller may set (size is rule-validated, the others enum-validated).
OPTION_KEYS: tuple[str, ...] = ("size", "quality", "background")

# What the catalog card offers for size. A hand-authored catalog value may be any
# rule-valid size; the card shows it as "Custom (WxH)" rather than dropping it.
SIZE_PRESETS: tuple[str, ...] = ("2048x1152", "1920x1440", "1024x1024")

# The one output format (UDR-0169 D3). Sent on every call; every saved file is .png.
OUTPUT_FORMAT = "png"

# Product defaults: the bottom precedence tier, always SENT (UDR-0169 D2).
PRODUCT_DEFAULTS: dict[str, str] = {
    "size": "2048x1152",
    "quality": "xhigh",
    "background": "opaque",
    "format": OUTPUT_FORMAT,
}

# The documented size rule. Resolutions above 2560x1440 are "experimental": accepted,
# never a default or a preset, and described as such to the model.
SIZE_MULTIPLE = 16
SIZE_MAX_EDGE = 3840
SIZE_MAX_RATIO = 3
SIZE_MIN_PIXELS = 655_360
SIZE_MAX_PIXELS = 8_294_400
SIZE_EXPERIMENTAL_ABOVE = (2560, 1440)

SIZE_RULE: dict[str, int] = {
    "multiple": SIZE_MULTIPLE,
    "max_edge": SIZE_MAX_EDGE,
    "max_ratio": SIZE_MAX_RATIO,
    "min_pixels": SIZE_MIN_PIXELS,
    "max_pixels": SIZE_MAX_PIXELS,
}

# Edit inputs (documented): up to 16 images; the editor lays them out as the source,
# an optional annotated copy and up to 14 references (UDR-0169 D9).
MAX_INPUT_IMAGES = 16
MAX_REFERENCE_IMAGES = 14
# Images per request. The API documents 1-10.
MAX_IMAGES_PER_REQUEST = 10
# Per-input size cap (documented "less than 50 MB").
MAX_INPUT_BYTES = 50 * 1024 * 1024


def parse_size(value: str) -> tuple[int, int] | None:
    """``"WxH"`` -> ``(W, H)``, or None when it is not two positive integers."""
    if not isinstance(value, str):
        return None
    parts = value.lower().strip().split("x")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    width, height = int(parts[0]), int(parts[1])
    if width <= 0 or height <= 0:
        return None
    return width, height


def size_problem(width: int, height: int) -> str | None:
    """Why ``width x height`` breaks the size rule, or None when it is valid."""
    if width % SIZE_MULTIPLE or height % SIZE_MULTIPLE:
        return f"both edges must be multiples of {SIZE_MULTIPLE} px"
    if max(width, height) > SIZE_MAX_EDGE:
        return f"neither edge may exceed {SIZE_MAX_EDGE} px"
    if max(width, height) > SIZE_MAX_RATIO * min(width, height):
        return f"the aspect ratio must be between 1:{SIZE_MAX_RATIO} and {SIZE_MAX_RATIO}:1"
    pixels = width * height
    if not (SIZE_MIN_PIXELS <= pixels <= SIZE_MAX_PIXELS):
        return f"total pixels must be between {SIZE_MIN_PIXELS:,} and {SIZE_MAX_PIXELS:,}"
    return None


def validate_size(value: str) -> str | None:
    """Return a human-readable problem with a size string, or None when it is valid."""
    parsed = parse_size(value)
    if parsed is None:
        return f"size must be WIDTHxHEIGHT (for example {PRODUCT_DEFAULTS['size']}), got {value!r}"
    problem = size_problem(*parsed)
    return f"size {value!r} is invalid: {problem}" if problem else None


def normalize_size(width: int, height: int, *, stable: bool = False) -> str:
    """The nearest rule-valid size with (about) the same shape as ``width x height``.

    Used to derive an edit's output size from its source (UDR-0169 D4) and to resample
    a non-conforming source before a mask is drawn on it (D14). The aspect ratio is
    clamped into 1:3..3:1, the pixel count into the documented window and the long
    edge to 3,840 px; both edges are rounded to multiples of 16, and a short fix-up
    loop walks the rounded result back inside the rule.

    ``stable=True`` additionally caps the pixel count at 2560x1440, the documented
    non-experimental ceiling. A size DERIVED from a source image uses it, so a large
    phone photo does not silently become an experimental (slow, costly) request.
    """
    if width <= 0 or height <= 0:
        return PRODUCT_DEFAULTS["size"]
    ceiling = SIZE_EXPERIMENTAL_ABOVE[0] * SIZE_EXPERIMENTAL_ABOVE[1] if stable else SIZE_MAX_PIXELS
    ratio = min(max(width / height, 1 / SIZE_MAX_RATIO), float(SIZE_MAX_RATIO))
    area = min(max(width * height, SIZE_MIN_PIXELS), ceiling)
    fw = (area * ratio) ** 0.5
    fh = fw / ratio
    scale = min(1.0, SIZE_MAX_EDGE / max(fw, fh))
    fw, fh = fw * scale, fh * scale
    w = max(SIZE_MULTIPLE, round(fw / SIZE_MULTIPLE) * SIZE_MULTIPLE)
    h = max(SIZE_MULTIPLE, round(fh / SIZE_MULTIPLE) * SIZE_MULTIPLE)
    for _ in range(2000):
        if size_problem(w, h) is None and w * h <= ceiling:
            break
        if w > SIZE_MAX_RATIO * h:
            h += SIZE_MULTIPLE
        elif h > SIZE_MAX_RATIO * w:
            w += SIZE_MULTIPLE
        elif w * h > ceiling or max(w, h) > SIZE_MAX_EDGE:
            if w >= h:
                w -= SIZE_MULTIPLE
            else:
                h -= SIZE_MULTIPLE
        elif w / h < ratio:
            w += SIZE_MULTIPLE
        else:
            h += SIZE_MULTIPLE
    return f"{w}x{h}"


def is_experimental_size(value: str) -> bool:
    """True above 2560x1440 (by pixel count), which the documentation calls experimental."""
    parsed = parse_size(value)
    if parsed is None:
        return False
    return parsed[0] * parsed[1] > SIZE_EXPERIMENTAL_ABOVE[0] * SIZE_EXPERIMENTAL_ABOVE[1]


def validate_option(option: str, value: str) -> str | None:
    """Return a human-readable problem with ``option=value``, or None when it is fine.

    Validates the VALUE SURFACE only -- whether a given model honors a supported value
    is a separate, learned question (``unsupported_for``).
    """
    if not value:
        return None
    if option == "size":
        return validate_size(value)
    allowed = OPTION_VALUES.get(option)
    if allowed is None:
        return f"unknown image option {option!r}"
    if value not in allowed:
        return f"{option} must be one of {', '.join(allowed)}; got {value!r}"
    return None


# Images API parameter name -> the option key used by the SPA / catalog defaults.
# output_format is not here: it is fixed to png and never dropped (UDR-0169 D3).
PARAM_TO_OPTION: dict[str, str] = {
    "size": "size",
    "quality": "quality",
    "background": "background",
}

# Error codes/types the Images API uses for "this model will not accept that value".
# A rejection of this class is a CAPABILITY fact; any other 400 is not recorded.
_UNSUPPORTED_ERROR_TYPES = frozenset({"image_generation_user_error", "invalid_request_error"})
_UNSUPPORTED_ERROR_CODES = frozenset({"invalid_value", "unsupported_value", "unsupported_parameter"})

# deployment -> {option key: {rejected value, ...}}
_unsupported: dict[str, dict[str, set[str]]] = {}
_lock = threading.Lock()


def reset(deployment: str | None = None) -> None:
    """Forget learned restrictions -- for one deployment, or all of them."""
    with _lock:
        if deployment is None:
            _unsupported.clear()
        else:
            _unsupported.pop(deployment, None)


def record_unsupported(deployment: str, option: str, value: str) -> bool:
    """Record that ``deployment`` rejected ``value`` for ``option``.

    Returns True when this is new information (the caller then knows a retry
    without the option is worth attempting and the capability map changed).
    """
    if option not in OPTION_KEYS or not value:
        return False
    with _lock:
        seen = _unsupported.setdefault(deployment, {}).setdefault(option, set())
        if value in seen:
            return False
        seen.add(value)
    logger.info(
        "Image model %r does not support %s=%r; it will be offered as unsupported from now on.",
        deployment,
        option,
        value,
    )
    return True


def unsupported_for(deployment: str) -> dict[str, list[str]]:
    """Learned unsupported values for ``deployment`` (option -> sorted values)."""
    with _lock:
        found = _unsupported.get(deployment) or {}
        return {option: sorted(values) for option, values in found.items() if values}


def rejected_option(error: Any) -> tuple[str, str] | None:
    """Map a provider error to ``(option key, provider message)``, or None.

    Only a rejection that names one of OUR options is treated as a capability fact --
    an unrelated 400 (content policy, bad prompt, quota) must never silently disable a
    UI option.

    The rejected VALUE is not read from the message: the caller knows exactly what it
    sent for that parameter, and parsing it out of prose would be guesswork. The error
    fields are read through ``app.core.provider_errors`` (the SDK unwraps the wire
    ``error`` key before constructing the exception, v0.117.6).
    """
    if not isinstance(error, BaseException):
        return None
    if (
        provider_errors.error_type(error) not in _UNSUPPORTED_ERROR_TYPES
        and provider_errors.error_code(error) not in _UNSUPPORTED_ERROR_CODES
    ):
        return None
    option = PARAM_TO_OPTION.get(provider_errors.error_param(error) or "")
    if option is None:
        return None
    return option, provider_errors.error_message(error) or ""


def capability_map(deployment: str) -> dict[str, Any]:
    """Capability view for ``GET /api/model`` (CTR-0069) and the catalog image card.

    ``values`` is the offered surface (``size`` = the presets); ``unsupported`` lists
    what this deployment has been observed to reject. Everything not listed is offered
    -- an option is never hidden on a guess, only on an observation.

    PRP-0187 (UDR-0169 D2/D5): ``size_rule`` is the rule any size must satisfy and
    ``defaults`` the product defaults the card renders as ``Default (<value>)``.
    """
    return {
        "deployment": deployment,
        "values": {
            "size": list(SIZE_PRESETS),
            **{option: list(values) for option, values in OPTION_VALUES.items()},
        },
        "size_rule": dict(SIZE_RULE),
        "size_presets": list(SIZE_PRESETS),
        "defaults": dict(PRODUCT_DEFAULTS),
        "output_format": OUTPUT_FORMAT,
        "unsupported": unsupported_for(deployment),
    }


__all__ = [
    "MAX_IMAGES_PER_REQUEST",
    "MAX_INPUT_BYTES",
    "MAX_INPUT_IMAGES",
    "MAX_REFERENCE_IMAGES",
    "OPTION_KEYS",
    "OPTION_VALUES",
    "OUTPUT_FORMAT",
    "PARAM_TO_OPTION",
    "PRODUCT_DEFAULTS",
    "SIZE_PRESETS",
    "SIZE_RULE",
    "capability_map",
    "is_experimental_size",
    "normalize_size",
    "parse_size",
    "record_unsupported",
    "rejected_option",
    "reset",
    "size_problem",
    "unsupported_for",
    "validate_option",
    "validate_size",
]
