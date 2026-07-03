"""Turn-scoped Memory Curation background task (CTR-0163, PRP-0100, UDR-0079).

The per-turn "like" write path for the Agent Curated Memory (CTR-0162). When the
operator gives a chat turn a thumbs-up (CTR-0165 -> CTR-0164), this task runs a
lightweight, post-turn LLM pass over that ONE turn (its user message + the
assistant reply) plus the current ``.agent/MEMORY.md``, RECONCILES the whole memory
(keep / merge duplicates / resolve contradictions / remove outdated / add a
genuinely durable, reusable, non-sensitive entry), and applies the result through
the CTR-0162 guarded ``§``-store write.

Runs only as a CTR-0108 background task (fire-and-forget, error-isolated), so it
never blocks the chat path. Whole-memory reconciliation -- rather than a granular
op diff -- lets the memory JUDGE existing content and avoids fragile substring
matching (the CTR-0117 philosophy, UDR-0079 D7).

Inert in DEMO_MODE and for temporary (``temp_``) threads (UDR-0079 D10). On finish
it updates the per-turn like state (``memory_liked``, CTR-0014) to ``curated`` /
``failed`` and pushes a ``memory_curated`` event over the CTR-0110 notification hub
so the like UI reflects completion in real time.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import re
from typing import Any

from app.agent.agent_memory import ENTRY_DELIMITER, load_agent_memory, parse_entries, write_agent_memory
from app.background import register_task
from app.core.config import settings

logger = logging.getLogger(__name__)

# Reconciliation prompt. The model returns the COMPLETE updated memory (one entry
# per line); the deterministic backend filter and cap are the authoritative guard.
_RECONCILE_SYSTEM_PROMPT = (
    "You maintain a small, durable AGENT MEMORY: the assistant's own curated notes ABOUT THE "
    "WORK -- environment facts, project conventions, tool quirks, and stable operating rules. "
    "You are given the CURRENT memory (one entry per line) and one conversation turn the user "
    "marked as worth remembering. Produce the UPDATED memory as the COMPLETE new list of "
    "entries, one per line. This is a reconciliation of the whole memory, not an append.\n\n"
    "Rules:\n"
    "- KEEP every existing entry that is still valid.\n"
    "- MERGE duplicates and near-duplicates into a single line.\n"
    "- RESOLVE contradictions: keep the most recent / most explicit fact and DROP the other.\n"
    "- REMOVE an entry that is clearly outdated or was reversed.\n"
    "- ADD a line ONLY for a durable, reusable, non-sensitive fact ABOUT THE WORK clearly "
    "present in the turn (a convention, a command, a tool quirk, an operating rule).\n"
    "- Do NOT store user PREFERENCES (those live in a separate user memory), transient work "
    "logs, one-off requests, long code, raw tool output, or ANY secret/credential or sensitive "
    "personal data.\n"
    "- Keep each entry to 1-2 concise sentences; keep the whole list short.\n"
    "- If nothing should change, output the CURRENT memory unchanged.\n\n"
    "Output ONLY the memory entries, one per line. No numbering, no bullets, no prose, no code "
    "fences."
)

_USER_TEMPLATE = "CURRENT MEMORY:\n{memory}\n\nLIKED TURN:\n{turn}\n\nUPDATED MEMORY:"

# Bound the turn text fed to the model.
_TURN_CHAR_CAP = 8000
# Defensive upper bound on the reconciled entry count (the cap is authoritative).
_MAX_LINES = 40


def _default_model() -> str:
    """Resolve the fallback reconciliation model (honors DEMO_MODE; mirrors CTR-0117)."""
    from app.demo import is_demo_mode

    if is_demo_mode():
        from app.demo import resolve_demo_models

        models = resolve_demo_models()
        return models[0] if models else "chatwalaau-demo"
    from app import providers

    resolved = providers.resolve_models()
    return resolved[0][0] if resolved else ""


def _parse_memory_lines(raw: str) -> list[str]:
    """Parse the model output into a clean list of memory entries (tolerant)."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        text = text.removesuffix("```").strip()
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
                    value = item.get("content") or item.get("entry") or ""
                    if isinstance(value, str):
                        collected.append(value)
            text = "\n".join(collected)
    out: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "§":  # never let the delimiter line survive as an entry
            continue
        if line.startswith(("- ", "* ", "+ ")):
            line = line[2:].strip()
        line = re.sub(r"^\d+[.)]\s+", "", line)  # strip "1. " / "1) " numbering
        if line:
            out.append(line)
    return out[:_MAX_LINES]


async def _reconcile(memory_body: str, turn_text: str, model: str | None) -> list[str]:
    """Run a single non-streaming completion and return the reconciled entries."""
    from agent_framework import Message

    from app.agui.agent_registry import _build_chat_client

    reconcile_model = settings.agent_memory_curation_model.strip() or (model or "") or _default_model()
    client = _build_chat_client(reconcile_model)
    prompt = _USER_TEMPLATE.format(memory=memory_body or "(empty)", turn=turn_text)
    messages = [
        Message(role="system", contents=[_RECONCILE_SYSTEM_PROMPT]),
        Message(role="user", contents=[prompt]),
    ]
    response = await client.get_response(messages, stream=False)
    return _parse_memory_lines(getattr(response, "text", "") or "")


# ---------------------------------------------------------------------------
# Per-turn like state (memory_liked, CTR-0014). Shared by the router and this task.
# ---------------------------------------------------------------------------
def set_liked(thread_id: str, turn_key: str, status: str) -> None:
    """Upsert a ``memory_liked`` entry for ``turn_key`` (RMW; best-effort)."""
    from app.session.storage import read_session_json, write_session_json

    data = read_session_json(thread_id)
    if data is None:
        return
    liked = data.get("memory_liked")
    if not isinstance(liked, list):
        liked = []
    updated = [e for e in liked if isinstance(e, dict) and e.get("turn_key") != turn_key]
    updated.append({"turn_key": turn_key, "status": status})
    data["memory_liked"] = updated
    data["updated_at"] = datetime.now(UTC).isoformat()
    try:
        write_session_json(thread_id, data)
    except OSError:
        logger.warning("Could not persist memory_liked for session %s", thread_id, exc_info=True)


def clear_liked(thread_id: str, turn_key: str) -> None:
    """Remove the ``memory_liked`` entry for ``turn_key`` (RMW; best-effort)."""
    from app.session.storage import read_session_json, write_session_json

    data = read_session_json(thread_id)
    if data is None:
        return
    liked = data.get("memory_liked")
    if not isinstance(liked, list):
        return
    updated = [e for e in liked if isinstance(e, dict) and e.get("turn_key") != turn_key]
    if len(updated) == len(liked):
        return
    data["memory_liked"] = updated
    data["updated_at"] = datetime.now(UTC).isoformat()
    try:
        write_session_json(thread_id, data)
    except OSError:
        logger.warning("Could not clear memory_liked for session %s", thread_id, exc_info=True)


async def _push(thread_id: str, turn_key: str, status: str) -> None:
    """Broadcast a ``memory_curated`` event over the CTR-0110 hub (best-effort)."""
    from app.notifications import hub

    await hub.broadcast({"type": "memory_curated", "thread_id": thread_id, "turn_key": turn_key, "status": status})


async def run_memory_curate_task(ctx: dict[str, Any]) -> None:
    """CTR-0163 task body. Registered as ``"memory-curate"`` in CTR-0108.

    ctx carries: ``thread_id``, ``turn_key``, ``user_text``, ``assistant_text``,
    and (optional) ``model``.
    """
    if not settings.agent_memory_enabled:
        logger.info("memory curation skipped: AGENT_MEMORY_ENABLED is false")
        return

    thread_id = ctx.get("thread_id")
    turn_key = ctx.get("turn_key")
    user_text = (ctx.get("user_text") or "").strip()
    assistant_text = (ctx.get("assistant_text") or "").strip()
    model = ctx.get("model")
    if not thread_id or not turn_key or not (user_text or assistant_text):
        logger.info(
            "memory curation skipped: missing thread_id/turn_key/turn text (thread=%s turn=%s)", thread_id, turn_key
        )
        return

    logger.info(
        "memory curation START: thread=%s turn=%s (user_chars=%d assistant_chars=%d)",
        thread_id,
        turn_key,
        len(user_text),
        len(assistant_text),
    )

    # Skip temporary chats (UDR-0052 D7) and DEMO_MODE (no real LLM; never mutate
    # the operator memory from scripted demo content).
    from app.agent.temporary import is_temporary
    from app.demo import is_demo_mode

    if is_temporary(thread_id) or is_demo_mode():
        logger.info(
            "memory curation acked without a write (temporary chat or DEMO_MODE): thread=%s turn=%s",
            thread_id,
            turn_key,
        )
        set_liked(thread_id, turn_key, "curated")  # ack without a write in demo/temp
        await _push(thread_id, turn_key, "curated")
        return

    turn_text = f"User: {user_text}\nAssistant: {assistant_text}".strip()
    if len(turn_text) > _TURN_CHAR_CAP:
        turn_text = turn_text[:_TURN_CHAR_CAP]

    status = "failed"
    try:
        memory_body = load_agent_memory()
        current_entries = parse_entries(memory_body)
        reconciled = _parse_memory_lines("\n".join(current_entries))
        resolved_model = settings.agent_memory_curation_model.strip() or (model or "") or "<provider-default>"
        logger.info(
            "memory curation reconciling: thread=%s turn=%s model=%s current_entries=%d",
            thread_id,
            turn_key,
            resolved_model,
            len(current_entries),
        )
        proposed = await _reconcile(ENTRY_DELIMITER.join(current_entries), turn_text, model)
        changed = bool(proposed and proposed != reconciled)
        logger.info(
            "memory curation reconciled: thread=%s turn=%s proposed_entries=%d changed=%s",
            thread_id,
            turn_key,
            len(proposed),
            changed,
        )
        # Guard: never WIPE the memory on an empty/garbled rewrite; skip a write
        # when the reconciliation is a no-op (avoid churn + a needless backup).
        if changed:
            result = write_agent_memory(proposed)
            if result.get("status") == "applied":
                logger.info(
                    "memory curation WROTE .agent/MEMORY.md: thread=%s turn=%s entries=%s",
                    thread_id,
                    turn_key,
                    result.get("entries"),
                )
            else:
                logger.warning(
                    "memory curation write rejected: thread=%s turn=%s reason=%s",
                    thread_id,
                    turn_key,
                    result.get("reason"),
                )
        else:
            logger.info("memory curation no-op (nothing new to remember): thread=%s turn=%s", thread_id, turn_key)
        status = "curated"
    except Exception:
        logger.warning("memory curation FAILED: thread=%s turn=%s", thread_id, turn_key, exc_info=True)
        status = "failed"

    set_liked(thread_id, turn_key, status)
    await _push(thread_id, turn_key, status)
    logger.info("memory curation DONE: thread=%s turn=%s status=%s", thread_id, turn_key, status)


register_task("memory-curate", run_memory_curate_task)
