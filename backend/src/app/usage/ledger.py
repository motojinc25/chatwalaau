"""Append-only token usage ledger (CTR-0200, PRP-0158, UDR-0136).

One line of JSON per record, in a monthly file under ``USAGE_DIR``:

    USAGE_DIR/2026-09.jsonl

Append-only because a JSON array would need a read-modify-write of the whole month
on every turn, and a crash mid-write would cost the month; an appended line costs
at most itself. Monthly because a date range then selects the files directly, and
day / month / per-chat views are all served by the same file (UDR-0136 D2).

**Atomicity.** One ``os.write`` to a descriptor opened ``O_APPEND``, under a
process-wide ``threading.Lock``.

The lock is NOT belt-and-braces. ``O_APPEND`` alone was measured on Windows and
LOSES RECORDS: 60 concurrent single-line appends from 60 threads produced 58 lines
and raised NOTHING -- writes silently overwrote one another, so the best-effort
exception handling below would never have reported the loss. With the lock the same
harness produces 200 of 200. A ledger that drops billed records without an error is
the failure this contract exists to prevent, so the lock is load-bearing.

The lock is process-wide, which matches the deployment this project assumes
elsewhere (``cron/lock.py`` documents the same single-instance expectation). Under
``uvicorn --workers N`` the guarantee degrades to ``O_APPEND``'s, i.e. to the
measured loss above; that is a stated limit, not a solved problem.

**The append is SYNCHRONOUS.** The CTR-0009 site records the turn from a ``finally``
that also runs on cancellation (a user Stop, a dropped connection), where an async
generator may not await or yield. A blocking single-line write costs less than
handing the work to a thread would, and it removes the entire class of
cancellation-path hazards -- so every entry point here is a plain function.

PRP-0158 section 3.4 proposed reusing the CTR-0130 D5 cron tick lock. That was
wrong for this use and is deliberately NOT what shipped: ``cron/lock.acquire()``
returns False on contention so the caller SKIPS the tick, which for a ledger means
silently dropping a billed record -- the exact loss this contract exists to
prevent. A guard that can decline is a guard for work that may be skipped; an
append may not be.

**Nothing here may fail a turn** (UDR-0136 D6). Every public entry point swallows
its own exceptions after logging.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.demo import is_demo_mode

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

# The token fields, in the shape PRP-0157 / UDR-0135 D6 fixed: price points kept
# SEPARATE, because cache reads and writes are billed at rates that cannot be
# recovered from a merged input figure. No total_token_count -- the Anthropic client
# reports none and a computed one would mean different things per provider.
TOKEN_FIELDS: tuple[str, ...] = (
    "uncached_input_token_count",
    "cache_read_input_token_count",
    "cache_creation_input_token_count",
    "output_token_count",
    "reasoning_output_token_count",
)

# The COMPLETE key set a record may carry (UDR-0136 D3). The builder below accepts
# named arguments only and never a free-form mapping, so message content cannot
# reach the ledger by being passed through. An invariant test pins this tuple.
LEDGER_FIELDS: tuple[str, ...] = (
    "ts",
    "lane",
    "kind",
    "purpose",
    "thread_id",
    "temporary",
    "model",
    "provider",
    "run_target",
    "model_calls",
    *TOKEN_FIELDS,
    "outcome",
)


# Serializes appends across threads: background tasks, request handlers and the
# CTR-0009 seam all write from different threads of one process. See the module
# docstring for the measurement that makes this mandatory.
_append_lock = threading.Lock()


def ledger_dir() -> Path:
    """The ledger root. NEVER under SESSIONS_DIR (UDR-0136 D1)."""
    return Path(settings.usage_dir)


def month_path(when: datetime) -> Path:
    """The monthly file a record timestamped ``when`` belongs to."""
    return ledger_dir() / f"{when.astimezone(UTC):%Y-%m}.jsonl"


def _int_or_none(source: Mapping[str, Any] | None, key: str) -> int | None:
    """Read an int, or None when absent / not an integer (UDR-0135 D7).

    An absent measurement stays absent all the way into the record. Zero never
    stands in for unknown, because a consumer summing a false zero cannot tell it
    from a real one.
    """
    if not source:
        return None
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def build_record(
    *,
    lane: str,
    kind: str,
    model: str | None,
    provider: str | None,
    turn: Mapping[str, Any] | None,
    thread_id: str | None = None,
    temporary: bool = False,
    purpose: str | None = None,
    run_target: str | None = None,
    outcome: str = "completed",
    when: datetime | None = None,
) -> dict[str, Any] | None:
    """Assemble one ledger record, or None when nothing was measured.

    Named arguments only, by design (UDR-0136 D3): there is no path by which a
    caller's arbitrary dict -- a message, a title, a tool argument -- can be carried
    into the ledger. Every key the record can hold is in ``LEDGER_FIELDS``.

    ``turn`` is the accumulated usage: the CTR-0009 ``turn`` object for a lane turn,
    or a MAF ``UsageDetails`` for a helper pass. Absent token keys are omitted, and
    a record with no token key at all is not written -- an empty record would assert
    a measurement that never happened.
    """
    when = when or datetime.now(UTC)
    record: dict[str, Any] = {
        "ts": when.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "lane": lane,
        "kind": kind,
    }
    if purpose:
        record["purpose"] = purpose

    # A Temporary Chat's tokens were really billed, so day / month totals must
    # include them; the per-chat view has no chat to attribute them to, which is the
    # truth about an ephemeral conversation rather than a gap (UDR-0136 D9).
    if temporary:
        record["thread_id"] = None
        record["temporary"] = True
    elif thread_id:
        record["thread_id"] = thread_id

    if model:
        record["model"] = model
    if provider:
        record["provider"] = provider
    if run_target:
        record["run_target"] = run_target

    calls = _int_or_none(turn, "model_calls")
    if calls is not None:
        record["model_calls"] = calls

    measured = False
    for field in TOKEN_FIELDS:
        value = _int_or_none(turn, field)
        if value is not None:
            record[field] = value
            measured = True

    # A helper pass reports a raw MAF UsageDetails, which carries `input_token_count`
    # rather than the CTR-0009 `uncached_input_token_count` split. Fall back to it so
    # such a lane still records its input. The caller normalizes when it can; here
    # the reported value is recorded as-is rather than guessed at.
    if "uncached_input_token_count" not in record:
        raw_input = _int_or_none(turn, "input_token_count")
        if raw_input is not None:
            record["uncached_input_token_count"] = raw_input
            measured = True

    if not measured:
        return None

    record["outcome"] = outcome
    return record


def _append_line(path: Path, line: str) -> None:
    """One O_APPEND write, serialized process-wide.

    Both halves are required: the lock keeps concurrent writers from overwriting one
    another (measured on Windows -- see the module docstring), and O_APPEND keeps the
    write positioned at the true end of file even if another process holds a handle.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _append_lock:
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


def append_record(record: dict[str, Any] | None) -> None:
    """Append one record. Best-effort: this MUST NOT fail a turn (UDR-0136 D6).

    DEMO_MODE writes nothing (UDR-0136 D7): DemoChatClient's counts are synthetic
    (``chars // 4``), so recording them would corrupt real statistics with
    fabricated numbers rather than merely adding noise.
    """
    if record is None:
        return
    if is_demo_mode():
        return
    try:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        when = datetime.now(UTC)
        # The record's own timestamp decides its month, so a record built just
        # before a month boundary lands in the month it says it happened in.
        with contextlib.suppress(KeyError, ValueError):
            when = datetime.fromisoformat(str(record["ts"]))
        _append_line(month_path(when), line)
    except Exception:
        # Never propagate. A statistics write is not worth a user's turn.
        logger.warning("usage ledger append failed", exc_info=True)


def append_turn_usage(
    *,
    lane: str,
    turn: Mapping[str, Any] | None,
    model: str | None,
    thread_id: str | None,
    temporary: bool = False,
    run_target: str | None = None,
    outcome: str = "completed",
) -> None:
    """Record one lane turn (CTR-0009 / CTR-0057 / CTR-0140 append sites).

    ``turn`` is the CTR-0009 ``turn`` object -- already the normalized price-point
    breakdown -- so no provider convention has to be re-applied here.
    """
    try:
        provider = _provider_name(model)
        record = build_record(
            lane=lane,
            kind="turn",
            model=model,
            provider=provider,
            turn=turn,
            thread_id=thread_id,
            temporary=temporary,
            run_target=run_target,
            outcome=outcome,
        )
    except Exception:
        logger.warning("usage ledger record build failed", exc_info=True)
        return
    append_record(record)


def append_helper_usage(
    *,
    purpose: str,
    usage_details: Any,
    model: str | None,
    thread_id: str | None = None,
    outcome: str = "completed",
) -> None:
    """Record one background helper pass (the six ``get_response`` sites).

    Those calls are NON-streaming, which is the path MAF aggregates for us
    (``_tools.py`` sets ``response.usage_details`` to the summed total), so the
    complete figure is already on the response object -- it was simply never read.

    ``model_calls`` is recorded as 1: MAF's aggregate does not report how many
    calls it summed, and inventing a count would be worse than omitting the
    distinction for a single-shot helper.
    """
    try:
        usage = dict(usage_details) if usage_details else None
        if usage is not None:
            usage.setdefault("model_calls", 1)
        provider = _provider_name(model)
        record = build_record(
            lane="helper",
            kind="helper",
            purpose=purpose,
            model=model,
            provider=provider,
            turn=usage,
            thread_id=thread_id,
            outcome=outcome,
        )
    except Exception:
        logger.warning("usage ledger helper record build failed", exc_info=True)
        return
    append_record(record)


def _provider_name(model: str | None) -> str | None:
    """Resolve the owning provider's name for ``model``, or None.

    Recorded PER RECORD rather than resolved at read time: a catalog offering can be
    renamed or removed, and the record must still say what served that turn.
    """
    if not model:
        return None
    try:
        from app import providers

        return str(getattr(providers.provider_for(model), "name", "") or "") or None
    except Exception:
        return None


__all__ = [
    "LEDGER_FIELDS",
    "TOKEN_FIELDS",
    "append_helper_usage",
    "append_record",
    "append_turn_usage",
    "build_record",
    "ledger_dir",
    "month_path",
]
