"""Live voice usage ledger (PRP-0188 step 3, UDR-0170 D10).

GPT-Live is metered in voice SECONDS, not tokens. The token ledger (CTR-0200) is
token-only by design (UDR-0155), so voice time is kept in its own stream beside it:

    USAGE_DIR/voice/2026-09.jsonl

one line per ended Live session. Like the token ledger it lives OUTSIDE the session
store (UDR-0136 D1), so deleting a chat never deletes the record of what its Live
conversations used. The chat's own ``live_sessions[]`` (CTR-0014) keeps the same facts
for that chat; this stream is what the dashboard aggregates.

A record carries identifiers and numbers only -- never content (UDR-0136 D3):
``ts`` (the end, UTC), ``thread_id``, ``temporary``, ``live_session_id``, ``seconds``
(None when the service never reported them), ``reason`` and ``delegations``.

The token ledger's month reader globs ``USAGE_DIR/*.jsonl`` only, so this sub-directory
can never be read as token records.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo
import json
import logging
from typing import TYPE_CHECKING, Any

from app.demo import is_demo_mode
from app.usage.aggregate import _months_in_range, record_datetime
from app.usage.ledger import _append_line, ledger_dir

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

VOICE_FIELDS: tuple[str, ...] = (
    "ts",
    "thread_id",
    "temporary",
    "live_session_id",
    "seconds",
    "reason",
    "delegations",
)

TEMPORARY_KEY = "(temporary)"


def voice_dir() -> Path:
    return ledger_dir() / "voice"


def append_voice_usage(
    *,
    thread_id: str,
    temporary: bool,
    live_session_id: str,
    seconds: int | None,
    reason: str,
    delegations: int,
    ended_at: datetime | None = None,
) -> None:
    """Append one ended Live session. Best-effort: never fails the close (UDR-0136 D6)."""
    if is_demo_mode():
        return  # Live is never offered in DEMO_MODE; kept for symmetry with CTR-0200.
    try:
        when = (ended_at or datetime.now(UTC)).astimezone(UTC)
        record = {
            "ts": when.isoformat(),
            "thread_id": thread_id,
            "temporary": bool(temporary),
            "live_session_id": live_session_id,
            "seconds": seconds if isinstance(seconds, int) and seconds >= 0 else None,
            "reason": reason,
            "delegations": int(delegations),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        _append_line(voice_dir() / f"{when:%Y-%m}.jsonl", line)
    except Exception:
        logger.warning("voice usage append failed for %s", live_session_id, exc_info=True)


def iter_voice_records(start: date, end: date, tz: tzinfo = UTC) -> tuple[list[dict[str, Any]], int]:
    """Records whose LOCAL end date (in ``tz``) falls in [start, end]; plus skipped lines."""
    records: list[dict[str, Any]] = []
    skipped = 0
    root = voice_dir()
    for stem in _months_in_range(start - timedelta(days=1), end + timedelta(days=1)):
        path = root / f"{stem}.jsonl"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("voice usage month unreadable: %s", path, exc_info=True)
            continue
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(record, dict):
                skipped += 1
                continue
            when = record_datetime(record, tz)
            if when is None or not start <= when.date() <= end:
                continue
            record["_local"] = when
            records.append(record)
    return records, skipped


def summarize_voice(*, start: date, end: date, tz: tzinfo = UTC, tz_name: str = "UTC") -> dict[str, Any]:
    """Totals, per local day and per chat. Seconds a session never reported stay out of
    the sum and are counted in ``sessions_without_seconds`` -- never shown as zero."""
    records, skipped = iter_voice_records(start, end, tz)

    def blank() -> dict[str, Any]:
        return {"seconds": 0, "sessions": 0, "sessions_without_seconds": 0, "delegations": 0}

    def add(bucket: dict[str, Any], record: dict[str, Any]) -> None:
        bucket["sessions"] += 1
        seconds = record.get("seconds")
        if isinstance(seconds, int) and not isinstance(seconds, bool):
            bucket["seconds"] += seconds
        else:
            bucket["sessions_without_seconds"] += 1
        delegations = record.get("delegations")
        if isinstance(delegations, int):
            bucket["delegations"] += delegations

    totals = blank()
    days: dict[str, dict[str, Any]] = {}
    chats: dict[str, dict[str, Any]] = {}
    for record in records:
        add(totals, record)
        day = record["_local"].date().isoformat()
        add(days.setdefault(day, blank()), record)
        key = TEMPORARY_KEY if record.get("temporary") else str(record.get("thread_id") or "(unknown)")
        chat = chats.setdefault(key, blank())
        add(chat, record)
        last = record["_local"].isoformat()
        if last > chat.get("last_ts", ""):
            chat["last_ts"] = last

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "tz": tz_name,
        "totals": totals,
        "days": [{"key": k, **v} for k, v in sorted(days.items())],
        "chats": sorted(
            ({"key": k, **v} for k, v in chats.items()),
            key=lambda c: (-c["seconds"], c["key"]),
        ),
        "skipped_lines": skipped,
    }
