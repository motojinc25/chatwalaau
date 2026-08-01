"""Image output option capabilities for the configured image model (v0.117.6).

CTR-0049 / CTR-0120. The image output options -- size / quality / format /
compression / background -- are NOT uniformly supported across image models. The
Images API rejects an unsupported one with an HTTP 400 naming the parameter::

    {'message': 'Transparent background is not supported for this model.',
     'type': 'image_generation_user_error', 'param': 'background', ...}
    {'message': "Invalid value: 'webp'. Supported values are: 'png' and 'jpeg'.",
     'type': 'invalid_request_error', 'param': 'output_format', ...}

The SPA control (CTR-0120) offered every value unconditionally, so a user could
pick one the deployed model cannot honor and the turn failed with a raw provider
error surfaced as the tool result.

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

# The full option surface advertised to the SPA. Single source of truth: the control
# renders these and the backend validates against them, so a value cannot exist in one
# and not the other.
#
# v0.117.6, per the gpt-image-2 specification:
#  - the size list gained the 2K / 4K entries, which were missing entirely;
#  - `transparent` is WITHDRAWN from background: the model reports it as not
#    supported for this model;
#  - `webp` is WITHDRAWN from format: the deployment accepts png and jpeg only.
# Withdrawing them is an operator decision recorded here rather than a guess: an
# option nobody can select cannot fail a turn. The learned-capability path below
# still covers anything else a deployment turns out to refuse.
OPTION_VALUES: dict[str, tuple[str, ...]] = {
    "size": (
        "auto",
        "1024x1024",  # square
        "1536x1024",  # landscape
        "1024x1536",  # portrait
        "2048x2048",  # 2K square
        "2048x1152",  # 2K landscape
        "3840x2160",  # 4K landscape
        "2160x3840",  # 4K portrait
    ),
    "quality": ("auto", "low", "medium", "high"),
    "format": ("png", "jpeg"),
    "background": ("auto", "opaque"),
}

# Documented API defaults, used when reporting what a dropped option fell back to.
OPTION_DEFAULTS: dict[str, str] = {
    "size": "auto",
    "quality": "auto",
    "format": "png",
    "background": "auto",
}

# output_compression is an integer 0-100 and only applies to the lossy formats.
# Derived from the offered surface so re-adding a format cannot leave this stale.
COMPRESSION_RANGE = (0, 100)
COMPRESSION_FORMATS = frozenset(OPTION_VALUES["format"]) - {"png"}


def validate_option(option: str, value: str) -> str | None:
    """Return a human-readable problem with ``option=value``, or None when it is fine.

    v0.117.6: the resolved options were sent to the Images API unchecked, so a typo or
    a stale localStorage entry became an opaque provider 400. This validates the VALUE
    SURFACE only -- whether a given model honors a supported value is a separate,
    learned question (``unsupported_for``), because no static table can answer it.
    """
    if not value:
        return None
    if option == "compression":
        try:
            number = int(value)
        except (TypeError, ValueError):
            return f"compression must be a whole number 0-100, got {value!r}"
        low, high = COMPRESSION_RANGE
        if not (low <= number <= high):
            return f"compression must be between {low} and {high}, got {number}"
        return None
    allowed = OPTION_VALUES.get(option)
    if allowed is None:
        return f"unknown image option {option!r}"
    if value not in allowed:
        return f"{option} must be one of {', '.join(allowed)}; got {value!r}"
    return None

# Images API parameter name -> the option key used by the SPA / catalog defaults.
PARAM_TO_OPTION: dict[str, str] = {
    "size": "size",
    "quality": "quality",
    "output_format": "format",
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
    if option not in OPTION_VALUES or not value:
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
    sent for that parameter, and parsing it out of prose would be guesswork.

    v0.117.6: the error fields are read through ``app.core.provider_errors``. This
    used to reach for ``error.body["error"]``, but the OpenAI SDK unwraps that key
    before constructing the exception, so the lookup always missed -- no retry
    happened, nothing was learned, and the tool reported a bare "BadRequestError".
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
    """Capability view for ``GET /api/model`` (CTR-0069) and the SPA control.

    ``values`` is the full surface; ``unsupported`` lists what this deployment has
    been observed to reject. Everything not listed is offered -- an option is never
    hidden on a guess, only on an observation.
    """
    return {
        "deployment": deployment,
        "values": {option: list(values) for option, values in OPTION_VALUES.items()},
        "unsupported": unsupported_for(deployment),
    }


__all__ = [
    "COMPRESSION_FORMATS",
    "COMPRESSION_RANGE",
    "OPTION_DEFAULTS",
    "OPTION_VALUES",
    "PARAM_TO_OPTION",
    "capability_map",
    "record_unsupported",
    "rejected_option",
    "reset",
    "unsupported_for",
    "validate_option",
]
