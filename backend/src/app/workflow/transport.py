"""Workflow node model calls use the streaming transport (PRP-0194, UDR-0176, CTR-0180).

MAF's declarative executor runs a workflow Prompt node with a NON-streaming
``agent.run()``. On the provider SDKs a non-streaming request with the effort
ladder's output budget (UDR-0166 D6, xhigh = 64000) is either refused before it is
sent (Anthropic: "Streaming is required for operations that may take longer than 10
minutes", fired for ``max_tokens > 21333``) or cut at the 600 s default timeout and
retried twice (OpenAI). The streaming transport has neither limit: its timeout
applies between chunks.

``collect_via_stream`` converts at the chat client's ``_inner_get_response`` -- the
Raw connector's method, BELOW the function-invocation loop, chat middleware and
telemetry layers (UDR-0176 D2). A non-streaming call is served by the streaming call
and returns that ONE model call's final response, so every layer above -- including
the ``NodeUsageRecorder`` middleware, which measures only non-streaming contexts
(UDR-0152) -- still sees a non-streaming call. A streaming call passes through.

The override is bound on the INSTANCE (UDR-0176 D3): a ``__class__`` swap to a mixin
subclass fails on the production classes ("object layout differs"), and
``build_prompt_agent`` -- the only caller -- owns a fresh client per node agent, so
the override never reaches another lane.
"""

from __future__ import annotations

import types
from typing import Any

from agent_framework import ResponseStream

# Private attribute (leading underscore): MAF's SerializationMixin.to_dict skips it.
_MARKER = "_chatwalaau_collect_via_stream"


def is_collecting(client: Any) -> bool:
    """True when ``client`` already serves non-streaming calls via the stream."""
    return bool(getattr(client, _MARKER, False))


def collect_via_stream(client: Any) -> Any:
    """Serve ``client``'s non-streaming model calls with its streaming transport.

    Returns the SAME instance. Options, messages and kwargs are passed through
    untouched; no timeout, retry or background flag is added (UDR-0176 D2).
    Idempotent.
    """
    if is_collecting(client):
        return client
    original = client._inner_get_response

    def _inner_get_response(
        _client: Any,  # the bound instance; the original method is already bound
        *,
        messages: Any,
        stream: bool,
        options: Any,
        **kwargs: Any,
    ) -> Any:
        if stream:
            return original(messages=messages, stream=True, options=options, **kwargs)
        return _collect(original, messages, options, kwargs)

    client._inner_get_response = types.MethodType(_inner_get_response, client)
    setattr(client, _MARKER, True)
    return client


async def _collect(original: Any, messages: Any, options: Any, kwargs: dict[str, Any]) -> Any:
    """Stream ONE model call and return its final ``ChatResponse``.

    The connector builds the final response with the request's ``response_format``,
    so structured output survives. The stream is read to the end here, so the
    single-round caveat of UDR-0135 D2 does not apply. A cancelled node cancels this
    coroutine inside the connector's stream, which closes it.
    """
    response_stream = original(messages=messages, stream=True, options=options, **kwargs)
    if not isinstance(response_stream, ResponseStream):
        response_stream = await response_stream
    return await response_stream.get_final_response()


__all__ = ["collect_via_stream", "is_collecting"]
