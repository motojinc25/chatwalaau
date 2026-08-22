"""Auto Session Title background task (CTR-0109, PRP-0077, UDR-0053).

Summarizes a chat's first user message + first assistant reply into a concise
title, replacing the leading-characters truncation. Runs only as a CTR-0108
background task (post-turn, fire-and-forget, error-isolated), so it never blocks
or fails the chat turn.

Gated by ``SESSION_TITLE_MODE`` (CTR-0006): ``truncate`` (default) dispatches
nothing; ``llm`` enables this task. The model is the catalog ``roles.session_title``
binding if set, else the session's model, else the catalog default
(``models_catalog.resolve_task_model``, PRP-0115 / UDR-0096). The ChatClient is
built through the same chokepoint the agent registry uses
(``app.agui.agent_registry._build_chat_client``), so DEMO_MODE is honored and
provider routing is reused. The call is a SINGLE non-streaming completion -- NOT
a full Agent run (no tools, no history, no Identity/Memory assembly).

Idempotency and manual-rename safety use the additive ``auto_title_done``
session-metadata flag (CTR-0014): the task writes at most once and never
overwrites a manually renamed title (the rename handler sets the flag).

PRP-0143 / UDR-0124 adds a SECOND, operator-initiated entry point --
regeneration -- alongside the automatic one, which is unchanged. Regeneration is
the only path allowed to overwrite a claimed title, and it does so by carrying an
explicit ``force`` marker into :func:`_finalize` rather than by clearing or
reinterpreting ``auto_title_done`` (D3/D4).

Its INPUT WINDOW deliberately differs from the automatic one (D4). Automatic
titling summarizes the first user message plus the first assistant reply;
re-running the model over those same two strings would return the same title and
make the control inert. Regeneration therefore summarizes the first exchange plus
the most recent :data:`REGEN_RECENT_EXCHANGES` exchanges, under the same
character cap so the cost profile is unchanged.

In ``truncate`` mode there is no model to call, so regeneration is SYNCHRONOUS
and takes the most recent user message that carries text
(:func:`latest_user_text`, D5) -- deliberately asymmetric with automatic
truncation, which uses the FIRST user message. Re-deriving from the first message
would reproduce the very title the operator just rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any

from app.background import register_task

logger = logging.getLogger(__name__)

# The summarization prompt. Kept terse and language-preserving so titles match
# the conversation's language and stay short.
_TITLE_SYSTEM_PROMPT = (
    "You write a very short, descriptive title for a chat conversation. "
    "Output ONLY the title text -- a concise noun phrase of at most 8 words, "
    "in the SAME language as the conversation, with no surrounding quotes and "
    "no trailing punctuation. Do not prefix it with 'Title:' or any label."
)

_TITLE_USER_TEMPLATE = (
    "Summarize the topic of this conversation as a short title.\n\n"
    "First user message:\n{user}\n\nAssistant reply:\n{assistant}\n\nTitle:"
)

_REGEN_USER_TEMPLATE = (
    "Summarize the topic of this conversation as a short title.\n\nConversation:\n{conversation}\n\nTitle:"
)

# Bound the prompt inputs so a long opening exchange cannot balloon the call.
_INPUT_CHAR_CAP = 2000

# Regeneration window (PRP-0143 / UDR-0124 D4): the first exchange establishes
# the conversation's SUBJECT, the most recent ones establish where it WENT.
# Dropping either produces a worse title than the one being replaced. Tunable --
# what is normative is that the window differs from the automatic one, not the
# number.
REGEN_RECENT_EXCHANGES = 3
# The session title column is capped at 100 chars elsewhere; mirror it here.
_TITLE_CHAR_CAP = 100
# Trailing punctuation stripped from the model output (ASCII + common CJK).
_TRAILING_PUNCT = " \t\r\n.。!！?？,，;；:：、"


def _clean_title(raw: str) -> str:
    """Normalize model output to a single-line, unquoted, capped title."""
    title = " ".join(raw.split())  # collapse whitespace / newlines to one line
    if len(title) >= 2 and title[0] in "\"'" and title[-1] == title[0]:
        title = title[1:-1].strip()
    title = title.strip(_TRAILING_PUNCT)
    return title[:_TITLE_CHAR_CAP].strip()


def _message_text(message: dict[str, Any]) -> str:
    """Return a stored message's text, ignoring non-text content parts.

    Session messages persist as ``{"role": ..., "contents": [{"type": "text",
    "text": ...}, ...]}`` (CTR-0014). An assistant turn may also carry tool
    calls / results; only text is meaningful for a title.
    """
    parts: list[str] = []
    for item in message.get("contents") or []:
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            parts.append(str(item["text"]))
        elif isinstance(item, str) and item:
            parts.append(item)
    return " ".join(" ".join(parts).split())


def latest_user_text(messages: list[dict[str, Any]]) -> str:
    """Most recent user message that carries text (PRP-0143 / UDR-0124 D5).

    The ``truncate``-mode regeneration source. Assistant messages are never
    used. Returns "" when no user message has text.
    """
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        text = _message_text(message)
        if text:
            return text
    return ""


def build_regeneration_window(messages: list[dict[str, Any]]) -> str:
    """Render the ``llm``-mode regeneration prompt window (UDR-0124 D4).

    The first exchange plus the most recent ``REGEN_RECENT_EXCHANGES``
    exchanges, oldest-first, as a plain ``User:`` / ``Assistant:`` transcript.
    Each message is capped so one long turn cannot crowd out the rest, and the
    whole transcript is capped at ``_INPUT_CHAR_CAP`` (keeping the automatic
    path's cost profile). Returns "" when there is nothing to summarize.
    """
    kept = [m for m in (messages or []) if m.get("role") in ("user", "assistant") and _message_text(m)]
    if not kept:
        return ""
    # Head = the opening exchange (first user + the assistant reply that follows
    # it, when present); tail = the most recent exchanges. Indices are used so an
    # overlap on a short conversation collapses instead of duplicating turns.
    head = list(range(min(2, len(kept))))
    tail = list(range(max(0, len(kept) - REGEN_RECENT_EXCHANGES * 2), len(kept)))
    indices = sorted(set(head) | set(tail))
    per_message_cap = max(200, _INPUT_CHAR_CAP // max(1, len(indices)))
    lines = []
    for i in indices:
        role = "User" if kept[i].get("role") == "user" else "Assistant"
        lines.append(f"{role}: {_message_text(kept[i])[:per_message_cap]}")
    return "\n".join(lines)[:_INPUT_CHAR_CAP]


async def _generate_title(user_text: str, assistant_text: str, model: str | None) -> str:
    """Run a single non-streaming completion and return a cleaned title."""
    from agent_framework import Message

    from app.agui.agent_registry import _build_chat_client
    from app.models_catalog import resolve_task_model

    title_model = resolve_task_model("session_title", model)
    client = _build_chat_client(title_model)
    prompt = _TITLE_USER_TEMPLATE.format(
        user=user_text[:_INPUT_CHAR_CAP],
        assistant=assistant_text[:_INPUT_CHAR_CAP],
    )
    messages = [
        Message(role="system", contents=[_TITLE_SYSTEM_PROMPT]),
        Message(role="user", contents=[prompt]),
    ]
    response = await client.get_response(messages, stream=False)
    return _clean_title(getattr(response, "text", "") or "")


async def _generate_title_from_conversation(conversation: str, model: str | None) -> str:
    """Regeneration variant of :func:`_generate_title` (UDR-0124 D4).

    Same system prompt, same single non-streaming completion, same client
    chokepoint -- only the user prompt differs, carrying a whole-conversation
    window instead of the opening exchange.
    """
    from agent_framework import Message

    from app.agui.agent_registry import _build_chat_client
    from app.models_catalog import resolve_task_model

    title_model = resolve_task_model("session_title", model)
    client = _build_chat_client(title_model)
    messages = [
        Message(role="system", contents=[_TITLE_SYSTEM_PROMPT]),
        Message(role="user", contents=[_REGEN_USER_TEMPLATE.format(conversation=conversation[:_INPUT_CHAR_CAP])]),
    ]
    response = await client.get_response(messages, stream=False)
    return _clean_title(getattr(response, "text", "") or "")


def _finalize(thread_id: str, title: str | None, *, force: bool = False) -> str | None:
    """Clear the pending flag and (if appropriate) write the generated title.

    Read-modify-write right before writing: returns None if the session was
    deleted or is temporary (nothing to broadcast). Otherwise ALWAYS clears
    ``auto_title_pending`` (so the sidebar spinner resolves even when generation
    failed and ``title`` is None), and sets ``title`` + ``auto_title_done`` only
    when a title was produced and no prior title was claimed (a manual rename
    sets ``auto_title_done`` first, CTR-0015, so a user title is never
    overwritten). Returns the effective current title to broadcast (CTR-0110).

    ``force=True`` marks an operator-initiated regeneration (PRP-0143 /
    UDR-0124 D4): it is the ONE path allowed to overwrite a claimed title, and
    it re-asserts ``auto_title_done`` afterwards so the AUTOMATIC path stays
    locked out. The flag's meaning (D3) is unchanged -- regeneration is simply
    not the automatic path.
    """
    from app.agent.temporary import is_temporary
    from app.session.storage import read_session_json, write_session_json

    if is_temporary(thread_id):
        return None
    data = read_session_json(thread_id)
    if data is None:
        return None  # session deleted between dispatch and write
    changed = False
    if data.get("auto_title_pending"):
        data["auto_title_pending"] = False
        changed = True
    if title and (force or not data.get("auto_title_done")):
        data["title"] = title
        data["auto_title_done"] = True
        changed = True
    if changed:
        data["updated_at"] = datetime.now(UTC).isoformat()
        write_session_json(thread_id, data)
        logger.info("Auto-title finalized for session %s: %r", thread_id, data.get("title", ""))
    return data.get("title", "")


async def run_session_title_task(ctx: dict[str, Any]) -> None:
    """CTR-0109 task body. Registered as ``"session-title"`` in CTR-0108.

    Generates the title (best-effort), finalizes the session metadata (always
    clearing the pending flag, even on generation failure), then pushes the
    effective title to connected SPA clients via the CTR-0110 notification hub
    so the sidebar updates in real time without a refetch.
    """
    thread_id = ctx.get("thread_id")
    user_text = (ctx.get("user_text") or "").strip()
    assistant_text = (ctx.get("assistant_text") or "").strip()
    model = ctx.get("model")
    if not thread_id or not user_text or not assistant_text:
        return

    from app.agent.temporary import is_temporary

    if is_temporary(thread_id):
        return

    title = ""
    try:
        title = await _generate_title(user_text, assistant_text, model)
    except Exception:
        # Generation failed -> keep the truncation title, but still finalize
        # (clear the pending spinner) below. The CTR-0108 wrapper would also
        # catch this, but catching here guarantees the pending flag is cleared.
        logger.warning("title generation failed for session %s", thread_id, exc_info=True)

    effective = _finalize(thread_id, title or None)
    if effective is not None:
        from app.notifications import hub

        await hub.broadcast({"type": "session_title", "thread_id": thread_id, "title": effective})


async def run_session_title_clear_task(ctx: dict[str, Any]) -> None:
    """Clear a stuck ``auto_title_pending`` spinner WITHOUT generating a title
    (CTR-0109, UDR-0053 D17).

    Dispatched by the AG-UI endpoint when the Auto Session Title generation task
    will NOT run for the first turn -- a run error, a user abort / client
    disconnect, or a first turn that produced no assistant text. Without this the
    sidebar spinner (driven by ``auto_title_pending``, set at session init) would
    animate forever. Acts only when the flag is actually set; reconciles the
    persisted metadata (so a reload shows no spinner) and pushes the current
    (truncation) title over the CTR-0110 hub so the live spinner clears too.
    """
    thread_id = ctx.get("thread_id")
    if not thread_id:
        return
    from app.agent.temporary import is_temporary

    if is_temporary(thread_id):
        return
    from app.session.storage import read_session_json

    data = read_session_json(thread_id)
    if data is None or not data.get("auto_title_pending"):
        return  # nothing pending to reconcile (no-op on later turns)
    effective = _finalize(thread_id, None)  # clears pending, writes no title
    if effective is not None:
        from app.notifications import hub

        await hub.broadcast({"type": "session_title", "thread_id": thread_id, "title": effective})


async def run_session_title_regenerate_task(ctx: dict[str, Any]) -> None:
    """CTR-0109 explicit-regeneration task body (PRP-0143 / UDR-0124 D4).

    Registered as ``"session-title-regenerate"`` in CTR-0108 -- a task type of
    its own, so its per-thread dedup (which is what bounds click-spam: no extra
    rate limiter is introduced) is independent of the automatic first-turn
    dispatch.

    Reads the session at RUN time rather than trusting a snapshot taken at
    request time, builds the whole-conversation window, generates, then finalizes
    with ``force=True``. Like the automatic task it always clears
    ``auto_title_pending`` -- including when generation fails -- so the sidebar
    spinner the endpoint started can never be left running (D7), and pushes the
    effective title over CTR-0110.
    """
    thread_id = ctx.get("thread_id")
    model = ctx.get("model")
    if not thread_id:
        return

    from app.agent.temporary import is_temporary

    if is_temporary(thread_id):
        return

    from app.session.storage import read_session_json

    data = read_session_json(thread_id)
    if data is None:
        return  # deleted between dispatch and run
    conversation = build_regeneration_window(data.get("messages") or [])

    title = ""
    if conversation:
        try:
            title = await _generate_title_from_conversation(conversation, model)
        except Exception:
            # Keep the existing title, but still clear the spinner below.
            logger.warning("title regeneration failed for session %s", thread_id, exc_info=True)

    effective = _finalize(thread_id, title or None, force=True)
    if effective is not None:
        from app.notifications import hub

        await hub.broadcast({"type": "session_title", "thread_id": thread_id, "title": effective})


register_task("session-title", run_session_title_task)
register_task("session-title-clear", run_session_title_clear_task)
register_task("session-title-regenerate", run_session_title_regenerate_task)
