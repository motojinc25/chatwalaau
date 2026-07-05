"""Run one agent turn for a Teams message (CTR-0140, PRP-0092, UDR-0070 D6/D8).

Reuses the EXACT agent core and approval machinery the AG-UI endpoint (CTR-0009)
uses -- the registry chokepoint agent, the resilient streaming run
(``_resilient_run``), the per-iteration ``IterationContentAccumulator``, and the
provider-aware approval continuation (``build_iteration_messages`` +
``to_function_approval_response``). The only difference from the AG-UI path is the
sink: instead of emitting AG-UI SSE events, this module ACCUMULATES the assistant
text and returns it for the adapter to chunk and send proactively (Teams is
request/response, UDR-0070 D6).

Tool approval (UDR-0070 D8): when MAF pauses on a ``function_approval_request``,
the loop honors a prior "Allow Session" grant via the CTR-0099 session cache, or
calls ``approval_renderer`` (an Adaptive Card; parks until the user decides). The
resolved decision is turned into the proper MAF ``function_approval_response`` and
the next iteration is built with the originating ``function_call`` paired alongside
it -- WITHOUT this pairing MAF re-emits the same approval request and the bot would
re-prompt in a loop. Teams NEVER auto-approves.

This module imports the Teams SDK nowhere; it depends only on the core.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.teams.message import TeamsMessage

logger = logging.getLogger(__name__)

# An approval renderer: given (record_id, thread_id, tool_name, arguments_preview,
# iteration, max_iterations), render the Adaptive Card, park until the user
# decides, and return True/False. The trailing (iteration, max_iterations) are the
# PRP-0103 / UDR-0082 D3 round counter shown on the card.
ApprovalRenderer = Callable[[str, str, str, dict[str, Any], int, int], Awaitable[bool]]


def _collect_generated_images(content: Any, out: list[str]) -> None:
    """Extract /api/uploads image URLs from an image-generation tool result.

    The generate_image / edit_image tools return a JSON result
    ``{"images": [{"url": "/api/uploads/<thread>/generated_*.png", ...}], ...}``.
    Other tools' results are ignored. Collected in order, de-duplicated.
    """
    import json

    raw = getattr(content, "result", "") or ""
    if not isinstance(raw, str):
        try:
            raw = json.dumps(raw)
        except (TypeError, ValueError):
            return
    if "/api/uploads/" not in raw:
        return
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, list):
        return
    for img in images:
        if isinstance(img, dict):
            url = img.get("url")
            if isinstance(url, str) and url.startswith("/api/uploads/") and url not in out:
                out.append(url)


def _build_input_messages(msg: TeamsMessage) -> list[Any]:
    """Build the MAF input messages for a TeamsMessage (text + cached images)."""
    from agent_framework import Message

    contents: list[Any] = []
    text = (msg.text or "").strip()
    if text:
        contents.append(text)
    # Cached images (when attachment caching lands) are referenced by stored URI.
    contents.extend({"type": "image", "uri": uri} for uri in msg.images)
    if not contents:
        contents.append("")
    return [Message(role="user", contents=contents)]


async def run_turn(
    msg: TeamsMessage,
    *,
    agent_registry: Any,
    model: str | None = None,
    approval_renderer: ApprovalRenderer | None = None,
) -> str:
    """Run the agent for one Teams message and return the accumulated reply text.

    Mirrors the AG-UI outer approval loop but accumulates text instead of streaming
    SSE. Honors the per-run instruction remainder and the image-gen contextvar, and
    resolves tool approval through the CTR-0099 store + ``approval_renderer``
    (UDR-0070 D8). The loop is bounded by the shared two-counter budget
    (``TOOL_APPROVAL_MAX_ITERATIONS`` interactive rounds +
    ``TOOL_APPROVAL_ABSOLUTE_MAX_ITERATIONS`` absolute backstop; PRP-0103 /
    UDR-0082 D2/D5), at parity with the AG-UI endpoint.
    """
    import uuid

    from agent_framework import AgentSession

    from app.agent.approval import approval_store, truncate_arguments_preview
    from app.agent.approval_iteration import IterationContentAccumulator
    from app.agui.endpoint import (  # type: ignore[attr-defined]
        _arguments_to_dict,
        _image_gen_thread_id,
        _resilient_run,
        _RetryNotice,
    )
    from app.core.config import settings
    from app.teams.session import persist_turn

    thread_id = msg.thread_id
    effective_model = model or agent_registry.default_model
    agent = agent_registry.get(model)

    run_options: dict[str, Any] = {}
    extra_instructions = agent_registry.run_instructions(model, user_profile_block=None, temporary=False)
    if extra_instructions:
        run_options["instructions"] = extra_instructions
    _image_gen_thread_id.set(thread_id)

    # The agent's FileHistoryProvider (CTR-0014) loads prior turns from this thread's
    # session file via metadata["ag_ui_thread_id"], so the model sees the Teams
    # conversation history (UDR-0070 D5); persistence below accumulates it.
    session = AgentSession()
    session.metadata = {"ag_ui_thread_id": thread_id, "ag_ui_run_id": uuid.uuid4().hex}
    iteration_messages = _build_input_messages(msg)
    assistant_text_parts: list[str] = []
    # Generated images live in the image tool's RESULT (not the assistant text); we
    # collect their /api/uploads URLs across iterations so they render on the Web SPA
    # (appended as Markdown below) AND can be sent to Teams as real data (UDR-0070 D9).
    generated_image_uris: list[str] = []

    def _finish() -> str:
        final_text = "".join(assistant_text_parts).strip()
        # Append each generated image as Markdown so the persisted message renders the
        # image on the Web SPA, and the Teams adapter can extract + attach the bytes.
        for uri in generated_image_uris:
            if uri and uri not in final_text:
                final_text = f"{final_text}\n\n![generated image]({uri})".strip()
        persist_turn(
            thread_id,
            user_text=msg.text,
            assistant_text=final_text,
            conversation_type=msg.conversation_type,
        )
        return final_text

    # PRP-0103 / UDR-0082 D2/D5: two-counter approval budget, parity with AG-UI.
    max_iterations = settings.tool_approval_max_iterations
    absolute_max_iterations = settings.tool_approval_absolute_max_iterations
    interactive_rounds = 0
    total_rounds = 0
    while True:
        # Per-iteration accumulator pairs this iteration's function_call(s) with the
        # approval response(s) for the next agent.run (prevents the re-prompt loop).
        accumulator = IterationContentAccumulator()
        pending: list[tuple[Any, Any]] = []  # (ApprovalRecord, request_content)

        async for update in _resilient_run(agent, iteration_messages, session, run_options):
            if isinstance(update, _RetryNotice):
                continue
            for content in getattr(update, "contents", None) or []:
                content_type = getattr(content, "type", None)
                if content_type == "text_reasoning":
                    accumulator.observe_text_reasoning(content)
                elif content_type == "text":
                    accumulator.observe_text(content)
                    text = getattr(content, "text", None)
                    if text:
                        assistant_text_parts.append(text)
                elif content_type == "function_call":
                    accumulator.observe_function_call(content)
                elif content_type == "function_result":
                    accumulator.observe_function_result(content)
                    _collect_generated_images(content, generated_image_uris)
                elif content_type == "function_approval_request":
                    fn_call = getattr(content, "function_call", None)
                    if fn_call is None:
                        continue
                    accumulator.observe_function_call_from_approval(fn_call)
                    tool_name = getattr(fn_call, "name", "") or "<unknown>"
                    call_id = getattr(fn_call, "call_id", "") or ""
                    preview = truncate_arguments_preview(_arguments_to_dict(getattr(fn_call, "arguments", None)))
                    record = await approval_store.register(
                        thread_id=thread_id, tool_name=tool_name, call_id=call_id, arguments_preview=preview
                    )
                    pending.append((record, content))

        if not pending:
            return _finish()

        # Resolve every paused approval (session-cache grant or rendered card), then
        # build the iter-N+1 input with the proper MAF approval responses.
        approval_response_contents, round_sources = await _resolve_pending(
            pending,
            thread_id,
            approval_renderer,
            approval_store,
            iteration=interactive_rounds + 1,
            max_iterations=max_iterations,
        )
        iter_messages = _build_iteration_messages(accumulator, approval_response_contents, effective_model)
        iteration_messages = [*iteration_messages, *iter_messages]
        # The approval handshake interrupted the iter-N response stream; clear the
        # uncommitted server-side response id so the re-run relies on explicit
        # context (mirrors the AG-UI post-approval reset).
        session.service_session_id = None

        # PRP-0103 / UDR-0082 D2: cached rounds (every approval resolved from the
        # session grant) are free against the interactive budget; every round
        # counts toward the absolute backstop.
        total_rounds += 1
        round_cached = bool(round_sources) and all(s == "session-cache" for s in round_sources)
        if not round_cached:
            interactive_rounds += 1
        if interactive_rounds > max_iterations or total_rounds > absolute_max_iterations:
            logger.warning(
                "Teams approval loop exceeded for thread %s (interactive=%d/%d, total=%d/%d)",
                thread_id,
                interactive_rounds,
                max_iterations,
                total_rounds,
                absolute_max_iterations,
            )
            return _finish()


async def _resolve_pending(
    pending: list[tuple[Any, Any]],
    thread_id: str,
    approval_renderer: ApprovalRenderer | None,
    approval_store: Any,
    *,
    iteration: int,
    max_iterations: int,
) -> tuple[list[Any], list[str]]:
    """Resolve each paused approval to a MAF function_approval_response (UDR-0070 D8).

    Returns ``(responses, sources)`` where ``sources`` is the resolution source
    per record ("session-cache" for a session-grant hit, "user" otherwise) so
    the caller can classify the round as cached or interactive (PRP-0103 /
    UDR-0082 D2). A rendered "Allow Session" decision caches the grant and
    cascades onto any sibling records still pending for the same
    (thread_id, tool_name), so they resolve as "session-cache" here.
    """
    responses: list[Any] = []
    sources: list[str] = []
    for record, request_content in pending:
        cached = await approval_store.lookup_session_cache(thread_id=thread_id, tool_name=record.tool_name)
        if cached is not None:
            approved = cached
            source = "session-cache"
        elif approval_renderer is not None:
            approved = await approval_renderer(
                record.id, thread_id, record.tool_name, record.arguments_preview, iteration, max_iterations
            )
            source = "user"
        else:
            # No renderer wired -> Teams must NOT auto-approve (UDR-0070 D8): deny.
            approved = False
            source = "user"
        # resolve() is idempotent: if a cascade (UDR-0082 D4) already released
        # this record as "session-cache", that resolution stands.
        await approval_store.resolve(record.id, approved=approved, source=source)
        actual = record.resolution.source if record.resolution else source
        sources.append(actual)
        # The original request content builds the MAF response MAF matches by id.
        responses.append(request_content.to_function_approval_response(approved=approved))
        await approval_store.drop(record.id)
    return responses, sources


def _build_iteration_messages(
    accumulator: Any, approval_response_contents: list[Any], effective_model: str
) -> list[Any]:
    """Build the iter-N+1 [assistant, user] pair, provider-aware (matches AG-UI)."""
    from app import providers
    from app.demo import is_demo_mode

    # Anthropic needs the signed reasoning + original tool_use Content replayed;
    # OpenAI reasoning models need the function_call rebuilt without server ids.
    is_anthropic = (not is_demo_mode()) and providers.provider_for(effective_model).name == "anthropic"
    return accumulator.build_iteration_messages(
        approval_response_contents,
        include_reasoning=is_anthropic,
        strip_function_call_ids=not is_anthropic,
    )
