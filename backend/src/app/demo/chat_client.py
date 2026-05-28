"""DemoChatClient -- a MAF ChatClient that streams scripted scenarios (PRP-0066).

Subclasses ``agent_framework.BaseChatClient`` so it plugs straight into
the existing AgentRegistry / Agent / AG-UI pipeline. No new MAF
Protocol seam is introduced -- the existing one is exactly the right
boundary (UDR-0041 D3).

When the Agent invokes ``get_response(stream=True)``:

1. The most recent user message is classified by `app.demo.scenarios.classify`.
2. The scripted steps for that scenario are replayed as
   ``ChatResponseUpdate`` items: text deltas, optional reasoning deltas,
   and (for tool-routed scenarios) a ``function_call`` Content that
   makes the Agent re-enter its tool loop. The MAF Agent then runs
   the real tool (Weather / demo image / demo RAG) and re-invokes
   this client with the longer message list.
3. The second invocation sees a ``function_result`` in the trailing
   message tail and replies in plain text.

Streaming is paced at ``DEMO_LATENCY_MS`` per token chunk so the SPA
streaming UX (token-by-token render, ScrollToBottom suspend, abort
button) is exercised.

UDR-0041 D4: no outbound HTTP / WS call is made by this client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
import uuid

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Mapping, Sequence

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
    UsageDetails,
)
from agent_framework._tools import FunctionInvocationLayer

from app.core.config import settings
from app.demo.scenarios import (
    FunctionCallStep,
    ReasoningStep,
    TextStep,
    build_script,
    classify,
    second_pass_text,
)

logger = logging.getLogger(__name__)


def _latest_user_text(messages: Sequence[Message]) -> str:
    """Return the trailing user message text content (empty if none)."""
    for msg in reversed(messages):
        role = getattr(msg, "role", None)
        if str(role) != "user":
            continue
        # Concatenate text contents of the trailing user message.
        parts: list[str] = []
        for content in getattr(msg, "contents", None) or []:
            if isinstance(content, str):
                parts.append(content)
                continue
            ctype = getattr(content, "type", None)
            if ctype in ("text", "text_reasoning"):
                value = getattr(content, "text", None)
                if value:
                    parts.append(value)
        return " ".join(parts).strip()
    return ""


def _has_recent_tool_result(messages: Sequence[Message]) -> bool:
    """True when the message list tail contains a function_result.

    The Agent re-invokes the client with the function_result appended
    after a tool call completes. Walk from the end until we hit a real
    user message; finding a function_result on the way means we are in
    the post-tool pass.
    """
    for msg in reversed(messages):
        role = str(getattr(msg, "role", "") or "")
        if role == "user":
            return False
        for content in getattr(msg, "contents", None) or []:
            ctype = getattr(content, "type", None)
            if ctype == "function_result":
                return True
    return False


def _chunk_text(text: str, *, chunk_chars: int = 24) -> list[str]:
    """Split text into streaming-sized chunks for ChatResponseUpdate deltas."""
    if not text:
        return []
    chunks: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        end = min(i + chunk_chars, n)
        # Try to break on whitespace if possible so chunks render as
        # word-ish units instead of mid-word fragments.
        if end < n:
            space_idx = text.rfind(" ", i, end + 8)
            if space_idx > i:
                end = space_idx + 1
        chunks.append(text[i:end])
        i = end
    return chunks


class DemoChatClient(FunctionInvocationLayer, BaseChatClient):
    """In-process MAF ChatClient that streams scripted demo scenarios.

    This client is intentionally simple: every reply is determined by
    a keyword lookup on the latest user message, with a follow-up
    branch for the post-tool-result pass. No external service is
    contacted (UDR-0041 D4).

    Inherits ``FunctionInvocationLayer`` so MAF's Agent recognises this
    client as supporting tool calls -- otherwise the function_call
    Content emitted from scenarios would not be dispatched to the
    registered tools. This matches the pattern used by OpenAIChatClient.
    """

    OTEL_PROVIDER_NAME = "chatwalaau-demo"
    STORES_BY_DEFAULT = False

    def __init__(self, *, model: str = "chatwalaau-demo") -> None:
        super().__init__()
        self._model = model

    # BaseChatClient hook ----------------------------------------------------

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        # We do not run ``await self._validate_options(options)`` because the
        # demo client accepts every option and ignores them. Live clients
        # validate against their TypedDict; this one is provider-agnostic.
        del options, kwargs  # documented unused; the demo ignores all options.

        if stream:
            return ResponseStream(
                self._stream_updates(messages),
                finalizer=ChatResponse.from_updates,
            )
        return self._collect_non_stream(messages)

    # Streaming implementation ----------------------------------------------

    async def _stream_updates(
        self,
        messages: Sequence[Message],
    ) -> AsyncIterable[ChatResponseUpdate]:
        user_text = _latest_user_text(messages)
        has_tool_result = _has_recent_tool_result(messages)
        kind = classify(user_text, has_prior_tool_result=has_tool_result)

        latency_ms = settings.demo_latency_ms_clamped
        per_chunk_delay = latency_ms / 1000.0

        response_id = f"demo-{uuid.uuid4().hex[:12]}"
        message_id = f"demo-msg-{uuid.uuid4().hex[:12]}"

        # Track input characters so the usage event has plausible numbers.
        input_chars = sum(len(_latest_user_text([msg])) for msg in messages if str(getattr(msg, "role", "")) == "user")
        output_chars = 0

        # Post-tool pass: emit a single prose reply tailored to the scenario.
        if has_tool_result:
            follow_up_text = second_pass_text(kind) or (
                "I've processed the tool result. (In demo mode this reply is scripted.)"
            )
            async for update in self._emit_text(
                follow_up_text,
                response_id=response_id,
                message_id=message_id,
                per_chunk_delay=per_chunk_delay,
            ):
                yield update
            output_chars += len(follow_up_text)
            yield self._build_usage_update(
                response_id=response_id,
                input_chars=input_chars,
                output_chars=output_chars,
            )
            return

        # First pass: replay the scripted steps for this scenario.
        script = build_script(kind, model=self._model, user_text=user_text)
        for step in script:
            if isinstance(step, ReasoningStep):
                async for update in self._emit_reasoning(
                    step.text,
                    response_id=response_id,
                    message_id=message_id,
                    per_chunk_delay=per_chunk_delay,
                ):
                    yield update
                output_chars += len(step.text)
            elif isinstance(step, TextStep):
                async for update in self._emit_text(
                    step.text,
                    response_id=response_id,
                    message_id=message_id,
                    per_chunk_delay=per_chunk_delay,
                ):
                    yield update
                output_chars += len(step.text)
            elif isinstance(step, FunctionCallStep):
                yield self._build_function_call_update(
                    name=step.name,
                    arguments=step.arguments,
                    response_id=response_id,
                    message_id=message_id,
                )
            else:  # pragma: no cover -- exhaustive on Step union
                logger.warning("Unknown demo step kind: %r", step)

        yield self._build_usage_update(
            response_id=response_id,
            input_chars=input_chars,
            output_chars=output_chars,
        )

    async def _emit_text(
        self,
        text: str,
        *,
        response_id: str,
        message_id: str,
        per_chunk_delay: float,
    ) -> AsyncIterable[ChatResponseUpdate]:
        for chunk in _chunk_text(text):
            yield ChatResponseUpdate(
                role="assistant",
                contents=[Content(type="text", text=chunk)],
                response_id=response_id,
                message_id=message_id,
                model=self._model,
            )
            if per_chunk_delay > 0:
                await asyncio.sleep(per_chunk_delay)

    async def _emit_reasoning(
        self,
        text: str,
        *,
        response_id: str,
        message_id: str,
        per_chunk_delay: float,
    ) -> AsyncIterable[ChatResponseUpdate]:
        for chunk in _chunk_text(text):
            yield ChatResponseUpdate(
                role="assistant",
                contents=[Content(type="text_reasoning", text=chunk)],
                response_id=response_id,
                message_id=message_id,
                model=self._model,
            )
            if per_chunk_delay > 0:
                await asyncio.sleep(per_chunk_delay)

    def _build_function_call_update(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        response_id: str,
        message_id: str,
    ) -> ChatResponseUpdate:
        call_id = f"call_demo_{uuid.uuid4().hex[:10]}"
        return ChatResponseUpdate(
            role="assistant",
            contents=[
                Content(
                    type="function_call",
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                )
            ],
            response_id=response_id,
            message_id=message_id,
            model=self._model,
        )

    def _build_usage_update(
        self,
        *,
        response_id: str,
        input_chars: int,
        output_chars: int,
    ) -> ChatResponseUpdate:
        """Emit a synthetic usage event derived from character counts.

        Token counts are deliberately approximate: chars/4 is a
        reasonable rough match for English content, and the SPA's
        context-window indicator only needs plausible numbers, not
        accurate ones.
        """
        input_tokens = max(1, input_chars // 4)
        output_tokens = max(1, output_chars // 4)
        usage = UsageDetails(
            input_token_count=input_tokens,
            output_token_count=output_tokens,
            total_token_count=input_tokens + output_tokens,
        )
        return ChatResponseUpdate(
            role="assistant",
            contents=[Content(type="usage", usage_details=usage)],
            response_id=response_id,
            model=self._model,
            created_at=int(time.time()),
        )

    # Non-streaming fallback ------------------------------------------------

    async def _collect_non_stream(self, messages: Sequence[Message]) -> ChatResponse:
        """Materialise the streaming result as a single ChatResponse.

        Used by callers that pass ``stream=False`` (rare in this codebase,
        but supported by the BaseChatClient contract).
        """
        updates: list[ChatResponseUpdate] = [upd async for upd in self._stream_updates(messages)]
        return ChatResponse.from_updates(updates)


__all__ = ["DemoChatClient"]
