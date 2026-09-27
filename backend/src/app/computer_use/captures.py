"""Capture history (PRP-0189 amendment A3, UDR-0171 D8 as amended, CTR-0233).

Every observation the agent looks at is kept as a PNG, per chat:

    COMPUTER_USE_DIR/captures/<thread_id>/<run_id>-<obs_id>.png
    COMPUTER_USE_DIR/captures/<thread_id>/latest.json   {file, obs, run, window, ts}

Per-chat sub-folders (the traces have none) because a capture belongs to one
conversation: the SPA's live viewer asks for "the latest capture of THIS chat", and
removing a conversation's captures is removing one folder. ``latest.json`` is written
atomically after the PNG, so a reader never sees a pointer to a file that is not there.

The history is for the operator (debugging what the agent saw). It is not sent to the
model -- the retention middleware (CTR-0234) still keeps only the newest screenshot in
the model's context -- and it is not part of the session JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
from typing import Any

logger = logging.getLogger(__name__)

THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
FILE_NAME = re.compile(r"^[0-9a-f]{12}-o[0-9]{1,6}\.png$")
LATEST = "latest.json"


def captures_dir() -> Path:
    from app.core.config import settings

    return Path(settings.computer_use_dir or ".computeruse") / "captures"


def thread_dir(thread_id: str) -> Path | None:
    if not THREAD_ID.match(thread_id or ""):
        return None
    return captures_dir() / thread_id


def save(*, thread_id: str, run_id: str, obs_id: str, png: bytes, window: str) -> str | None:
    """Store one capture and move the chat's ``latest`` pointer. Never raises."""
    directory = thread_dir(thread_id)
    name = f"{run_id}-{obs_id}.png"
    if directory is None or not FILE_NAME.match(name):
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(png)
        pointer = {
            "file": name,
            "obs": obs_id,
            "run": run_id,
            "window": window,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }
        tmp = directory / f".{LATEST}.tmp"
        tmp.write_text(json.dumps(pointer, ensure_ascii=False), encoding="utf-8")
        tmp.replace(directory / LATEST)
    except OSError:
        logger.warning("Computer Use capture could not be saved for %s", thread_id, exc_info=True)
        return None
    return name


def latest(thread_id: str) -> dict[str, Any] | None:
    directory = thread_dir(thread_id)
    if directory is None:
        return None
    try:
        pointer = json.loads((directory / LATEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(pointer, dict) or not FILE_NAME.match(str(pointer.get("file", ""))):
        return None
    return pointer


def file_path(thread_id: str, name: str) -> Path | None:
    """The capture file, or None -- names are validated, never joined raw."""
    directory = thread_dir(thread_id)
    if directory is None or not FILE_NAME.match(name or ""):
        return None
    path = directory / name
    return path if path.is_file() else None


__all__ = ["captures_dir", "file_path", "latest", "save"]
