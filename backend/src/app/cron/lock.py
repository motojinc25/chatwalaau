"""Cross-platform single-flight tick lock (CTR-0130 D5 / UDR-0067 D5).

The lock is an atomic exclusive file create (``O_CREAT | O_EXCL``) carrying the
owner PID + ISO timestamp, with a stale-TTL takeover. OS advisory locks
(``fcntl.flock`` / ``msvcrt.locking``) are deliberately NOT used -- their per-OS
behavior diverges; an atomic exclusive create is uniformly portable across
Linux / macOS / Windows.

The scheduler is expected to be single-instance; with multiple processes (or
``uvicorn --workers N``) the lock guarantees a single tick flight at a time.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
import json
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _is_stale(path: Path, ttl_seconds: float) -> bool:
    """A lock is stale when its timestamp is older than ``ttl_seconds``.

    A lock whose contents are unreadable/garbage is treated as stale so a
    corrupted lock can never wedge the scheduler permanently.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - ts).total_seconds()
    return age > ttl_seconds


def acquire(path: Path, *, ttl_seconds: float) -> bool:
    """Try to acquire the lock. Returns True on success.

    On contention, a stale lock (older than ttl) is removed and acquisition is
    retried once; a fresh lock yields False (another flight holds it).
    """
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        if _is_stale(path, ttl_seconds):
            logger.warning("Removing stale cron tick lock: %s", path)
            try:
                path.unlink()
            except OSError:
                return False
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except OSError:
                return False
        else:
            return False
    except OSError:
        return False
    try:
        payload = json.dumps({"pid": os.getpid(), "ts": datetime.now(UTC).isoformat()})
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def release(path: Path) -> None:
    """Release the lock (best-effort)."""
    with contextlib.suppress(OSError):
        path.unlink()


__all__ = ["acquire", "release"]
