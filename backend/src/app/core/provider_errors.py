"""Reading the provider's own error detail out of an SDK exception (v0.117.6).

Both the AG-UI run-error classifier (CTR-0009) and the image-generation tools
(CTR-0049) need the same three facts from a failed OpenAI / Azure OpenAI call: the
human-readable ``message``, the ``param`` at fault, and the error ``code``. Getting
at them is easy to get WRONG, and this module exists so it is only done once.

The trap: the wire body is::

    {"error": {"message": ..., "type": ..., "param": ..., "code": ...}}

but the SDK UNWRAPS it before constructing the exception --
``openai/_client.py``: ``data = body.get("error", body)`` -- so ``exc.body`` is the
INNER dict, with no ``"error"`` key. Code that reached for ``exc.body["error"]``
silently found nothing: the image tools stopped retrying and reported a bare
"BadRequestError", and the structured-output classifier lost the provider's
diagnosis. Both looked correct in tests because the test doubles copied the
assumption instead of the SDK.

``openai.APIError`` also exposes ``.message`` / ``.param`` / ``.code`` / ``.type``
directly (set in its ``__init__`` from that same inner dict). Those attributes are
the SDK's own public contract, so they are preferred here; the body is only a
fallback, and it accepts BOTH shapes so a future SDK that stops unwrapping does not
reintroduce the bug.
"""

from __future__ import annotations

from typing import Any

__all__ = ["error_code", "error_detail", "error_message", "error_param", "error_type", "walk_causes"]


def walk_causes(exc: BaseException) -> list[BaseException]:
    """The exception and its ``__cause__`` / ``__context__`` chain, cycle-safe.

    agent-framework wraps provider errors (e.g. in ``ChatClientException``), so the
    interesting exception is rarely the outermost one.
    """
    out: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def error_detail(exc: BaseException) -> dict[str, Any] | None:
    """The provider's error dict for ``exc`` itself (no chain walk), or None.

    Accepts the unwrapped shape the SDK produces AND the wrapped wire shape.
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    inner = body.get("error")
    if isinstance(inner, dict):
        return inner
    # Unwrapped by the SDK: the body IS the error object. Require a recognizable
    # key so an unrelated JSON body is not mistaken for one.
    if any(key in body for key in ("message", "param", "code", "type")):
        return body
    return None


def _field(exc: BaseException, attr: str) -> str | None:
    """One error field, preferring the SDK attribute over the body, across the chain."""
    for current in walk_causes(exc):
        value = getattr(current, attr, None)
        if isinstance(value, str) and value:
            return value
        detail = error_detail(current)
        if detail is not None:
            from_body = detail.get(attr)
            if isinstance(from_body, str) and from_body:
                return from_body
    return None


def error_message(exc: BaseException) -> str | None:
    """The provider's human-readable message, e.g. "Transparent background is not
    supported for this model." Never the SDK's "Error code: 400 - {...}" dump."""
    for current in walk_causes(exc):
        detail = error_detail(current)
        if detail is not None:
            message = detail.get("message")
            if isinstance(message, str) and message:
                return message
        # `.message` on an APIStatusError is the "Error code: N - {...}" dump, which
        # is not what callers want, so it is only used when the body carried nothing
        # AND it does not look like that dump.
        value = getattr(current, "message", None)
        if isinstance(value, str) and value and not value.startswith("Error code:"):
            return value
    return None


def error_param(exc: BaseException) -> str | None:
    """The request parameter the provider rejected, e.g. ``background``."""
    return _field(exc, "param")


def error_code(exc: BaseException) -> str | None:
    """The provider's error code, e.g. ``invalid_value`` / ``invalid_json_schema``."""
    return _field(exc, "code")


def error_type(exc: BaseException) -> str | None:
    """The provider's error type, e.g. ``image_generation_user_error``."""
    return _field(exc, "type")
