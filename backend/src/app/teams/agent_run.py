"""Run one agent turn for a Teams message (CTR-0140, PRP-0092, UDR-0070 D6).

Reuses the EXACT agent core the AG-UI endpoint (CTR-0009) uses -- the registry
chokepoint agent and the resilient streaming run (``_resilient_run``). The only
difference from the AG-UI path is the sink: instead of emitting AG-UI SSE events,
this module ACCUMULATES the assistant text and returns it for the adapter to chunk
and send proactively (Teams is request/response, UDR-0070 D6).

A turn is ONE agent run (PRP-0179, UDR-0161 D2). No tool is approval-gated, so the
former Adaptive Card approval loop (UDR-0070 D8, CTR-0141) is gone. Which tools a
Teams turn can reach is decided by configuration (CODING_ENABLED, the active
agent's tool allow-list) and who can start one by ``ALLOWED_USERS`` (UDR-0161 D4).
A residual approval request is a named defect that ends the turn (D3).

This module imports the Teams SDK nowhere; it depends only on the core.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agent_framework import add_usage_details

from app.agui.sanitize import sanitize_text

if TYPE_CHECKING:
    from app.teams.message import TeamsMessage

logger = logging.getLogger(__name__)


def _record_turn(
    turn_usage: dict[str, Any] | None,
    model_calls: int,
    effective_model: str,
    thread_id: str,
) -> None:
    """Append this Teams turn to the CTR-0200 ledger (PRP-0158, UDR-0136 D5).

    Writes the SUMMARY produced by the shared CTR-0009 derivation, so this lane and
    the SPA lane record the same normalized price points rather than two dialects of
    the same numbers. Self-silencing: statistics may never fail a turn (D6).
    """
    if not turn_usage or model_calls <= 0:
        return
    try:
        from app import providers
        from app.agui.token_usage import turn_summary
        from app.usage.ledger import append_turn_usage

        append_turn_usage(
            lane="teams",
            turn=turn_summary(
                turn_usage,
                model_calls=model_calls,
                includes_cache_read=providers.input_tokens_include_cache_read(effective_model),
            ),
            model=effective_model,
            thread_id=thread_id,
        )
    except Exception:
        logger.warning("usage ledger teams append failed for thread %s", thread_id, exc_info=True)


def _collect_generated_images(content: Any, out: list[str]) -> None:
    """Extract /api/uploads image URLs from an image-generation tool result.

    The image_generate / image_edit tools return a JSON result
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
) -> str:
    """Run the agent for one Teams message and return the accumulated reply text.

    One run per turn (UDR-0161 D2), accumulating text instead of streaming SSE.
    Honors the per-run instruction remainder and the image-gen contextvar.
    """
    import uuid

    from agent_framework import AgentSession

    from app.agui.endpoint import (  # type: ignore[attr-defined]
        _image_gen_thread_id,
        _resilient_run,
        _RetryNotice,
    )
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
    input_messages = _build_input_messages(msg)
    assistant_text_parts: list[str] = []
    # Generated images live in the image tool's RESULT (not the assistant text); we
    # collect their /api/uploads URLs so they render on the Web SPA (appended as
    # Markdown below) AND can be sent to Teams as real data (UDR-0070 D9).
    generated_image_uris: list[str] = []

    # Token Usage Ledger accumulation (CTR-0200, PRP-0158, UDR-0136 D5), summed over
    # every model call of the run exactly as CTR-0009 does.
    turn_usage: dict[str, Any] | None = None
    model_calls = 0
    unexpected_approval_tool: str | None = None

    async for update in _resilient_run(agent, input_messages, session, run_options):
        if isinstance(update, _RetryNotice):
            continue
        for content in getattr(update, "contents", None) or []:
            content_type = getattr(content, "type", None)
            if content_type == "text":
                text = getattr(content, "text", None)
                if text:
                    assistant_text_parts.append(text)
            elif content_type == "usage":
                # One MAF usage content per MODEL CALL. Summed with the framework's
                # own public helper, exactly as the AG-UI seam does, so both lanes
                # report the same quantity (PRP-0158).
                details = getattr(content, "usage_details", None) or {}
                turn_usage = dict(add_usage_details(turn_usage, dict(details)))
                model_calls += 1
            elif content_type == "function_result":
                _collect_generated_images(content, generated_image_uris)
            elif content_type == "function_approval_request":
                # UDR-0161 D3: no tool is approval-gated, so this is a construction
                # defect. Not parked, approved, denied or dropped -- the turn ends and
                # the reply names the tool.
                fn_call = getattr(content, "function_call", None)
                unexpected_approval_tool = getattr(fn_call, "name", "") or "<unknown>"
                logger.error(
                    "Tool %r asked for approval (call_id=%s, thread=%s), but no tool is "
                    "approval-gated since PRP-0179; ending the Teams turn (UDR-0161 D3).",
                    unexpected_approval_tool,
                    getattr(fn_call, "call_id", None),
                    thread_id,
                )
                break
        if unexpected_approval_tool is not None:
            break

    if unexpected_approval_tool is not None:
        assistant_text_parts.append(
            f"\n\nTool '{unexpected_approval_tool}' asked for approval, which this version no "
            "longer supports. This is a defect -- please report it."
        )

    # Model-private citation markup never reaches a reply (CTR-0218, UDR-0160 D1).
    # This channel has no surface for the report, so the removal is logged only.
    cleaned, stripped = sanitize_text("".join(assistant_text_parts))
    if stripped:
        logger.warning(
            "Removed model-private citation markup from a Teams reply: count=%d markers=%s",
            len(stripped),
            stripped[:10],
        )
    final_text = cleaned.strip()
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
    # CTR-0200 append (PRP-0158), exactly once per turn. Best-effort (UDR-0136 D6).
    _record_turn(turn_usage, model_calls, effective_model, thread_id)
    return final_text
