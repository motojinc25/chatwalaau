"""Live session persistence (CTR-0014 / CTR-0227, PRP-0188, UDR-0170 D6 / D7).

Everything Live writes goes through ``update_session_json`` -- the single session
write seam (UDR-0156 D1) -- and is idempotent by ``message_id`` (D4), so a retried
commit never duplicates a turn. Live adds only OPTIONAL fields:

- on a message: ``source: "live"`` and ``live: {session_id, kind, ...}``;
- on the session record: ``live_sessions: [{live_session_id, started_at, ...}]``.

A temporary thread (``temp_`` prefix) is routed to the ``.temporary/`` quarantine by
``session_path`` exactly like every other temporary write (CTR-0106).
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any

from app.session.storage import empty_session_record, update_session_json

logger = logging.getLogger(__name__)

LIVE_SOURCE = "live"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def build_message(
    *,
    message_id: str,
    role: str,
    text: str,
    live: dict[str, Any],
    tool_calls: list[dict[str, Any]] | None = None,
    activity_log: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A stored message in the CTR-0014 shape plus the CTR-0227 fields."""
    from app.session.provider import MESSAGE_TYPE_ID

    msg: dict[str, Any] = {
        "type": MESSAGE_TYPE_ID,
        "role": role,
        "contents": [{"type": "text", "text": text}],
        "message_id": message_id,
        "source": LIVE_SOURCE,
        "live": live,
    }
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if activity_log:
        msg["activity_log"] = activity_log
    if usage:
        msg["usage"] = usage
    return msg


def ensure_session(thread_id: str) -> None:
    """Create the session record when the thread has none (a Live-first chat)."""

    def untouched(_data: dict[str, Any]) -> bool:
        return False

    update_session_json(thread_id, untouched, create=lambda: empty_session_record(thread_id))


def append_messages(thread_id: str, messages: list[dict[str, Any]]) -> None:
    """Append committed Live messages; already-stored ids are skipped (D4).

    Raises on an unreadable session file (nothing is written, UDR-0156 D2); the
    controller logs and carries on so a storage fault never kills the call.
    """
    if not messages:
        return

    def append(data: dict[str, Any]) -> bool:
        existing = data.get("messages")
        if not isinstance(existing, list):
            existing = []
        stored = {m.get("message_id") for m in existing if isinstance(m, dict)}
        fresh = [m for m in messages if m["message_id"] not in stored]
        if not fresh:
            return False
        existing.extend(fresh)
        data["messages"] = existing
        data["message_count"] = len(existing)
        data["updated_at"] = _now()
        if not data.get("title"):
            for m in fresh:
                if m["role"] == "user":
                    data["title"] = m["contents"][0]["text"][:100]
                    break
        return True

    update_session_json(thread_id, append, create=lambda: empty_session_record(thread_id))


def start_live_record(thread_id: str, live_session_id: str) -> None:
    """Open this session's ``live_sessions[]`` entry (CTR-0014)."""

    def add(data: dict[str, Any]) -> bool:
        records = data.get("live_sessions")
        if not isinstance(records, list):
            records = []
        if any(isinstance(r, dict) and r.get("live_session_id") == live_session_id for r in records):
            return False
        records.append({"live_session_id": live_session_id, "started_at": _now()})
        data["live_sessions"] = records
        return True

    update_session_json(thread_id, add, create=lambda: empty_session_record(thread_id))


def finish_live_record(
    thread_id: str,
    live_session_id: str,
    *,
    seconds: int | None,
    reason: str,
    delegations: int,
) -> None:
    """Close this session's ``live_sessions[]`` entry with seconds and reason (D10)."""

    def finish(data: dict[str, Any]) -> bool:
        records = data.get("live_sessions")
        if not isinstance(records, list):
            return False
        for record in records:
            if isinstance(record, dict) and record.get("live_session_id") == live_session_id:
                record["ended_at"] = _now()
                if seconds is not None:
                    record["seconds"] = seconds
                record["reason"] = reason
                record["delegations"] = delegations
                return True
        return False

    update_session_json(thread_id, finish)
