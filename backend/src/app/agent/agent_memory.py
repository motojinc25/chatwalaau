"""Agent Curated Memory -- section-delimited store, memory tool, and snapshot.

Contract: CTR-0162 (PRP-0100 / UDR-0079). The SECOND built-in file memory and the
occupant of Prompt Assembly slot #2b (the Agent Memory Block), a sibling of the
User Preference Memory (CTR-0105, slot #2a). Where ``USER.md`` records "what the
agent knows about the USER", ``MEMORY.md`` records what the AGENT itself should
durably remember about the WORK: environment facts, project conventions, tool
quirks, and learned operating rules.

A single instance-wide file ``.agent/MEMORY.md`` (same directory as
``.agent/USER.md`` / ``.agent/IDENTITY.md``) holds the curated entries, stored as
a list of independent entries separated by ``ENTRY_DELIMITER = "\\n§\\n"`` (a line
that is a lone section sign). The entries are rendered into a ``<agent-memory>``
block placed at slot #2b -- but ONLY when non-empty (UDR-0079 D8), so a fresh
install injects nothing.

Key properties (UDR-0079):

- D2: fixed path + EMPTY ``DEFAULT_AGENT_MEMORY`` code-constant fallback;
  best-effort create; graceful in-memory fallback on any read/write failure
  (failure semantics reuse CTR-0104 ``load_identity()`` / CTR-0105).
- D3: the ``§``-entry codec -- memory as a SET of small independent facts, so
  duplicate detection, contradiction resolution, and per-entry editing are
  tractable and the injected structure stays stable under the cap.
- D4: the deterministic secret/PII filter is REUSED from CTR-0105 (the
  authoritative "never store secrets" guarantee).
- D5: the hardened guarded write -- exact-duplicate reject, unique-substring
  addressing (0 or >1 match -> reject; NOT entry ids), ``.lock`` exclusion,
  temp-file + atomic replace, and backup-on-write.
- D6: a FROZEN per-session snapshot captured once at session start; mid-session
  writes never change the running prompt (take effect from the next session).
- D7: two write paths -- this inline ``manage_memory`` tool and the per-turn
  ``memory-curate`` background pass (CTR-0163).
- D11: no new Capability and no new Protocol seam -- a MAF function tool plus
  stdlib file I/O within CAP-002.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import time
from typing import Annotated

from pydantic import Field

# The deterministic secret/PII pre-write filter is shared with the User
# Preference Memory (CTR-0105 v3 / UDR-0079 D4). ``detect_prohibited`` is the
# authoritative guard; reusing it keeps both memories held to the same rule.
from app.agent.user_memory import detect_prohibited as screen_sensitive_content
from app.core.config import settings

logger = logging.getLogger(__name__)

# Fixed, CWD-relative agent-memory file path (UDR-0079 D2 -- not env-configurable;
# same directory as .agent/USER.md and .agent/IDENTITY.md).
AGENT_MEMORY_PATH = Path(".agent") / "MEMORY.md"

# Advisory cross-platform write lock (UDR-0079 D5). Co-located in .agent/ so the
# temp file, backup, and target share one volume for an atomic os.replace.
_LOCK_PATH = Path(".agent") / ".memory.lock"

# Built-in default agent memory -- EMPTY (UDR-0079 D2). Unlike USER.md's seeded
# default, the agent's notebook starts blank and is filled only by curation, so
# an empty memory injects NO block (D8).
DEFAULT_AGENT_MEMORY = ""

# Entry delimiter: a line that is a lone section sign (UDR-0079 D3). Entries are
# joined / split on this; an entry whose body would itself contain this line is
# rejected (codec safety).
ENTRY_DELIMITER = "\n§\n"
_DELIMITER_LINE = "§"

# Fixed boilerplate that wraps the curated entries into the Agent Memory Block.
# It states the boundary (agent/environment facts here; user preferences in
# USER.md) and precedence, and is NOT counted against MEMORY_CHAR_LIMIT.
_BLOCK_HEADER = (
    "<agent-memory>\n"
    "The following are the agent's own curated notes about this environment, project, and\n"
    "conventions (durable facts, tool quirks, and operating rules). Treat them as durable\n"
    "operational context. They are ABOUT THE WORK, not about the user (user preferences live\n"
    "elsewhere). They do not override system, safety, developer, or explicit current user\n"
    "instructions.\n"
)
_BLOCK_FOOTER = "</agent-memory>"


# ---------------------------------------------------------------------------
# Codec (UDR-0079 D3)
# ---------------------------------------------------------------------------
def parse_entries(content: str) -> list[str]:
    """Split the raw ``MEMORY.md`` body into clean, non-empty entries."""
    return [entry.strip() for entry in content.split(ENTRY_DELIMITER) if entry.strip()]


def serialize_entries(entries: list[str]) -> str:
    """Join entries back into the ``§``-delimited body."""
    return ENTRY_DELIMITER.join(entries)


def _normalize(text: str) -> str:
    """Whitespace-normalized form used for exact-duplicate detection."""
    return " ".join(text.split())


def _contains_delimiter_line(entry: str) -> bool:
    """True if ``entry`` contains a lone-``§`` line (would break the codec)."""
    return any(line.strip() == _DELIMITER_LINE for line in entry.splitlines())


# ---------------------------------------------------------------------------
# Load / render
# ---------------------------------------------------------------------------
def load_agent_memory() -> str:
    """Return the curated agent-memory body text (CTR-0162).

    Reads ``.agent/MEMORY.md`` when it exists. When absent, best-effort creates an
    empty file so it is discoverable/editable. On any read or write failure logs a
    WARNING and returns the in-memory ``DEFAULT_AGENT_MEMORY`` (empty). Never
    raises (UDR-0079 D2 -- mirrors CTR-0104 ``load_identity()``).
    """
    try:
        if AGENT_MEMORY_PATH.is_file():
            return AGENT_MEMORY_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning(
            "Could not read agent memory file %s; using the built-in default (empty)",
            AGENT_MEMORY_PATH,
            exc_info=True,
        )
        return DEFAULT_AGENT_MEMORY

    # File absent -> best-effort materialize an empty file (UDR-0079 D2). A failure
    # here (e.g. read-only container filesystem) must not block agent construction.
    try:
        AGENT_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        AGENT_MEMORY_PATH.write_text(DEFAULT_AGENT_MEMORY, encoding="utf-8")
        logger.info("Created empty agent memory file at %s", AGENT_MEMORY_PATH)
    except OSError:
        logger.warning(
            "Could not create agent memory file %s (read-only filesystem?)",
            AGENT_MEMORY_PATH,
            exc_info=True,
        )
    return DEFAULT_AGENT_MEMORY


def render_agent_memory_block(content: str) -> str | None:
    """Wrap curated entries in the ``<agent-memory>`` block, or None if empty.

    Returns None when there are no entries (UDR-0079 D8), so an empty memory
    injects NO block and a fresh install's prompt prefix is unchanged.
    """
    entries = parse_entries(content)
    if not entries:
        return None
    body = "\n".join(f"- {entry}" for entry in entries)
    return f"{_BLOCK_HEADER}\n{body}\n{_BLOCK_FOOTER}"


# ---------------------------------------------------------------------------
# Durable guarded write (UDR-0079 D5)
# ---------------------------------------------------------------------------
def _acquire_lock(timeout: float = 5.0) -> int | None:
    """Best-effort advisory lock via an O_EXCL lock file (cross-platform).

    Returns the open fd on success, or None if the lock could not be acquired in
    ``timeout`` seconds. A stale lock older than 30s is reclaimed so a crashed
    writer never blocks forever.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            return os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Reclaim a stale lock left by a crashed writer.
            try:
                if time.time() - _LOCK_PATH.stat().st_mtime > 30:
                    _LOCK_PATH.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
        except OSError:
            # Directory not creatable (read-only FS): proceed without a lock
            # rather than block; the atomic replace still prevents torn writes.
            return None


def _release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    finally:
        _LOCK_PATH.unlink(missing_ok=True)


def _write_entries(entries: list[str]) -> str | None:
    """Back up + atomically write the entries. Return an error string or None.

    Order (UDR-0079 D5): acquire ``.memory.lock`` -> back up the prior file ->
    write a temp file -> ``os.replace`` (atomic) -> release the lock.
    """
    content = serialize_entries(entries)
    if content:
        content += "\n"
    fd = _acquire_lock()
    try:
        AGENT_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        if AGENT_MEMORY_PATH.is_file():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            backup = AGENT_MEMORY_PATH.parent / f"MEMORY.md.bak-{stamp}"
            backup.write_text(AGENT_MEMORY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        tmp = AGENT_MEMORY_PATH.parent / f".MEMORY.md.{os.getpid()}.tmp"
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(AGENT_MEMORY_PATH)  # atomic same-volume replace (Path.replace -> os.replace)
    except OSError:
        logger.warning("Could not update agent memory file %s", AGENT_MEMORY_PATH, exc_info=True)
        return "the agent memory file could not be written (read-only filesystem?)"
    finally:
        _release_lock(fd)
    return None


def _result(status: str, reason: str = "", **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {"status": status}
    if reason:
        payload["reason"] = reason
    payload.update(extra)
    return payload


def _validate_new_entry(entry: str, others: list[str]) -> str | None:
    """Validate a candidate entry against the codec, filter, and duplicates.

    Returns a rejection reason, or None when the entry is acceptable. ``others``
    is the set of entries it is compared against for exact-duplicate detection.
    """
    if not entry:
        return "empty entry"
    if _contains_delimiter_line(entry):
        return "an entry may not contain a lone '§' line (it is the entry delimiter)"
    reason = screen_sensitive_content(entry)
    if reason:
        return reason
    normalized = _normalize(entry)
    if any(_normalize(o) == normalized for o in others):
        return "entry already present"
    return None


def _resolve_unique(entries: list[str], substring: str) -> tuple[int | None, str | None]:
    """Resolve the single entry containing ``substring`` (UDR-0079 D5).

    Returns ``(index, None)`` on exactly one match, else ``(None, reason)`` for
    zero or multiple matches -- the write never guesses.
    """
    needle = substring.strip()
    if not needle:
        return None, "a non-empty match substring is required"
    matches = [i for i, e in enumerate(entries) if needle in e]
    if not matches:
        return None, f"no entry contains the substring {needle!r}"
    if len(matches) > 1:
        return None, f"the substring {needle!r} matches {len(matches)} entries; make it unique"
    return matches[0], None


def _apply_one(entries: list[str], op: dict[str, str]) -> tuple[bool, str]:
    """Apply one op to ``entries`` in place. Return (applied, reason)."""
    action = str(op.get("operation", "")).strip().lower()
    content = str(op.get("content", "")).strip()
    match = str(op.get("match", "") or op.get("old", "")).strip()
    limit = settings.memory_char_limit

    if action == "add":
        reason = _validate_new_entry(content, entries)
        if reason:
            return False, reason
        candidate = [*entries, content]
        if len(serialize_entries(candidate)) > limit:
            return False, f"would exceed MEMORY_CHAR_LIMIT ({limit}); remove or consolidate entries first"
        entries.append(content)
        return True, "added"

    if action == "remove":
        index, reason = _resolve_unique(entries, match or content)
        if reason:
            return False, reason
        assert index is not None
        entries.pop(index)
        return True, "removed"

    if action == "modify":
        index, reason = _resolve_unique(entries, match)
        if reason:
            return False, reason
        assert index is not None
        others = [e for i, e in enumerate(entries) if i != index]
        reason = _validate_new_entry(content, others)
        if reason:
            return False, reason
        candidate = [*others, content]
        if len(serialize_entries(candidate)) > limit:
            return False, f"would exceed MEMORY_CHAR_LIMIT ({limit}); remove or consolidate entries first"
        entries[index] = content
        return True, "modified"

    return False, f"unknown operation {action!r} (use add, remove, or modify)"


def apply_agent_memory_ops(ops: list[dict[str, str]]) -> dict[str, object]:
    """Apply a BATCH of ops through the guarded ``§``-store write (CTR-0162).

    Each op is ``{"operation": add|remove|modify, "content": ..., "match": ...}``.
    Ops are applied sequentially to an in-memory working copy; every op's result is
    recorded, and a SINGLE atomic write happens at the end when the copy changed
    (UDR-0079 D5). The deterministic secret/PII filter, the exact-duplicate guard,
    the unique-substring rule, and the ``MEMORY_CHAR_LIMIT`` cap are enforced per
    op. This function does NOT check the enable toggle / temporary contextvar --
    callers gate those.
    """
    entries = parse_entries(load_agent_memory())
    before = list(entries)
    results: list[dict[str, object]] = []
    for op in ops:
        applied, reason = _apply_one(entries, op)
        results.append({"status": "applied" if applied else "rejected", "reason": reason})

    if entries != before:
        error = _write_entries(entries)
        if error:
            return _result("rejected", error, results=results)
        applied_count = sum(1 for r in results if r["status"] == "applied")
        logger.info("Agent memory updated via memory tool (%d op(s) applied, entries=%d)", applied_count, len(entries))

    any_applied = any(r["status"] == "applied" for r in results)
    return _result("applied" if any_applied else "rejected", results=results)


def write_agent_memory(entries: list[str]) -> dict[str, object]:
    """Reconcile-write the WHOLE memory to ``entries`` (CTR-0163 curation path).

    Validates every entry (codec + secret/PII filter), drops exact duplicates,
    enforces the cap, and performs a single guarded atomic write. Used by the
    background curation pass, which reconciles the whole memory rather than issuing
    granular ops (UDR-0079 D7). Returns a structured result.
    """
    clean: list[str] = []
    for raw in entries:
        entry = raw.strip()
        reason = _validate_new_entry(entry, clean)
        if reason:
            # A duplicate is silently skipped; a filtered/invalid entry is dropped
            # and logged, never a partial/unsafe write (mirrors CTR-0117).
            logger.debug("agent memory reconcile dropped an entry: %s", reason)
            continue
        clean.append(entry)
    limit = settings.memory_char_limit
    while clean and len(serialize_entries(clean)) > limit:
        # Trim from the tail until within the cap (defensive; the reconcile prompt
        # is told to stay concise).
        clean.pop()
    error = _write_entries(clean)
    if error:
        return _result("rejected", error)
    logger.info("Agent memory reconciled (entries=%d)", len(clean))
    return _result("applied", entries=len(clean))


# ---------------------------------------------------------------------------
# Memory tool (CTR-0162). Registered on the shared agent when AGENT_MEMORY_ENABLED.
# ---------------------------------------------------------------------------
def manage_memory(
    operation: Annotated[
        str,
        Field(description="One of: add, remove, modify. For batch, pass 'operations' instead."),
    ] = "",
    content: Annotated[
        str,
        Field(
            description=(
                "For add: the new memory entry (1-2 sentences). For modify: the new full entry "
                "text that replaces the matched one. For remove: leave empty."
            )
        ),
    ] = "",
    match: Annotated[
        str,
        Field(
            description=(
                "For remove/modify: a UNIQUE substring contained in the existing entry to target. "
                "It must match exactly one entry."
            )
        ),
    ] = "",
    operations: Annotated[
        list[dict[str, str]] | None,
        Field(
            description=(
                "Optional BATCH: a list of ops, each {operation, content, match}. When given, the "
                "single operation/content/match arguments are ignored."
            )
        ),
    ] = None,
) -> str:
    """Update the agent's durable curated memory (.agent/MEMORY.md).

    Use this for information ABOUT THE WORK that is durable, reusable across
    sessions, and non-sensitive -- environment facts, project conventions, tool
    quirks, and operating rules you will keep needing. This is the agent's own
    notebook; user PREFERENCES belong in the separate User Preference Memory
    (manage_user_memory), not here.

    DO store (high-signal, recurring): "This project uses pnpm", "Run tests with
    `uv run pytest`", "DB migrations use alembic", "The user prefers a design
    explanation before implementation".

    DO NOT store: transient work logs, the full detail of a recent conversation,
    one-off requests, long code snippets, full search/tool output, or ANY
    secret/credential or sensitive personal data (rejected deterministically by
    the backend).

    The change is persisted to .agent/MEMORY.md immediately (a backup is written
    first) and takes effect from the NEXT session -- the current session's frozen
    memory snapshot is intentionally unchanged.

    Operations:
    - add: append a new entry (in 'content'). An exact duplicate is rejected; an
      over-cap add is rejected (remove/consolidate first).
    - remove: delete the entry uniquely identified by the 'match' substring.
    - modify: replace the entry uniquely identified by 'match' with 'content'.
    - batch: pass 'operations' = [{operation, content, match}, ...] to apply
      several at once.

    A remove/modify whose 'match' hits zero or several entries is rejected (make
    it unique). Returns a JSON string with a per-op status.
    """
    if not settings.agent_memory_enabled:
        return json.dumps(_result("rejected", "agent memory is disabled"), ensure_ascii=False)
    # Temporary Chat (CTR-0106, UDR-0052 D7/D8): a temporary run must neither read
    # nor write the built-in learning loop. The tool stays registered but no-ops.
    from app.agent.temporary import in_temporary_run

    if in_temporary_run():
        return json.dumps(_result("rejected", "temporary chat does not update memory"), ensure_ascii=False)

    ops = operations or [{"operation": operation, "content": content, "match": match}]
    return json.dumps(apply_agent_memory_ops(ops), ensure_ascii=False)


# Capability-layer guidance for the memory tool (slot #3..). Appended to the agent
# instructions only when AGENT_MEMORY_ENABLED (PRP-0100).
AGENT_MEMORY_INSTRUCTION = (
    " You maintain a durable Agent Memory (a curated notebook about the WORK) via the "
    "manage_memory tool. When you learn something durable, reusable, and non-sensitive about "
    "this environment or project -- conventions (e.g. the package manager, the test command, "
    "the migration tool), tool quirks, or stable operating rules -- record it via the tool. "
    "This memory is ABOUT THE WORK; user PREFERENCES belong in the User Preference Memory "
    "(manage_user_memory), not here. Reconcile rather than pile up: if a new fact duplicates "
    "or contradicts an existing entry in the <agent-memory> block, use 'modify' or 'remove' "
    "(each targets an entry by a UNIQUE substring) instead of 'add'. Never store transient "
    "work logs, one-off requests, long code, raw tool output, secrets, or credentials. Updates "
    "take effect from the next session."
)


# ---------------------------------------------------------------------------
# Per-session frozen snapshot (UDR-0079 D6)
# ---------------------------------------------------------------------------
def current_agent_memory_block() -> str | None:
    """Render the Agent Memory Block from the live MEMORY.md (session start)."""
    return render_agent_memory_block(load_agent_memory())


def session_agent_memory_snapshot(thread_id: str) -> str | None:
    """Return the frozen Agent Memory Block for a session, capturing it once.

    Returns None when AGENT_MEMORY_ENABLED is false or the memory is empty.
    Otherwise: if the session already has a stored ``agent_memory_snapshot``,
    return it (frozen -- never re-read mid-session, UDR-0079 D6); else render the
    block from the live MEMORY.md, best-effort persist it into the session JSON so
    it survives reload, and return it.
    """
    if not settings.agent_memory_enabled:
        return None

    # Lazy import avoids import-order coupling between agent and session subpackages.
    from app.session.storage import read_session_json, write_session_json

    try:
        data = read_session_json(thread_id)
    except (OSError, ValueError, json.JSONDecodeError):
        data = None

    if data and data.get("agent_memory_snapshot"):
        return data["agent_memory_snapshot"]

    block = current_agent_memory_block()
    if block is None:
        # Empty memory -> no block, and nothing to freeze (a later curated entry
        # is captured by the NEXT session, UDR-0079 D6/D8).
        return None

    record = data or {
        "thread_id": thread_id,
        "title": "",
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "message_count": 0,
        "image_count": 0,
        "folder_id": None,
        "messages": [],
    }
    record["agent_memory_snapshot"] = block
    try:
        write_session_json(thread_id, record)
    except OSError:
        logger.warning("Could not persist agent memory snapshot for session %s", thread_id, exc_info=True)
    return block
