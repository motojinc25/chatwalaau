"""Teams session persistence (CTR-0014, CTR-0140, PRP-0092, UDR-0070 D5).

Persists each Teams turn to the SHARED session storage (SESSIONS_DIR file-based
JSON) -- the SAME store as Web (`ag-ui`) and the OpenAI Responses API (`resp_`)
sessions. A Teams conversation therefore:

- accumulates as ONE chat history (the `teams_` thread id is stable per Teams
  conversation, so consecutive turns chain naturally -- no `previous_response_id`
  is needed, unlike CTR-0057);
- loads its prior turns as model context on the next turn, via the agent's
  FileHistoryProvider (CTR-0014) once `session.metadata["ag_ui_thread_id"]` is set;
- appears in the sidebar / search with a `source: "teams"` badge (peer of the
  `openai-api` "API" badge).

Stored message shape matches the SPA / API persistence:
``{"role": ..., "contents": [{"type": "text", "text": ...}]}`` (CTR-0014).
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any

from app.session.storage import ensure_session_defaults, read_session_json, write_session_json

logger = logging.getLogger(__name__)


def _text_message(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "contents": [{"type": "text", "text": text}]}


def persist_turn(
    thread_id: str,
    *,
    user_text: str,
    assistant_text: str,
    conversation_type: str,
) -> None:
    """Append one Teams turn (user + assistant) to the session file (UDR-0070 D5).

    Creates the session with ``source: "teams"`` on the first turn and appends on
    later turns, keeping the FULL assistant text (including any image markdown so
    the SPA can render generated images from /api/uploads). Best-effort: a storage
    failure is logged and swallowed so it never breaks the Teams reply.
    """
    try:
        now = datetime.now(UTC).isoformat()
        data = read_session_json(thread_id)
        if data is None:
            data = {
                "thread_id": thread_id,
                "title": (user_text or "").strip()[:100],
                "source": "teams",
                "teams_conversation_type": conversation_type,
                "created_at": now,
                "updated_at": now,
                "message_count": 0,
                "image_count": 0,
                "folder_id": None,
                "messages": [],
            }
        data = ensure_session_defaults(data)
        data["source"] = "teams"  # ensure the badge survives older files
        messages = data.get("messages", [])
        user_clean = (user_text or "").strip()
        if user_clean:
            messages.append(_text_message("user", user_clean))
        messages.append(_text_message("assistant", assistant_text or ""))
        data["messages"] = messages
        data["message_count"] = len(messages)
        data["updated_at"] = now
        if not data.get("title") and user_clean:
            data["title"] = user_clean[:100]
        write_session_json(thread_id, data)
        logger.info("Persisted Teams turn to session %s (%d messages)", thread_id, len(messages))
    except Exception:  # persistence must never break the reply
        logger.warning("Failed to persist Teams turn for %s", thread_id, exc_info=True)
