"""User Memory Extraction background task (CTR-0117, PRP-0079, UDR-0051 Phase 2).

Resolves the UDR-0051 D5 deferral. Periodically RECONCILES the durable User
Preference Memory (``.agent/USER.md``) against the recent conversation: it merges
duplicates, resolves contradictions, drops reversed / outdated preferences, and
integrates genuinely new ones -- so the memory actively JUDGES existing content
instead of only appending.

It does this by asking the model to return the COMPLETE updated profile (a
reconciliation, not a diff) and applying it as a SINGLE ``consolidate`` through
the existing CTR-0105 guarded write primitive (``apply_memory_operation`` -- the
same deterministic secret/PII filter, ``USER_CHAR_LIMIT`` cap, and backup-on-write
as the inline ``manage_user_memory`` tool). Full-profile reconciliation avoids the
fragile exact-line matching that an add/replace/remove diff requires, so modify /
delete actually happen (the original add-biased deficiency).

Runs only as a CTR-0108 background task (post-turn, fire-and-forget,
error-isolated), so reconciliation never blocks or fails the chat turn. Gated by
``USER_MEMORY_EXTRACTION`` (default false) AND ``USER_PROFILE_ENABLED``; the
default is byte-for-byte Phase 1 behavior. Throttled by
``USER_MEMORY_EXTRACTION_EVERY_N_TURNS`` via the additive ``memory_extracted_index``
session-metadata cursor (CTR-0014): below the threshold the task is a cheap no-op
(no LLM call). ``temp_`` chats (UDR-0052 D7) and DEMO_MODE never reconcile.

Safety: the reconciled profile is written only when it is non-empty (so a bad
rewrite can never WIPE the memory) and differs from the current one (so a stable
profile causes no churn); every write backs up the prior file first (CTR-0105 D8),
and the running session's FROZEN snapshot (UDR-0051 D3) is untouched -- changes
take effect from the NEXT session.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.background import register_task
from app.core.config import settings

logger = logging.getLogger(__name__)

# Reconciliation prompt. The model returns the COMPLETE updated profile (one
# preference per line); the deterministic backend filter and cap are the
# authoritative guard regardless of what the model returns.
_RECONCILE_SYSTEM_PROMPT = (
    "You maintain a small, durable User Preference Memory. You are given the "
    "CURRENT profile (one preference per line) and a recent slice of conversation. "
    "Produce the UPDATED profile as the COMPLETE new list of preferences, one per "
    "line. This is a reconciliation of the whole profile, not an append.\n\n"
    "Rules:\n"
    "- KEEP every existing preference that is still valid.\n"
    "- MERGE duplicates and near-duplicates into a single line.\n"
    "- RESOLVE contradictions: when two preferences conflict, keep the one the "
    "user most recently or most explicitly expressed and DROP the other.\n"
    "- REMOVE a preference the user has explicitly reversed or that is clearly "
    "outdated.\n"
    "- ADD a line ONLY for a durable, reusable, user-scoped, non-sensitive "
    "preference clearly expressed in the conversation (stable preferences, "
    "communication style, expectations, recurring workflow habits).\n"
    "- Do NOT invent preferences. Do NOT include one-off instructions, the "
    "transient topic of this chat, project-specific facts, file paths, or ANY "
    "secret/credential or sensitive personal data (passwords, API keys, tokens, "
    "private keys, government IDs, credit cards, exact addresses, health, "
    "political, religious, or ethnicity information).\n"
    "- Keep it concise (a handful of short lines).\n"
    "- If nothing should change, output the CURRENT profile unchanged.\n\n"
    "Output ONLY the profile lines, one preference per line. No numbering, no "
    "bullets, no prose, no code fences."
)

_USER_TEMPLATE = "CURRENT PROFILE:\n{profile}\n\nRECENT CONVERSATION:\n{transcript}\n\nUPDATED PROFILE:"

# Bound the transcript fed to the model (the tail is kept when over budget).
_TRANSCRIPT_CHAR_CAP = 8000
# A sane upper bound on the reconciled line count (defensive; the cap is the
# authoritative size guard in CTR-0105).
_MAX_LINES = 40


def _message_text(message: dict[str, Any]) -> str:
    """Flatten an AG-UI message's content (str or list of parts) to text."""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts).strip()
    return ""


def _user_turn_count(messages: list[dict[str, Any]]) -> int:
    """Number of user-role messages in the conversation."""
    return sum(1 for m in messages if m.get("role") == "user")


def _slice_since(messages: list[dict[str, Any]], cursor_user_turns: int) -> list[dict[str, Any]]:
    """Return messages from the start of the (cursor+1)-th user turn onward.

    ``cursor_user_turns`` is the number of user turns already considered. The slice
    is the new, not-yet-considered tail of the conversation.
    """
    if cursor_user_turns <= 0:
        return messages
    seen = 0
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            seen += 1
            if seen > cursor_user_turns:
                return messages[index:]
    return []


def _build_transcript(messages: list[dict[str, Any]], assistant_text: str) -> str:
    """Render the new slice (+ the latest assistant reply) as a capped transcript."""
    lines: list[str] = []
    for message in messages:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(message)
        if text:
            lines.append(f"{'User' if role == 'user' else 'Assistant'}: {text}")
    if assistant_text:
        lines.append(f"Assistant: {assistant_text}")
    transcript = "\n".join(lines)
    if len(transcript) > _TRANSCRIPT_CHAR_CAP:
        # Keep the most recent content (the tail) within budget.
        transcript = transcript[-_TRANSCRIPT_CHAR_CAP:]
    return transcript


def _parse_profile_lines(raw: str) -> list[str]:
    """Parse the model output into a clean list of preference lines.

    Tolerant: strips code fences, leading bullets / numbering, and accepts a JSON
    array of strings (or {content|preference} objects) if the model returns one
    despite the plain-line instruction. Any failure yields an empty list (which
    the caller treats as a safe no-op -- it never wipes the profile).
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        text = text.removesuffix("```").strip()
    # Tolerate a JSON array (of strings or {content}/{preference} objects).
    if text.startswith("["):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, list):
            collected: list[str] = []
            for item in data:
                if isinstance(item, str):
                    collected.append(item)
                elif isinstance(item, dict):
                    value = item.get("content") or item.get("preference") or ""
                    if isinstance(value, str):
                        collected.append(value)
            text = "\n".join(collected)
    out: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(("- ", "* ", "+ ")):
            line = line[2:].strip()
        line = re.sub(r"^\d+[.)]\s+", "", line)  # strip "1. " / "1) " numbering
        if line:
            out.append(line)
    return out[:_MAX_LINES]


async def _reconcile(profile: str, transcript: str, model: str | None) -> list[str]:
    """Run a single non-streaming completion and return the reconciled lines."""
    from agent_framework import Message

    from app.agui.agent_registry import _build_chat_client
    from app.models_catalog import resolve_task_model

    reconcile_model = resolve_task_model("user_memory_extraction", model)
    client = _build_chat_client(reconcile_model)
    prompt = _USER_TEMPLATE.format(profile=profile, transcript=transcript)
    messages = [
        Message(role="system", contents=[_RECONCILE_SYSTEM_PROMPT]),
        Message(role="user", contents=[prompt]),
    ]
    response = await client.get_response(messages, stream=False)
    return _parse_profile_lines(getattr(response, "text", "") or "")


def _advance_cursor(thread_id: str, user_turns: int) -> None:
    """Persist ``memory_extracted_index`` so the same turns are not reprocessed.

    Read-modify-write right before writing (CTR-0014). A no-op if the session was
    deleted between dispatch and write. Best-effort: a lost update (racing the
    frontend save) only causes one extra pass later.
    """
    from app.session.storage import read_session_json, write_session_json

    data = read_session_json(thread_id)
    if data is None:
        return
    if data.get("memory_extracted_index") == user_turns:
        return
    data["memory_extracted_index"] = user_turns
    try:
        write_session_json(thread_id, data)
    except OSError:
        logger.warning("Could not persist memory cursor for session %s", thread_id, exc_info=True)


async def run_user_memory_extract_task(ctx: dict[str, Any]) -> None:
    """CTR-0117 task body. Registered as ``"user-memory-extract"`` in CTR-0108.

    ctx carries: ``thread_id``, ``messages`` (the AG-UI request history),
    ``assistant_text`` (the just-produced reply), and ``model``.
    """
    if not (settings.user_profile_enabled and settings.user_memory_extraction):
        return

    thread_id = ctx.get("thread_id")
    messages = ctx.get("messages") or []
    assistant_text = (ctx.get("assistant_text") or "").strip()
    model = ctx.get("model")
    if not thread_id or not isinstance(messages, list):
        return

    # Skip temporary chats (UDR-0052 D7) and DEMO_MODE (no real LLM; never mutate
    # the operator profile from scripted demo content).
    from app.agent.temporary import is_temporary
    from app.demo import is_demo_mode

    if is_temporary(thread_id) or is_demo_mode():
        return

    user_turns = _user_turn_count(messages)
    every_n = max(1, settings.user_memory_extraction_every_n_turns)

    from app.session.storage import read_session_json

    data = read_session_json(thread_id)
    raw_cursor = (data or {}).get("memory_extracted_index") or 0
    cursor = raw_cursor if isinstance(raw_cursor, int) and not isinstance(raw_cursor, bool) else 0
    # Self-heal a cursor left ahead of the (edited/truncated) conversation.
    cursor = min(cursor, user_turns)
    if user_turns - cursor < every_n:
        return  # cadence gate: not enough new turns -> cheap no-op, no LLM call

    from app.agent.user_memory import apply_memory_operation, load_user_profile

    profile = load_user_profile()
    transcript = _build_transcript(_slice_since(messages, cursor), assistant_text)
    if not transcript:
        _advance_cursor(thread_id, user_turns)
        return

    reconciled = await _reconcile(profile, transcript, model)
    current = _parse_profile_lines(profile)

    # Guards: never WIPE the profile on an empty/garbled rewrite, and skip a write
    # when the reconciliation is a no-op (avoid churn + a needless backup).
    if reconciled and reconciled != current:
        result = apply_memory_operation("consolidate", content="\n".join(reconciled))
        try:
            status = json.loads(result).get("status")
        except (json.JSONDecodeError, ValueError):
            status = None
        if status == "applied":
            logger.info(
                "User memory reconciled for session %s (%d -> %d lines)",
                thread_id,
                len(current),
                len(reconciled),
            )
        else:
            logger.debug("memory reconciliation rejected for %s: %s", thread_id, result)

    # Advance the cursor whether or not anything changed (the slice was
    # considered), so the same messages are never reprocessed.
    _advance_cursor(thread_id, user_turns)


register_task("user-memory-extract", run_user_memory_extract_task)
