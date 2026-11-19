"""OpenAI API session integration (CTR-0058, PRP-0030).

Maps response_id (resp_ prefix) to thread_id for unified session storage
via FileHistoryProvider. Tracks response_chain for multi-turn conversations.

PRP-0256 (UDR-0237 D8-D10): ``previous_response_id`` resolves through the response id
index (``app.openai_api.response_index``) for saved and temporary threads alike, and saved
API sessions may expire after ``responses_session_retention_days``.
"""

from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import shutil
import threading
import time
from typing import Any
import uuid

from app.core import config as _config
from app.openai_api import response_index
from app.session.storage import (
    create_session_json,
    iter_session_files,
    read_session_json,
    sessions_dir,
    update_session_json,
)

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


def resolve_thread_id(previous_response_id: str) -> str | None:
    """Find the session thread_id containing the given response_id.

    1. Check if previous_response_id is itself a thread_id (first response in session)
    2. Look it up in the response id index (UDR-0237 D9): saved and temporary threads,
       no scan once the index is complete.
    """
    # Direct match: the response_id IS the thread_id (first response)
    session = _read_session(previous_response_id)
    if session is not None:
        chain = session.get("response_chain", [])
        if previous_response_id in chain or session.get("thread_id") == previous_response_id:
            return previous_response_id

    return response_index.lookup(previous_response_id)


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
    create_session_json(thread_id, data)
    response_index.record(response_id, thread_id)
    logger.info("Created API session %s", thread_id)
    return data


def update_api_session(
    thread_id: str,
    new_response_id: str,
    user_message: dict[str, Any],
    assistant_message: dict[str, Any],
    route: dict[str, str] | None = None,
) -> None:
    """Update an existing API session with new messages and response_id.

    ``route`` is the decision routing outcome of the turn (PRP-0258, UDR-0239 D8), kept for
    the operator under ``routes[response_id]``; never sent back to a model.
    """

    def append(data: dict[str, Any]) -> None:
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
        if route:
            routes = data.get("routes") if isinstance(data.get("routes"), dict) else {}
            routes[new_response_id] = dict(route)
            data["routes"] = routes

        # Update title if empty
        if not data.get("title"):
            for msg in messages:
                if msg.get("role") == "user":
                    for c in msg.get("contents", []):
                        if isinstance(c, dict) and c.get("type") == "text":
                            data["title"] = c.get("text", "")[:100]
                            break
                    break

    # Serialised; an unreadable file raises and is never replaced (UDR-0156 D1/D2).
    if update_session_json(thread_id, append) is None:
        logger.warning("Session not found for update: %s", thread_id)
        return
    response_index.record(new_response_id, thread_id)


# ---- retention of saved API sessions (PRP-0256, UDR-0237 D10) -----------------------

#: The opportunistic sweep runs at most this often.
SWEEP_INTERVAL_SECS = 3600
_last_sweep = 0.0
_sweep_lock = threading.Lock()


def _retention_days() -> int:
    try:
        return max(0, int(getattr(_config.settings, "responses_session_retention_days", 0) or 0))
    except (TypeError, ValueError):
        return 0


def sweep_api_sessions(retention_days: int | None = None) -> int:
    """Delete saved ``source: openai-api`` sessions not updated for ``retention_days``.

    0 keeps everything. App chats are never touched (only ``source == "openai-api"``).
    Never raises; returns how many sessions were deleted.
    """
    days = _retention_days() if retention_days is None else max(0, retention_days)
    if days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=days)
    deleted = 0
    for path in iter_session_files(_sessions_dir()):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("source") != "openai-api":
                continue
            stamp = datetime.fromisoformat(str(data.get("updated_at") or data.get("created_at")))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            if stamp >= cutoff:
                continue
            thread_id = str(data.get("thread_id") or path.stem)
            path.unlink()
            uploads = Path(_config.settings.upload_dir) / thread_id
            if uploads.is_dir():
                shutil.rmtree(uploads, ignore_errors=True)
            response_index.forget_thread(thread_id)
            deleted += 1
        except (OSError, ValueError, TypeError):
            logger.warning("API session retention could not process %s", path, exc_info=True)
    if deleted:
        logger.info("API session retention removed %d session(s) older than %d day(s)", deleted, days)
    return deleted


def sweep_now() -> int:
    """The retention sweep plus index pruning (startup and the hourly opportunistic run)."""
    global _last_sweep
    with _sweep_lock:
        _last_sweep = time.monotonic()
    deleted = sweep_api_sessions()
    try:
        response_index.prune_missing()
    except Exception:
        logger.warning("Response id index pruning failed", exc_info=True)
    return deleted


def sweep_due() -> bool:
    """True at most once per SWEEP_INTERVAL_SECS, and only when retention is on."""
    if _retention_days() <= 0:
        return False
    with _sweep_lock:
        return _last_sweep == 0.0 or time.monotonic() - _last_sweep >= SWEEP_INTERVAL_SECS
