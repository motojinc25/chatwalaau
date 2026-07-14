"""Session metadata index (CTR-0014 v2, PRP-0112 Part 4 / UDR-0091 D2).

The sidebar needs eleven metadata fields per session (title, updated_at,
pinned_at, folder_id, counts, ...). Before PRP-0112 those were obtained by
parsing EVERY session document in full -- message bodies included -- on every
list request, then discarding everything but the metadata. Listing therefore cost
``O(N x filesize)``: proportional to the total size of all conversation history.

This module keeps the same metadata in ``.sessions/index.json`` so a list request
opens (almost) no session files:

    1. ONE os.scandir() of the session directory -- opens nothing.
    2. Serve from the index every entry whose recorded mtime matches the file's.
    3. Re-parse ONLY files whose mtime differs, or that the index does not know.
    4. Drop index entries whose file is gone.
    5. Persist the reconciled index atomically.

Cost becomes ``O(N) stat + O(changed) parse`` -- zero or one parse in the steady
state.

The reconciliation in step 2/3 is the whole design, not an optimization detail
(UDR-0091 D2). Correctness MUST NOT depend on any write path remembering to
update the index: a write-hook-maintained index would rest on every current and
future call site (save / rename / pin / folder assign / import / delete / init /
background auto-title) remembering, and ONE omission would serve a stale title
forever, silently. By re-checking mtime against the filesystem on every read, the
filesystem -- which cannot be forgotten -- stays the source of truth and the index
is only ever an accelerator. A hand-edited file, an external tool, or a crash
mid-write all self-correct on the next list.

A missing or unparseable index is rebuilt by a full scan, i.e. the worst case is
exactly the pre-PRP-0112 behavior, never an error. The index is derived,
disposable state: deleting it is always safe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from app.session.storage import (
    SESSION_INDEX_FILENAME,
    ensure_session_defaults,
    sessions_dir,
    write_json_atomic,
)

logger = logging.getLogger(__name__)

# Bump when the shape of an entry changes; a mismatch discards the file and
# triggers a full rebuild (cheap, and safer than guessing at an old shape).
INDEX_VERSION = 1

# Internal-only key: the source file's mtime_ns at the time the entry was built.
# Stripped before an entry is handed to a caller.
_MTIME_KEY = "_mtime_ns"

# Serializes index WRITES. The backend is a single asyncio process, so this plus
# the atomic temp-file + replace in write_json_atomic is sufficient.
_write_lock = asyncio.Lock()

_IMAGE_GEN_TOOLS = frozenset({"generate_image", "edit_image"})


def index_path() -> Path:
    """Path of the session metadata index."""
    return sessions_dir() / SESSION_INDEX_FILENAME


def _count_images(messages: list[dict[str, Any]]) -> int:
    """Count image_url content entries and generated images across all messages."""
    count = 0
    for msg in messages:
        for c in msg.get("contents", []):
            if isinstance(c, dict) and c.get("type") == "image_url":
                count += 1
        for tc in msg.get("tool_calls", []):
            if tc.get("name") not in _IMAGE_GEN_TOOLS:
                continue
            result = tc.get("result", "")
            if not isinstance(result, str):
                continue
            try:
                parsed = json.loads(result)
                count += len(parsed.get("images", []))
            except (json.JSONDecodeError, TypeError):
                pass
    return count


def read_session_metadata(path: Path) -> dict[str, Any] | None:
    """Parse ONE session file and project it to the list metadata.

    This is the expensive operation the index exists to avoid; after PRP-0112 it
    runs only for files the index does not already know at their current mtime.
    """
    try:
        data = ensure_session_defaults(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read session file: %s", path)
        return None

    messages = data.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    # message_count / image_count are persisted by the write paths, but an
    # externally-edited or legacy file may lack them -- recompute as a fallback so
    # the index never caches a wrong count.
    message_count = data.get("message_count")
    if not isinstance(message_count, int):
        message_count = len(messages)
    image_count = data.get("image_count")
    if not isinstance(image_count, int):
        image_count = _count_images(messages)

    return {
        "thread_id": data.get("thread_id", path.stem),
        "title": data.get("title", ""),
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
        "message_count": message_count,
        "image_count": image_count,
        "pinned_at": data.get("pinned_at"),
        "folder_id": data.get("folder_id"),
        "source": data.get("source", "ag-ui"),
        # Auto Session Title pending state (PRP-0077, CTR-0109): drives the
        # sidebar spinner until the background title task finalizes.
        "auto_title_pending": bool(data.get("auto_title_pending", False)),
    }


def _load_index() -> dict[str, dict[str, Any]]:
    """Read the index, tolerating every failure by returning an empty map.

    A missing / truncated / corrupt / stale-version index simply means "know
    nothing", which makes the next reconcile a full scan -- correct, just slower.
    """
    path = index_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Session index unreadable; rebuilding by full scan: %s", path)
        return {}

    if not isinstance(raw, dict) or raw.get("version") != INDEX_VERSION:
        return {}

    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}

    # Drop anything structurally wrong rather than trusting it.
    return {
        thread_id: entry
        for thread_id, entry in entries.items()
        if isinstance(thread_id, str) and isinstance(entry, dict) and isinstance(entry.get(_MTIME_KEY), int)
    }


async def _persist_index(entries: dict[str, dict[str, Any]]) -> None:
    """Write the index atomically. Failure is logged and swallowed.

    A failed write only costs performance on the next list (the index is derived
    state), so it MUST NOT surface as an error to the caller.
    """
    async with _write_lock:
        try:
            write_json_atomic(index_path(), {"version": INDEX_VERSION, "entries": entries})
        except OSError:
            logger.warning("Failed to persist the session index (non-fatal)", exc_info=True)


async def list_session_metadata() -> list[dict[str, Any]]:
    """Return the metadata of every session, reconciling the index against disk.

    The returned order is unspecified; callers sort (see router.sort_sessions).
    """
    base = sessions_dir()
    if not base.is_dir():
        return []

    cached = _load_index()
    fresh: dict[str, dict[str, Any]] = {}
    reparsed = 0

    with os.scandir(base) as scan:
        for entry in scan:
            # Skip subdirectories (e.g. folders/), non-JSON, and the index itself
            # (UDR-0091 D6 -- otherwise the index would be listed as a broken chat).
            if entry.name == SESSION_INDEX_FILENAME or not entry.name.endswith(".json"):
                continue
            if not entry.is_file():
                continue

            thread_id = entry.name[: -len(".json")]
            try:
                mtime_ns = entry.stat().st_mtime_ns
            except OSError:
                continue

            hit = cached.get(thread_id)
            if hit is not None and hit.get(_MTIME_KEY) == mtime_ns:
                fresh[thread_id] = hit
                continue

            meta = read_session_metadata(Path(entry.path))
            if meta is None:
                continue
            meta[_MTIME_KEY] = mtime_ns
            fresh[thread_id] = meta
            reparsed += 1

    # Rewrite only when the reconciled view actually differs from what is on disk
    # (a steady-state list must not churn the file).
    if fresh != cached:
        logger.debug(
            "Session index reconciled: %d entries, %d reparsed, %d dropped",
            len(fresh),
            reparsed,
            max(0, len(cached) - (len(fresh) - reparsed)),
        )
        await _persist_index(fresh)

    return [{k: v for k, v in meta.items() if k != _MTIME_KEY} for meta in fresh.values()]


async def invalidate() -> None:
    """Delete the index. Purely an escape hatch -- correctness never needs it.

    Kept because the index is disposable by design (UDR-0091 D13) and a test or an
    operator may want to force the full-scan path.
    """
    async with _write_lock:
        try:
            index_path().unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to delete the session index (non-fatal)", exc_info=True)
