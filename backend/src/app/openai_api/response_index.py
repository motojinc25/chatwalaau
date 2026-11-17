"""Response id index (CTR-0058 v2, PRP-0256, UDR-0237 D9).

``previous_response_id`` used to be resolved by opening every session file. This index maps
``response_id -> thread_id`` for saved (``.sessions/``) and temporary (``.temporary/``)
API threads, in ``response_index.json`` beside the sessions directory.

- Written by the only two writers of a ``response_chain`` (``create_api_session`` and
  ``update_api_session``), under a process lock.
- Missing (first use after an upgrade, or deleted): built ONCE by one scan of both
  directories and marked complete. After that a miss is a 404 without a scan, so unknown
  ids cannot force full scans.
- A hit is verified against the session file (the id must be in its chain); an entry whose
  file is gone is dropped. Deleting the file is always safe: it is rebuilt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import threading
from typing import Any

from app.core import config as _config

logger = logging.getLogger(__name__)

INDEX_FILENAME = "response_index.json"
_lock = threading.RLock()
#: Loaded indexes by file path (tests switch SESSIONS_DIR).
_loaded: dict[str, dict[str, str]] = {}


def index_path() -> Path:
    return Path(_config.settings.sessions_dir).parent / INDEX_FILENAME


def _thread_files() -> list[Path]:
    from app.agent.temporary import TEMP_THREAD_PREFIX, temporary_dir
    from app.session.storage import iter_session_files, sessions_dir

    files = list(iter_session_files(sessions_dir()))
    qdir = temporary_dir()
    if qdir.is_dir():
        files.extend(qdir.glob(f"{TEMP_THREAD_PREFIX}*.json"))
    return files


def _scan() -> dict[str, str]:
    entries: dict[str, str] = {}
    for path in _thread_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        thread_id = str(data.get("thread_id") or path.stem)
        for response_id in data.get("response_chain") or []:
            entries[str(response_id)] = thread_id
    return entries


def _write(path: Path, entries: dict[str, str]) -> None:
    from app.session.storage import write_json_atomic

    try:
        write_json_atomic(path, {"complete": True, "entries": entries})
    except OSError:
        logger.warning("Could not write the response id index %s", path, exc_info=True)


def _entries() -> dict[str, str]:
    """The loaded index (built by one scan when the file is missing or unreadable)."""
    path = index_path()
    key = str(path)
    entries = _loaded.get(key)
    if entries is not None:
        return entries
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("complete") is True and isinstance(raw.get("entries"), dict):
            entries = {str(k): str(v) for k, v in raw["entries"].items()}
    except (OSError, ValueError):
        entries = None
    if entries is None:
        entries = _scan()
        logger.info("Response id index built: %d response id(s)", len(entries))
        _write(path, entries)
    _loaded[key] = entries
    return entries


def record(response_id: str, thread_id: str) -> None:
    with _lock:
        entries = _entries()
        if entries.get(response_id) == thread_id:
            return
        entries[response_id] = thread_id
        _write(index_path(), entries)


def _chain_has(thread_id: str, response_id: str) -> bool:
    from app.session.storage import read_session_json

    try:
        data = read_session_json(thread_id)
    except Exception:
        return False
    return bool(data) and (response_id in (data.get("response_chain") or []) or data.get("thread_id") == response_id)


def lookup(response_id: str) -> str | None:
    """The thread holding ``response_id``, or None (no scan once the index is complete)."""
    with _lock:
        entries = _entries()
        thread_id = entries.get(response_id)
        if thread_id is None:
            return None
        if _chain_has(thread_id, response_id):
            return thread_id
        del entries[response_id]
        _write(index_path(), entries)
        return None


def forget_thread(thread_id: str) -> None:
    with _lock:
        entries = _entries()
        stale = [rid for rid, tid in entries.items() if tid == thread_id]
        if stale:
            for rid in stale:
                del entries[rid]
            _write(index_path(), entries)


def prune_missing() -> int:
    """Drop entries whose session file is gone (after the sweeps). Returns how many."""
    from app.session.storage import session_path

    with _lock:
        entries = _entries()
        gone = {tid for tid in set(entries.values()) if not session_path(tid).is_file()}
        stale = [rid for rid, tid in entries.items() if tid in gone]
        for rid in stale:
            del entries[rid]
        if stale:
            _write(index_path(), entries)
        return len(stale)


def reset_cache() -> None:
    """Forget the loaded indexes (tests)."""
    with _lock:
        _loaded.clear()


__all__ = ["INDEX_FILENAME", "forget_thread", "index_path", "lookup", "prune_missing", "record", "reset_cache"]
