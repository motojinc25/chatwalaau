"""Harness session snapshots (CTR-0279, PRP-0243, UDR-0224).

A harness conversation's working state -- history, todo list, mode, loop state and
the compaction marks on the stored history -- lives in ONE MAF ``AgentSession``
(UDR-0119 D4). This module saves that whole session at the end of each turn and
restores it when the runtime cache has to build the conversation again (a restart,
an LRU eviction, ``clear_cache()``), so the agent resumes where it stopped.

Layout (D3): next to the chat's session file,
``<session file without .json>.harness/<percent-encoded harness_id>.json`` -- a
DIRECTORY, so ``iter_session_files()`` (top-level ``*.json``) never lists it as a chat.

This is the ONE module under ``app/`` allowed to persist MAF ``Message`` records
(UDR-0224 D6, the named exception to UDR-0212 D4.3), and only inside the envelope
below. Nothing here may fail a turn: a save or restore error is logged and the
conversation continues (save skipped, or a fresh session).
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import shutil
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from app.session.storage import session_path, write_json_atomic

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

FORMAT = "chatwalaau.harness-session"
FORMAT_VERSION = 1
SNAPSHOT_DIR_SUFFIX = ".harness"
# A WARNING above this size: compacted messages stay stored (marked), so a very long
# conversation grows its snapshot (PRP-0243 Section 9).
SIZE_WARNING_BYTES = 50 * 1024 * 1024


def snapshot_dir(thread_id: str) -> Path:
    """``.sessions/<thread_id>.harness`` (``.temporary/`` for a temporary chat)."""
    path = session_path(thread_id)
    return path.with_name(path.stem + SNAPSHOT_DIR_SUFFIX)


def snapshot_path(thread_id: str, harness_id: str) -> Path:
    """One file per harness id: a conversation may switch harness (UDR-0119 D11)."""
    return snapshot_dir(thread_id) / f"{quote(harness_id, safe='')}.json"


def _maf_version() -> str:
    try:
        import agent_framework

        return str(getattr(agent_framework, "__version__", ""))
    except Exception:  # pragma: no cover - the framework is always importable here
        return ""


def save(
    thread_id: str,
    harness_id: str,
    session: Any,
    *,
    spec_fingerprint: str,
    model_id: str,
) -> bool:
    """Write the whole session atomically (D2). Never raises; True when written.

    Synchronous on purpose: it runs in the endpoint's ``finally``, where an async
    generator may not await during GeneratorExit (a user Stop or a dropped stream).
    """
    path = snapshot_path(thread_id, harness_id)
    try:
        envelope = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "harness_id": harness_id,
            "spec_fingerprint": spec_fingerprint,
            "model_id": model_id,
            "maf_version": _maf_version(),
            "saved_at": datetime.now(UTC).isoformat(),
            "session": session.to_dict(),
        }
        write_json_atomic(path, envelope)
        size = path.stat().st_size
        if size > SIZE_WARNING_BYTES:
            logger.warning(
                "[harness session] thread=%s harness=%s snapshot is %d bytes (above %d)",
                thread_id,
                harness_id,
                size,
                SIZE_WARNING_BYTES,
            )
        else:
            logger.debug("[harness session] thread=%s harness=%s saved %d bytes", thread_id, harness_id, size)
        return True
    except Exception:
        logger.exception("[harness session] thread=%s harness=%s could not be saved to %s", thread_id, harness_id, path)
        return False


def _set_aside(path: Path, reason: str) -> None:
    """Rename an unusable snapshot to ``<name>.corrupt-<timestamp>.json`` (D4.4)."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.stem}.corrupt-{stamp}.json")
    logger.warning("[harness session] snapshot %s is unusable (%s); set aside as %s", path, reason, target.name)
    try:
        path.replace(target)
    except OSError:
        logger.warning("[harness session] could not set aside %s", path, exc_info=True)


def load(thread_id: str, harness_id: str) -> dict[str, Any] | None:
    """Return the snapshot envelope with ``session`` as an ``AgentSession``, or None.

    None for an absent file AND for an unusable one; the latter is set aside first
    so the next turn does not trip over it again (D4.4). Never raises.
    """
    path = snapshot_path(thread_id, harness_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _set_aside(path, f"unreadable: {type(exc).__name__}")
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("format_version") != FORMAT_VERSION:
        _set_aside(path, "unknown format")
        return None
    try:
        from agent_framework import AgentSession

        data["session"] = AgentSession.from_dict(data["session"])
    except Exception as exc:
        _set_aside(path, f"not restorable: {type(exc).__name__}")
        return None
    return data


def history_only(session: Any, history_source_id: str, *, strip_reasoning: bool) -> Any:
    """The restored session reduced to its history (D4.2 / D4.3), in place.

    The todo list, the mode and the loop state start from the YAML again. The session
    OBJECT is kept rather than rebuilt with ``AgentSession(session_id=...)``: its id is
    the one MAF generated for the conversation, which keeps the file memory in scope
    (UDR-0224 D7), and no custom id is ever constructed (UDR-0162 D3). When the model
    changed, ``text_reasoning`` contents are dropped: reasoning items and thinking
    signatures are bound to the model that produced them (UDR-0141 D7).
    """
    history = session.state.get(history_source_id)
    session.state = {}
    if isinstance(history, dict):
        messages = list(history.get("messages") or [])
        if strip_reasoning:
            kept = []
            for message in messages:
                contents = [c for c in (message.contents or []) if getattr(c, "type", None) != "text_reasoning"]
                if not contents:
                    continue
                message.contents = contents
                kept.append(message)
            messages = kept
        session.state[history_source_id] = {**history, "messages": messages}
    return session


def delete_thread(thread_id: str) -> None:
    """Remove every snapshot of one conversation (truncate / delete, UDR-0224 D5)."""
    directory = snapshot_dir(thread_id)
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
        logger.info("[harness session] thread=%s snapshots deleted", thread_id)


def archive_thread(thread_id: str, archive_dir: Path) -> None:
    """Move the conversation's snapshots next to its archived session file (D5, Q6).

    Best-effort, like the uploads: the chat is already archived when this runs.
    """
    directory = snapshot_dir(thread_id)
    if not directory.is_dir():
        return
    try:
        dest = archive_dir / directory.name
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(directory), str(dest))
        logger.info("[harness session] thread=%s snapshots archived -> %s", thread_id, dest)
    except OSError:
        logger.warning("[harness session] thread=%s snapshots could not be archived", thread_id, exc_info=True)


__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "SNAPSHOT_DIR_SUFFIX",
    "archive_thread",
    "delete_thread",
    "history_only",
    "load",
    "save",
    "snapshot_dir",
    "snapshot_path",
]
