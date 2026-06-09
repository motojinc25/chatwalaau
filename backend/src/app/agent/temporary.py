"""Temporary Chat session policy (CTR-0106, PRP-0076, UDR-0052).

An ephemeral, "incognito-style" conversation that:

- is excluded from the user-facing history (sidebar list / search) -- it lives
  in a SEPARATE ``.temporary/`` quarantine directory, never in ``.sessions/``;
- runs de-personalized -- the effective system prompt is the Global Agent
  Identity block alone (slot #1; no Memory Block, no capability guidance text),
  while tools other than the memory tool stay available;
- never touches the ChatWalaʻau built-in learning loop -- it reads no User
  Preference Memory snapshot and ``manage_user_memory`` no-ops for the run;
- is short-term retained for safety / abuse monitoring and unavoidable
  operational display needs, then auto-deleted after
  ``TEMPORARY_CHAT_RETENTION_DAYS`` (default 30); and
- does not carry across conversations -- the SPA never persists the ``temp_``
  thread id, so closing / leaving / reloading loses the context.

This module owns the convention (the ``temp_`` thread-id prefix, the
``.temporary/`` directory), the retention sweep, and the per-run ``temporary``
contextvar. The directory I/O routing is implemented in CTR-0014
(``app.session.storage`` / ``app.session.provider``); the Identity-only prompt
is assembled via CTR-0104 (empty capability + no memory); the memory tool is
CTR-0105. No new Capability and no new Protocol seam (UDR-0052 D1).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from pathlib import Path
import shutil
import time
import uuid

from app.core.config import settings

logger = logging.getLogger(__name__)

# A Temporary Chat thread id is "temp_<uuid>" (mirrors the resp_ API-session
# convention, CTR-0058). The prefix is the single backend routing key.
TEMP_THREAD_PREFIX = "temp_"

# Quarantine directory name (UDR-0052 D3). Co-located as a sibling of
# SESSIONS_DIR so a custom SESSIONS_DIR keeps the quarantine on the same volume.
# Not env-configurable (UDR-0052 D9); only the retention period is a knob.
TEMPORARY_DIRNAME = ".temporary"

# Per-run flag marking the current run as a Temporary Chat run. Set by the
# AG-UI endpoint (CTR-0009) / OpenAI API (CTR-0057) before agent.run; read by
# manage_user_memory (CTR-0105) so it no-ops, and by the endpoint so it skips
# the Memory Block snapshot. Mirrors the image-gen thread_id contextvar pattern.
_temporary_run: contextvars.ContextVar[bool] = contextvars.ContextVar("temporary_run", default=False)


def is_temporary(thread_id: str | None) -> bool:
    """True when ``thread_id`` denotes a Temporary Chat thread."""
    return bool(thread_id) and thread_id.startswith(TEMP_THREAD_PREFIX)


def new_temporary_thread_id() -> str:
    """Mint a fresh ``temp_<uuid>`` thread id (used by the OpenAI API, CTR-0057)."""
    return f"{TEMP_THREAD_PREFIX}{uuid.uuid4().hex}"


def temporary_dir() -> Path:
    """Return the quarantine directory (sibling of SESSIONS_DIR)."""
    return Path(settings.sessions_dir).parent / TEMPORARY_DIRNAME


def temporary_path(thread_id: str) -> Path:
    """Return the quarantine JSON file path for a temporary thread."""
    return temporary_dir() / f"{thread_id}.json"


def set_temporary_run(value: bool) -> None:
    """Mark / unmark the current run as a Temporary Chat run (CTR-0106)."""
    _temporary_run.set(value)


def in_temporary_run() -> bool:
    """True when the current run was flagged temporary (read by the memory tool)."""
    return _temporary_run.get()


def sweep_temporary(retention_days: int | None = None) -> int:
    """Best-effort delete quarantine entries older than the retention period.

    Deletes ``.temporary/temp_*.json`` files and ``.uploads/temp_*/`` upload
    subdirectories whose last-modified age exceeds the retention period (UDR-0052
    D4). Returns the number of entries deleted. A failed delete logs a WARNING
    and is retried on the next sweep; this function never raises and never blocks
    startup. ``retention_days <= 0`` disables deletion (entries kept).
    """
    days = settings.temporary_chat_retention_days if retention_days is None else retention_days
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    deleted = 0

    qdir = temporary_dir()
    if qdir.is_dir():
        for path in qdir.glob(f"{TEMP_THREAD_PREFIX}*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted += 1
            except OSError:
                logger.warning("Could not delete expired temporary session %s", path, exc_info=True)

    uploads = Path(settings.upload_dir)
    if uploads.is_dir():
        for path in uploads.glob(f"{TEMP_THREAD_PREFIX}*"):
            if not path.is_dir():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
                    deleted += 1
            except OSError:
                logger.warning("Could not delete expired temporary uploads %s", path, exc_info=True)

    if deleted:
        logger.info("Temporary Chat sweep removed %d expired quarantine entr(ies)", deleted)
    return deleted


# Hold strong references to in-flight opportunistic sweep tasks so they are not
# garbage-collected before completion (asyncio keeps only weak references).
_sweep_tasks: set[asyncio.Task[int]] = set()


def schedule_sweep() -> None:
    """Fire-and-forget the retention sweep off the event loop (CTR-0106, UDR-0052 D4).

    Used opportunistically when a new temporary thread is created so retention
    holds on long-running hosts, not only at startup. Best-effort and
    non-blocking; ``sweep_temporary`` never raises.
    """
    task = asyncio.create_task(asyncio.to_thread(sweep_temporary))
    _sweep_tasks.add(task)
    task.add_done_callback(_sweep_tasks.discard)
