"""OpenAI API session integration (CTR-0058, PRP-0030).

Maps response_id (resp_ prefix) to thread_id for unified session storage
via FileHistoryProvider. Tracks response_chain for multi-turn conversations.
"""

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from typing import Any
import uuid

from app.session.storage import ensure_session_defaults, read_session_json, sessions_dir, write_session_json

logger = logging.getLogger(__name__)


def generate_response_id() -> str:
    """Generate a unique response_id with resp_ prefix."""
    return f"resp_{uuid.uuid4().hex[:24]}"


def _sessions_dir() -> Path:
    return sessions_dir()


def _read_session(thread_id: str) -> dict[str, Any] | None:
    """Read session JSON by thread_id."""
    try:
        return read_session_json(thread_id)
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read session: %s", thread_id)
        return None


def _write_session(thread_id: str, data: dict[str, Any]) -> None:
    """Write session JSON."""
    write_session_json(thread_id, data)


def resolve_thread_id(previous_response_id: str) -> str | None:
    """Find the session thread_id containing the given response_id.

    1. Check if previous_response_id is itself a thread_id (first response in session)
    2. Search sessions for response_chain containing the id
    """
    # Direct match: the response_id IS the thread_id (first response)
    session = _read_session(previous_response_id)
    if session is not None:
        chain = session.get("response_chain", [])
        if previous_response_id in chain or session.get("thread_id") == previous_response_id:
            return previous_response_id

    # Search all sessions for response_chain containing this id
    sessions_path = _sessions_dir()
    if not sessions_path.is_dir():
        return None

    for file in sessions_path.glob("*.json"):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            if previous_response_id in data.get("response_chain", []):
                return data.get("thread_id", file.stem)
        except (json.JSONDecodeError, OSError):
            continue

    return None


def create_api_session(thread_id: str, response_id: str, title: str = "") -> dict[str, Any]:
    """Create a new session for OpenAI API origin."""
    now = datetime.now(UTC).isoformat()
    data = {
        "thread_id": thread_id,
        "title": title[:100] if title else "",
        "source": "openai-api",
        "response_chain": [response_id],
        "latest_response_id": response_id,
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        "image_count": 0,
        "folder_id": None,
        "messages": [],
    }
    _write_session(thread_id, data)
    logger.info("Created API session %s", thread_id)
    return data


def update_api_session(
    thread_id: str,
    new_response_id: str,
    user_message: dict[str, Any],
    assistant_message: dict[str, Any],
) -> None:
    """Update an existing API session with new messages and response_id."""
    data = _read_session(thread_id)
    if data is None:
        logger.warning("Session not found for update: %s", thread_id)
        return
    data = ensure_session_defaults(data)

    messages = data.get("messages", [])
    messages.append(user_message)
    messages.append(assistant_message)
    data["messages"] = messages
    data["message_count"] = len(messages)
    data["updated_at"] = datetime.now(UTC).isoformat()

    # Update response chain
    chain = data.get("response_chain", [])
    chain.append(new_response_id)
    data["response_chain"] = chain
    data["latest_response_id"] = new_response_id

    # Update title if empty
    if not data.get("title"):
        for msg in messages:
            if msg.get("role") == "user":
                for c in msg.get("contents", []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        data["title"] = c.get("text", "")[:100]
                        break
                break

    _write_session(thread_id, data)
