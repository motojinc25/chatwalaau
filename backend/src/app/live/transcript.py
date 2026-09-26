"""Live transcript grouping (CTR-0227, PRP-0188, UDR-0170 D6).

GPT-Live streams transcripts as timed FRAGMENTS whose boundaries follow audio
cadence, not meaning. User and assistant fragments interleave because listening and
speaking overlap, and the service emits no turn-completed event. This module turns
that stream into chat turns. It is a pure state machine: no I/O, no clock of its
own (the caller passes ``now_ms``), so every rule below is unit-testable.

Rules (per speaker, independently):

1. A turn opens with the first fragment after that speaker's previous turn closed.
2. A turn closes when that speaker has produced no fragment for ``gap_ms``
   (1,500 ms). Fragments of the OTHER speaker never close it, so a user's "mm-hm"
   in the middle of the assistant's sentence does not split the sentence.
3. A turn is COMMITTED only when every turn that started before it is also closed,
   so messages reach the chat in the order they were started, even when they
   overlap.
4. An assistant turn is marked ``interrupted`` when a user turn started during it
   and the assistant turn stopped within ``gap_ms`` of that start -- it stopped
   because it was talked over. A back-channel the assistant talks through does not
   mark it.
5. ``force_close`` closes open turns immediately (delegation, close, idle).
6. Empty (whitespace-only) turns are dropped.

The gap is measured on the ARRIVAL clock. Fragments arrive in real time, so an
arrival gap tracks the timeline gap; and on a WebRTC session the timeline fields are
not guaranteed (the media path carries no JSON timing), so the service's
``start_ms`` / ``end_ms`` are recorded when present and never relied on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Literal

Role = Literal["user", "assistant"]

#: Silence, per speaker, that ends a turn (UDR-0170 D6; operator-confirmed Q1).
TURN_GAP_MS = 1500

#: Minimum spacing between two provisional pushes of the same open turn.
PROVISIONAL_INTERVAL_MS = 250

_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def safe_session_key(live_session_id: str) -> str:
    """The service session id reduced to a message-id-safe token."""
    return _ID_SAFE.sub("", live_session_id)[:64] or "live"


def message_id(live_session_id: str, kind: str, seq: int) -> str:
    """Deterministic message id: ``live_{session}_{u|a|d}_{seq}`` (UDR-0156 D4)."""
    return f"live_{safe_session_key(live_session_id)}_{kind}_{seq:04d}"


@dataclass
class Turn:
    role: Role
    seq: int
    message_id: str
    opened_at: int  # arrival clock, ms
    last_at: int  # arrival clock, ms
    text: str = ""
    start_ms: int | None = None
    end_ms: int | None = None
    closed: bool = False
    interrupted: bool = False
    last_pushed_at: int | None = None
    last_pushed_text: str = ""

    def to_update(self, live_session_id: str, *, final: bool) -> dict[str, Any]:
        """The CTR-0224 ``live.message`` payload for this turn."""
        return {
            "message_id": self.message_id,
            "role": self.role,
            "text": self.text.strip(),
            "final": final,
            "live": live_meta(
                live_session_id,
                kind="transcript",
                start_ms=self.start_ms,
                end_ms=self.end_ms,
                interrupted=self.interrupted if self.role == "assistant" else None,
            ),
        }


def live_meta(
    live_session_id: str,
    *,
    kind: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    interrupted: bool | None = None,
    delegation_id: str | None = None,
    failed: bool | None = None,
    cancelled: bool | None = None,
) -> dict[str, Any]:
    """The ``live`` object stored on a message (CTR-0227). Absent values are omitted.

    ``kind`` is ``transcript`` (spoken), ``typed`` (step 3: typed during Live) or
    ``delegation`` (a delegated run's answer).
    """
    meta: dict[str, Any] = {"session_id": live_session_id, "kind": kind}
    if start_ms is not None:
        meta["start_ms"] = start_ms
    if end_ms is not None:
        meta["end_ms"] = end_ms
    if interrupted:
        meta["interrupted"] = True
    if delegation_id:
        meta["delegation_id"] = delegation_id
    if failed:
        meta["failed"] = True
    if cancelled:
        meta["cancelled"] = True
    return meta


@dataclass
class GroupingResult:
    """What one ``feed`` / ``tick`` / ``force_close`` produced."""

    provisional: list[dict[str, Any]] = field(default_factory=list)
    committed: list[Turn] = field(default_factory=list)


class TranscriptGrouper:
    """Fragments in, ordered chat turns out (CTR-0227)."""

    def __init__(
        self,
        live_session_id: str,
        *,
        gap_ms: int = TURN_GAP_MS,
        provisional_interval_ms: int = PROVISIONAL_INTERVAL_MS,
    ) -> None:
        self.live_session_id = live_session_id
        self.gap_ms = gap_ms
        self.provisional_interval_ms = provisional_interval_ms
        self._open: dict[Role, Turn | None] = {"user": None, "assistant": None}
        # Every turn not yet committed, in the order it STARTED.
        self._pending: list[Turn] = []
        self._seq = 0
        # Recent user turn starts, for the interruption rule.
        self._user_starts: list[int] = []

    # ---- input -------------------------------------------------------------

    def feed(
        self,
        role: Role,
        delta: str,
        now_ms: int,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> GroupingResult:
        """Append one fragment. Returns a provisional update when one is due."""
        result = GroupingResult()
        if not delta:
            return result
        turn = self._open[role]
        if turn is None:
            self._seq += 1
            kind = "u" if role == "user" else "a"
            turn = Turn(
                role=role,
                seq=self._seq,
                message_id=message_id(self.live_session_id, kind, self._seq),
                opened_at=now_ms,
                last_at=now_ms,
                start_ms=start_ms,
            )
            self._open[role] = turn
            self._pending.append(turn)
            if role == "user":
                self._user_starts.append(now_ms)
        turn.text += delta
        turn.last_at = now_ms
        if turn.start_ms is None and start_ms is not None:
            turn.start_ms = start_ms
        if end_ms is not None:
            turn.end_ms = end_ms
        self._maybe_push(turn, now_ms, result)
        return result

    def tick(self, now_ms: int) -> GroupingResult:
        """Close turns whose speaker has been silent for ``gap_ms``; commit in order."""
        result = GroupingResult()
        for role in ("user", "assistant"):
            turn = self._open[role]
            if turn is None:
                continue
            if now_ms - turn.last_at >= self.gap_ms:
                self._close(turn)
            else:
                self._maybe_push(turn, now_ms, result)
        result.committed.extend(self._drain_committable())
        return result

    def force_close(self, *, started_at_or_before: int | None = None) -> GroupingResult:
        """Close open turns now (delegation settle, session close, idle close).

        With ``started_at_or_before`` (arrival clock) only turns that had started by
        then are closed -- the delegation "settle" of UDR-0170 D4. Commit order is
        still start order, so a later turn that stays open holds nothing back that
        started before it.
        """
        result = GroupingResult()
        for role in ("user", "assistant"):
            turn = self._open[role]
            if turn is None:
                continue
            if started_at_or_before is None or turn.opened_at <= started_at_or_before:
                self._close(turn)
        result.committed.extend(self._drain_committable())
        return result

    @property
    def has_open_turns(self) -> bool:
        return any(t is not None for t in self._open.values())

    # ---- internals ---------------------------------------------------------

    def _maybe_push(self, turn: Turn, now_ms: int, result: GroupingResult) -> None:
        text = turn.text.strip()
        if not text or text == turn.last_pushed_text:
            return
        if turn.last_pushed_at is not None and now_ms - turn.last_pushed_at < self.provisional_interval_ms:
            return
        turn.last_pushed_at = now_ms
        turn.last_pushed_text = text
        result.provisional.append(turn.to_update(self.live_session_id, final=False))

    def _close(self, turn: Turn) -> None:
        turn.closed = True
        self._open[turn.role] = None
        if turn.role == "assistant":
            # Talked over: a user turn started inside this assistant turn, and the
            # assistant stopped within the gap after that start.
            for started in self._user_starts:
                if turn.opened_at < started <= turn.last_at and turn.last_at - started <= self.gap_ms:
                    turn.interrupted = True
                    break
        # Keep only starts that can still matter for an open assistant turn.
        open_assistant = self._open["assistant"]
        horizon = open_assistant.opened_at if open_assistant is not None else turn.last_at
        self._user_starts = [s for s in self._user_starts if s >= horizon]

    def _drain_committable(self) -> list[Turn]:
        committed: list[Turn] = []
        while self._pending and self._pending[0].closed:
            turn = self._pending.pop(0)
            if turn.text.strip():
                committed.append(turn)
        return committed
