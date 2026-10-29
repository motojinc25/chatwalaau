"""AI Discussion controller (CTR-0274, PRP-0232, UDR-0215 D1-D6, D9-D11).

The code decides; the models only write (UDR-0215 D8). This module owns:

- the state machine (``TRANSITIONS``) and every status change (``_transition``);
- one advance per run: a process-local guard plus a persisted lease taken by compare-and-set
  under the session file lock and refreshed while the phase runs (D3);
- the phase runner, which streams AG-UI events and commits each speaker's message before it is
  announced, skipping speakers whose message is already stored (D4);
- human input applied once by ``request_id`` (D5);
- the budget, with the summary's share reserved (D9);
- disconnect = interrupt: the phase lives in its request, a close pauses the run (D6);
- PRP-0234 (UDR-0217): research tools per seat (bounded, charged), auto mode (no wait after the
  framing or a round, a finish suggestion is only shown; Pause hands control back) and the full
  report after the stored final summary.

Every write goes through ``update_session_json`` / ``create_session_json`` (UDR-0156 D1).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any
import uuid

from app.discussion import participants as seats
from app.discussion import schema, speakers
from app.session.storage import (
    SessionUnavailableError,
    create_session_json,
    empty_session_record,
    read_session_json,
    update_session_json,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

#: A lease lives this long without a refresh; the runner refreshes it every LEASE_REFRESH_SECONDS.
LEASE_SECONDS = 45
LEASE_REFRESH_SECONDS = 15
#: One repair request when the moderator's output is not the expected object (PRP-0232 2.5).
MODERATOR_ATTEMPTS = 2

TRANSITIONS: dict[str, frozenset[str]] = {
    schema.CREATED: frozenset({schema.RUNNING, schema.CANCELLED}),
    schema.RUNNING: frozenset(
        {schema.RUNNING, schema.WAITING_HUMAN, schema.PAUSED, schema.SUMMARIZING, schema.CANCELLED, schema.FAILED}
    ),
    schema.WAITING_HUMAN: frozenset({schema.RUNNING, schema.SUMMARIZING, schema.CANCELLED}),
    schema.PAUSED: frozenset({schema.RUNNING, schema.SUMMARIZING, schema.CANCELLED}),
    schema.SUMMARIZING: frozenset(
        {schema.SUMMARIZING, schema.COMPLETED, schema.PAUSED, schema.CANCELLED, schema.FAILED}
    ),
    schema.COMPLETED: frozenset(),
    schema.CANCELLED: frozenset(),
    schema.FAILED: frozenset(),
}


class DiscussionError(Exception):
    """A request the controller refuses; ``code`` is the API error code."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# Runs this process is advancing right now, and what a pause / cancel asked of them.
_active: set[str] = set()
_stop_events: dict[str, asyncio.Event] = {}
_stop_requests: dict[str, str] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _transition(run: dict[str, Any], status: str) -> None:
    current = run.get("status")
    if status not in TRANSITIONS.get(str(current), frozenset()):
        raise DiscussionError("invalid_transition", f"a {current} discussion cannot become {status}")
    run["status"] = status
    run["updated_at"] = schema.now_iso()


def lease_valid(run: dict[str, Any], now: datetime | None = None) -> bool:
    lease = run.get("lease")
    if not isinstance(lease, dict):
        return False
    try:
        expires = datetime.fromisoformat(str(lease.get("expires_at")))
    except ValueError:
        return False
    return expires > (now or _now())


def heal(run: dict[str, Any]) -> bool:
    """An active run nobody is advancing reads as interrupted (D3). True when it changed."""
    if run.get("status") in schema.ACTIVE and not lease_valid(run) and run.get("id") not in _active:
        run["status"] = schema.PAUSED
        run["pause_reason"] = schema.PAUSE_INTERRUPTED
        run["lease"] = None
        run["updated_at"] = schema.now_iso()
        return True
    return False


def budget_from_settings() -> dict[str, int]:
    from app.core.config import settings

    return {
        "token_budget": int(settings.discussion_token_budget),
        "summary_reserve": int(settings.discussion_summary_reserve_tokens),
        "turn_max_output_tokens": int(settings.discussion_turn_max_output_tokens),
        "phase_timeout_seconds": int(settings.discussion_phase_timeout_seconds),
        "advisory_threshold_percent": int(settings.discussion_advisory_threshold_percent),
        "tool_calls_per_turn": int(settings.discussion_tool_calls_per_turn),
        "tool_result_max_chars": int(settings.discussion_tool_result_max_chars),
        "report_max_output_tokens": int(settings.discussion_report_max_output_tokens),
    }


def round_limits() -> tuple[int, int]:
    """``(default, maximum)`` rounds; the default never exceeds the maximum."""
    from app.core.config import settings

    maximum = max(int(settings.discussion_max_rounds), 1)
    return min(max(int(settings.discussion_default_rounds), 1), maximum), maximum


def limits() -> dict[str, Any]:
    from app.core.config import settings

    default_rounds, max_rounds = round_limits()
    return {
        "default_rounds": default_rounds,
        "max_rounds": max_rounds,
        "max_participants": int(settings.discussion_max_participants),
        **budget_from_settings(),
    }


# ---------------------------------------------------------------------------
# Session access
# ---------------------------------------------------------------------------


def _read(thread_id: str) -> dict[str, Any]:
    data = read_session_json(thread_id)
    if data is None:
        raise DiscussionError("not_found", "chat not found", 404)
    return data


def _mutate_run(thread_id: str, run_id: str, change: Any) -> tuple[dict[str, Any], Any]:
    """Run ``change(data, run)`` under the session lock; returns (run copy, change result)."""
    box: dict[str, Any] = {}

    def mutate(data: dict[str, Any]) -> bool | None:
        run = schema.find_run(data, run_id)
        if run is None:
            box["missing"] = True
            return False
        healed = heal(run)
        result = change(data, run)
        box["result"] = result
        box["run"] = dict(run)
        if result is _UNCHANGED and not healed:
            return False
        return None

    if update_session_json(thread_id, mutate) is None or box.get("missing"):
        raise DiscussionError("not_found", "discussion not found", 404)
    return box["run"], box.get("result")


_UNCHANGED = object()


def _append_messages(data: dict[str, Any], messages: list[dict[str, Any]]) -> list[str]:
    stored = data.get("messages")
    if not isinstance(stored, list):
        stored = []
        data["messages"] = stored
    present = schema.stored_message_ids(data)
    added: list[str] = []
    for message in messages:
        if message["message_id"] in present:
            continue
        stored.append(message)
        added.append(message["message_id"])
    if added:
        data["message_count"] = len(stored)
        data["updated_at"] = schema.now_iso()
    return added


def _next_sequence(run: dict[str, Any]) -> int:
    run["sequence"] = int(run.get("sequence") or 0) + 1
    return run["sequence"]


def _human_message(run: dict[str, Any], *, text: str, phase: str, kind: str, msg_id: str) -> dict[str, Any]:
    return schema.build_message(
        msg_id=msg_id,
        role="user",
        text=text,
        run_id=run["id"],
        speaker_id=schema.HUMAN_ID,
        speaker_role="human",
        display_name="You",
        phase=phase,
        round_no=int(run.get("round") or 0),
        sequence=_next_sequence(run),
        kind=kind,
    )


def _brief_text(run: dict[str, Any], *, continued: bool = False) -> str:
    brief = run["brief"]
    names = ", ".join(p["display_name"] for p in run["participants"])
    # Plain text: it is a USER-side message, which the chat shows as typed (and Privacy Screen
    # redacts as text), never as Markdown.
    lines = [f"{'Continued AI discussion' if continued else 'AI discussion'}: {brief['topic']}"]
    if brief.get("goal"):
        lines.append(f"Wanted outcome: {brief['goal']}")
    if brief.get("constraints"):
        lines.append(f"Constraints: {brief['constraints']}")
    tail = ""
    if continued:
        if run.get("focus"):
            lines.append(f"Focus: {run['focus']}")
        carried = run.get("carried")
        if carried:
            counts = (
                _count(carried.get("common"), "common point"),
                _count(carried.get("conflicts"), "conflict"),
                _count(carried.get("missing"), "open issue"),
            )
            lines.append(f"Carried over: {', '.join(counts)}")
        else:
            lines.append("Carried over: nothing (the previous run left no conclusion)")
        if run.get("skip_opening"):
            tail = " -- Opening skipped"
    lines.append(f"Participants: {names} -- Rounds: {run['rounds']}{tail}")
    return "\n".join(lines)


def _count(items: Any, noun: str) -> str:
    n = len(items) if isinstance(items, list) else 0
    return f"{n} {noun}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------------------
# Create / read
# ---------------------------------------------------------------------------


def _initial_folder(folder_id: str | None) -> str | None:
    if not folder_id:
        return None
    from app.session.storage import list_folder_ids

    return folder_id if folder_id in list_folder_ids() else None


def create_discussion(
    *,
    brief: dict[str, str],
    seat_requests: list[dict[str, Any]],
    moderator_model: str | None,
    rounds: int | None,
    folder_id: str | None,
    initiated_by: str,
    auto: bool = False,
    report: bool = True,
) -> tuple[str, dict[str, Any]]:
    """A new discussion chat with its first run (status ``created``)."""
    from app.core.config import settings

    default_rounds, max_rounds = round_limits()
    rounds = default_rounds if rounds is None else rounds
    if not 1 <= rounds <= max_rounds:
        raise DiscussionError("invalid_request", f"rounds must be between 1 and {max_rounds}", 422)
    try:
        participants = seats.resolve_seats(seat_requests, max_seats=int(settings.discussion_max_participants))
        moderator = seats.resolve_moderator(moderator_model)
    except seats.SeatError as exc:
        raise DiscussionError("invalid_request", str(exc), 422) from exc

    run = schema.new_run(
        brief=brief,
        participants=participants,
        moderator=moderator,
        rounds=rounds,
        budget=budget_from_settings(),
        initiated_by=initiated_by,
        auto=auto,
        report=report,
    )
    thread_id = str(uuid.uuid4())
    data = empty_session_record(thread_id)
    data["title"] = brief["topic"][:100]
    data["auto_title_done"] = True
    data["folder_id"] = _initial_folder(folder_id)
    data[schema.RUNS_KEY] = [run]
    _append_messages(
        data,
        [
            _human_message(
                run, text=_brief_text(run), phase="brief", kind=schema.KIND_BRIEF, msg_id=f"{run['id']}:brief"
            )
        ],
    )
    if not create_session_json(thread_id, data):  # pragma: no cover - a fresh uuid
        raise DiscussionError("conflict", "chat already exists")
    logger.info("AI discussion %s created in chat %s (%d participants)", run["id"], thread_id, len(participants))
    return thread_id, schema.find_run(data, run["id"]) or run


def list_runs(thread_id: str) -> list[dict[str, Any]]:
    healed: list[bool] = []

    def mutate(data: dict[str, Any]) -> bool:
        healed.extend(heal(r) for r in data.get(schema.RUNS_KEY) or [] if isinstance(r, dict))
        return any(healed)

    data = update_session_json(thread_id, mutate)
    if data is None:
        raise DiscussionError("not_found", "chat not found", 404)
    return [schema.public_run(r) for r in data.get(schema.RUNS_KEY) or [] if isinstance(r, dict)]


def get_run(thread_id: str, run_id: str) -> dict[str, Any]:
    run, _ = _mutate_run(thread_id, run_id, lambda _data, _run: _UNCHANGED)
    return schema.public_run(run)


def phase_running(thread_id: str) -> bool:
    """True while a phase of this chat's discussion runs (for the ``/ag-ui/`` refusal, D11)."""
    try:
        data = read_session_json(thread_id)
    except SessionUnavailableError:
        return False
    if not data:
        return False
    return any(
        isinstance(r, dict) and r.get("status") in schema.ACTIVE and lease_valid(r)
        for r in data.get(schema.RUNS_KEY) or []
    )


# ---------------------------------------------------------------------------
# Human actions (D5, D10, D11)
# ---------------------------------------------------------------------------


def apply_input(
    thread_id: str,
    run_id: str,
    *,
    request_id: str,
    action: str,
    text: str,
    speaker_id: str | None,
    actor: str,
) -> tuple[dict[str, Any], bool, list[dict[str, Any]]]:
    """Apply one answer to the pending request, once. Returns (run, already_applied, messages)."""
    if action not in schema.ACTIONS:
        raise DiscussionError("invalid_request", f"unknown action {action!r}", 422)
    text = (text or "").strip()
    stored: list[dict[str, Any]] = []

    def change(data: dict[str, Any], run: dict[str, Any]) -> Any:
        if any(h.get("request_id") == request_id for h in run.get("human_inputs") or []):
            return "already"
        pending = run.get("pending_request")
        if run.get("status") != schema.WAITING_HUMAN or not pending or pending.get("id") != request_id:
            raise DiscussionError("stale_request", "this question is no longer open")
        if action == schema.ACTION_ASK:
            if not text:
                raise DiscussionError("invalid_request", "a question needs text", 422)
            if not any(p["id"] == speaker_id for p in run["participants"]):
                raise DiscussionError("invalid_request", "unknown participant", 422)
        run["context_revision"] = int(run.get("context_revision") or 0) + 1
        run.setdefault("human_inputs", []).append(
            {
                "request_id": request_id,
                "action": action,
                "text": text,
                "speaker_id": speaker_id if action == schema.ACTION_ASK else None,
                "at": schema.now_iso(),
                "by": actor,
                "revision": run["context_revision"],
            }
        )
        run["pending_request"] = None
        if text:
            shown = text
            if action == schema.ACTION_ASK:
                name = next(p["display_name"] for p in run["participants"] if p["id"] == speaker_id)
                shown = f"To {name}: {text}"
            message = _human_message(
                run,
                text=shown,
                phase=str(pending.get("phase") or run["phase"]),
                kind=schema.KIND_HUMAN,
                msg_id=f"{run['id']}:{request_id}",
            )
            _append_messages(data, [message])
            stored.append(message)
        if action == schema.ACTION_FINISH:
            _transition(run, schema.SUMMARIZING)
            run["phase"] = schema.SUMMARY
            run["termination_reason"] = schema.HUMAN_FINISHED
        else:
            _transition(run, schema.RUNNING)
            if action == schema.ACTION_AUTO:
                run["auto"] = True
                run["auto_stopped"] = None
            if action == schema.ACTION_ASK:
                run["asks"] = int(run.get("asks") or 0) + 1
                run["ask"] = {"speaker_id": speaker_id, "text": text, "request_id": request_id, "n": run["asks"]}
        return None

    run, result = _mutate_run(thread_id, run_id, change)
    return schema.public_run(run), result == "already", stored


def request_stop(thread_id: str, run_id: str, kind: str) -> dict[str, Any]:
    """Pause (``pause``) or cancel (``cancel``) a run."""

    def change(_data: dict[str, Any], run: dict[str, Any]) -> Any:
        status = run.get("status")
        if status in schema.TERMINAL:
            raise DiscussionError("invalid_transition", f"the discussion is already {status}")
        local = run_id in _active
        if lease_valid(run) and not local:
            raise DiscussionError("busy", "another server process is running this discussion")
        if local:
            _stop_requests[run_id] = kind
            event = _stop_events.get(run_id)
            if event is not None:
                event.set()
            return _UNCHANGED
        if kind == "cancel":
            _cancel(run)
        else:
            if status not in schema.ACTIVE:
                raise DiscussionError("invalid_transition", f"a {status} discussion cannot be paused")
            _transition(run, schema.PAUSED)
            run["pause_reason"] = schema.PAUSE_USER
            _auto_off(run, schema.AUTO_STOPPED_PAUSED)
        return None

    run, _ = _mutate_run(thread_id, run_id, change)
    return schema.public_run(run)


def _auto_off(run: dict[str, Any], reason: str) -> None:
    """Hand control back to the person (UDR-0217 D4): the run waits after its rounds again."""
    if run.get("auto"):
        run["auto"] = False
        run["auto_stopped"] = reason


def _cancel(run: dict[str, Any]) -> None:
    _transition(run, schema.CANCELLED)
    run["termination_reason"] = schema.REASON_CANCELLED
    run["pending_request"] = None
    run["ask"] = None
    run["lease"] = None


def record_decision(thread_id: str, run_id: str, *, verdict: str, note: str, actor: str) -> dict[str, Any]:
    """The person's decision on a completed run -- a record apart from the AI's (D10)."""
    if verdict not in schema.VERDICTS:
        raise DiscussionError("invalid_request", f"verdict must be one of {', '.join(schema.VERDICTS)}", 422)

    def change(_data: dict[str, Any], run: dict[str, Any]) -> Any:
        if run.get("status") != schema.COMPLETED:
            raise DiscussionError("invalid_transition", "only a completed discussion can be decided")
        run["human_decision"] = {
            "verdict": verdict,
            "note": (note or "").strip()[:2000],
            "decided_at": schema.now_iso(),
            "decided_by": actor,
        }
        run["updated_at"] = schema.now_iso()
        return None

    run, _ = _mutate_run(thread_id, run_id, change)
    return schema.public_run(run)


#: Rounds of a continuation when the request names none (UDR-0216 D4).
CONTINUE_DEFAULT_ROUNDS = 1


def continue_discussion(
    thread_id: str,
    run_id: str,
    *,
    focus: str,
    rounds: int | None,
    actor: str,
    skip_opening: bool | None = None,
    seat_requests: list[dict[str, Any]] | None = None,
    auto: bool = False,
    report: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A NEW run seeded with this run's final state (D11). Never from an ordinary post.

    PRP-0233 (UDR-0216): the focus is the run's own field, the previous conclusion is carried
    into every turn, the run is 1 round unless asked otherwise, it starts at round 1 when a
    conclusion is carried (unless ``skip_opening`` is False), and ``seat_requests`` may replace
    the seats under the create rules. Returns the new run and its stored brief message.
    """
    from app.core.config import settings

    _default_rounds, max_rounds = round_limits()
    rounds = min(CONTINUE_DEFAULT_ROUNDS, max_rounds) if rounds is None else rounds
    if not 1 <= rounds <= max_rounds:
        raise DiscussionError("invalid_request", f"rounds must be between 1 and {max_rounds}", 422)
    focus = (focus or "").strip()
    new_seats: list[dict[str, Any]] | None = None
    if seat_requests is not None:
        try:
            new_seats = seats.resolve_seats(seat_requests, max_seats=int(settings.discussion_max_participants))
        except seats.SeatError as exc:
            raise DiscussionError("invalid_request", str(exc), 422) from exc
    box: dict[str, Any] = {}

    def mutate(data: dict[str, Any]) -> bool | None:
        source = schema.find_run(data, run_id)
        if source is None:
            box["error"] = DiscussionError("not_found", "discussion not found", 404)
            return False
        for run in data.get(schema.RUNS_KEY) or []:
            if isinstance(run, dict):
                heal(run)
        if schema.has_open_run(data):
            box["error"] = DiscussionError("discussion_active", "this chat already has a discussion under way")
            return False
        if source.get("status") not in schema.TERMINAL:
            box["error"] = DiscussionError("invalid_transition", "the discussion has not ended")
            return False
        final = source.get("final") or {}
        seed = None
        if final or source.get("state"):
            seed = {
                "common": list(final.get("agreements") or (source.get("state") or {}).get("common") or []),
                "conflicts": list(final.get("dissent") or (source.get("state") or {}).get("conflicts") or []),
                "missing": list(final.get("open_issues") or (source.get("state") or {}).get("missing") or []),
                "questions_for_human": [],
                "suggest_finish": {"value": False, "reason": ""},
            }
        carried = seed is not None and any(seed[key] for key in ("common", "conflicts", "missing"))
        skip = carried if skip_opening is None else bool(skip_opening)
        if skip and not carried:
            box["error"] = DiscussionError(
                "invalid_request", "there is no previous conclusion to start the mutual review from", 422
            )
            return False
        run = schema.new_run(
            brief=dict(source["brief"]),
            participants=new_seats if new_seats is not None else [dict(p) for p in source["participants"]],
            moderator=dict(source["moderator"]),
            rounds=rounds,
            budget=budget_from_settings(),
            initiated_by=actor,
            parent_run_id=source["id"],
            seed_state=seed if carried else None,
            focus=focus,
            skip_opening=skip,
            auto=auto,
            report=report,
        )
        schema.runs_of(data).append(run)
        text = _brief_text(run, continued=True)
        brief = _human_message(run, text=text, phase="brief", kind=schema.KIND_BRIEF, msg_id=f"{run['id']}:brief")
        _append_messages(data, [brief])
        box["run"] = dict(run)
        box["messages"] = [brief]
        return None

    if update_session_json(thread_id, mutate) is None:
        raise DiscussionError("not_found", "chat not found", 404)
    if "error" in box:
        raise box["error"]
    return schema.public_run(box["run"]), box["messages"]


# ---------------------------------------------------------------------------
# Advancing a run: D3, D4, D6, D9
# ---------------------------------------------------------------------------


@dataclass
class Advance:
    thread_id: str
    run_id: str
    owner: str


def begin_advance(thread_id: str, run_id: str) -> Advance:
    """Take the run's lease by compare-and-set; refuse when someone else holds it (D3)."""
    if run_id in _active:
        raise DiscussionError("busy", "this discussion is already running")
    owner = uuid.uuid4().hex

    def change(_data: dict[str, Any], run: dict[str, Any]) -> Any:
        if run.get("flow_version") != schema.FLOW_VERSION:
            raise DiscussionError("read_only", "this discussion was made by another version and can only be viewed")
        if lease_valid(run):
            raise DiscussionError("busy", "this discussion is already running")
        status = run.get("status")
        if status == schema.WAITING_HUMAN:
            raise DiscussionError("waiting_human", "the discussion waits for your input")
        if status in schema.TERMINAL:
            raise DiscussionError("invalid_transition", f"the discussion is already {status}")
        target = schema.SUMMARIZING if run.get("phase") == schema.SUMMARY else schema.RUNNING
        _transition(run, target)
        run["pause_reason"] = None
        run["error"] = None
        run["lease"] = {"owner": owner, "expires_at": (_now() + timedelta(seconds=LEASE_SECONDS)).isoformat()}
        return None

    _mutate_run(thread_id, run_id, change)
    _active.add(run_id)
    _stop_events[run_id] = asyncio.Event()
    _stop_requests.pop(run_id, None)
    return Advance(thread_id, run_id, owner)


class _Stop(Exception):
    """A pause or cancel request arrived while a speaker was generating."""


class _PhaseTimeout(Exception):
    pass


class _BudgetExhausted(Exception):
    pass


class _LeaseLost(Exception):
    pass


class _ModeratorUnreadable(Exception):
    pass


def _encode(encoder: Any, event: Any) -> str:
    return encoder.encode(event)


class _Runner:
    """One advance: runs phases until the run waits, pauses or ends."""

    def __init__(self, adv: Advance) -> None:
        from ag_ui.encoder import EventEncoder

        self.adv = adv
        self.encoder = EventEncoder()
        self.stop_event = _stop_events[adv.run_id]
        self.deadline = 0.0

    # ---- persistence ------------------------------------------------------

    def load(self) -> tuple[dict[str, Any], dict[str, Any]]:
        data = _read(self.adv.thread_id)
        run = schema.find_run(data, self.adv.run_id)
        if run is None:
            raise DiscussionError("not_found", "discussion not found", 404)
        return data, run

    def write(self, change: Any) -> dict[str, Any]:
        """``change(data, run)`` under the lock, only while this runner owns the lease."""

        def guarded(data: dict[str, Any], run: dict[str, Any]) -> Any:
            lease = run.get("lease")
            if not isinstance(lease, dict) or lease.get("owner") != self.adv.owner:
                raise _LeaseLost()
            lease["expires_at"] = (_now() + timedelta(seconds=LEASE_SECONDS)).isoformat()
            change(data, run)
            run["updated_at"] = schema.now_iso()
            return None

        run, _ = _mutate_run(self.adv.thread_id, self.adv.run_id, guarded)
        return run

    # ---- events -------------------------------------------------------------

    def custom(self, name: str, value: Any) -> str:
        from ag_ui.core import CustomEvent, EventType

        return _encode(self.encoder, CustomEvent(type=EventType.CUSTOM, name=name, value=value))

    def state_event(self, run: dict[str, Any]) -> str:
        return self.custom("discussion_state", schema.public_run(run))

    # ---- one speaker ---------------------------------------------------------

    async def _stream(self, iterator: AsyncIterator[str]) -> AsyncIterator[str]:
        """Pass deltas through; stop at once on a pause / cancel request or the deadline."""
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for delta in iterator:
                    await queue.put(("delta", delta))
                await queue.put(("done", None))
            except Exception as exc:
                await queue.put(("error", exc))

        task = asyncio.create_task(pump())
        try:
            while True:
                getter = asyncio.create_task(queue.get())
                stopper = asyncio.create_task(self.stop_event.wait())
                remaining = self.deadline - time.monotonic()
                done, _ = await asyncio.wait(
                    {getter, stopper}, timeout=max(remaining, 0.0), return_when=asyncio.FIRST_COMPLETED
                )
                if stopper in done:
                    getter.cancel()
                    raise _Stop()
                stopper.cancel()
                if not done:
                    getter.cancel()
                    raise _PhaseTimeout()
                kind, value = getter.result()
                if kind == "done":
                    return
                if kind == "error":
                    raise value
                yield value
        finally:
            if not task.done():
                task.cancel()

    def _check_budget(
        self, run: dict[str, Any], messages: list[tuple[str, str]], *, summary: bool, allowance: int = 0
    ) -> None:
        budget = run["budget"]
        # ``allowance``: the tool results a tool-enabled turn may add (UDR-0217 D2).
        estimate = speakers.estimate_tokens(messages, int(budget["turn_max_output_tokens"])) + allowance
        reserve = 0 if summary else int(budget["summary_reserve"])
        if not summary and int(budget.get("used_tokens") or 0) + estimate + reserve > int(budget["token_budget"]):
            raise _BudgetExhausted()

    async def speak(
        self,
        run: dict[str, Any],
        *,
        participant: dict[str, Any],
        phase: str,
        log: list[dict[str, str]],
        question: str | None = None,
    ) -> AsyncIterator[str]:
        """One participant turn: announce, stream, COMMIT, then say it is committed (D4)."""
        from ag_ui.core import EventType, TextMessageContentEvent, TextMessageEndEvent, TextMessageStartEvent

        from app.discussion.tools import TurnTools

        msg_id = schema.message_id(run["id"], phase, participant["id"])
        messages = seats.participant_messages(run=run, participant=participant, phase=phase, log=log, question=question)
        budget = run["budget"]
        turn_tools = TurnTools(
            list(participant.get("tools") or []),
            model=participant["model"],
            calls_per_turn=int(budget.get("tool_calls_per_turn") or 0),
            result_max_chars=int(budget.get("tool_result_max_chars") or 10_000),
        )
        self._check_budget(run, messages, summary=False, allowance=turn_tools.allowance_tokens())
        revision = int(run.get("context_revision") or 0)
        round_no = schema.phase_round(phase) or int(run.get("round") or 0)
        speaker = {
            "message_id": msg_id,
            "speaker_id": participant["id"],
            "role": participant["role"],
            "display_name": participant["display_name"],
            "color": participant.get("color"),
            "phase": phase,
            "round": round_no,
            "kind": schema.KIND_PARTICIPANT,
        }
        yield self.custom("discussion_speaker", speaker)
        yield _encode(
            self.encoder, TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id=msg_id, role="assistant")
        )
        result = speakers.TurnResult()
        max_out = int(run["budget"]["turn_max_output_tokens"])
        if speakers.use_scripted_replies():
            source = speakers.scripted_turn(participant=participant, phase=phase, log=log, result=result)
        else:
            source = speakers.model_turn(
                model=participant["model"],
                messages=messages,
                max_output_tokens=max_out,
                result=result,
                effort=participant.get("effort"),
                **({"turn_tools": turn_tools} if turn_tools.enabled else {}),
            )
        async for delta in self._stream(source):
            yield _encode(
                self.encoder,
                TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id=msg_id, delta=delta),
            )
        yield _encode(self.encoder, TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=msg_id))
        text = result.text.strip() or "_(no reply)_"
        used = turn_tools.record() or list(result.extra.get("tools") or [])

        def commit(data: dict[str, Any], current: dict[str, Any]) -> None:
            if int(current.get("context_revision") or 0) != revision:
                raise _LeaseLost()  # the brief changed under this reply: never publish it (D5)
            message = schema.build_message(
                msg_id=msg_id,
                role="assistant",
                text=text,
                run_id=current["id"],
                speaker_id=participant["id"],
                speaker_role=participant["role"],
                display_name=participant["display_name"],
                phase=phase,
                round_no=round_no,
                sequence=_next_sequence(current),
                kind=schema.KIND_PARTICIPANT,
                color=participant.get("color"),
                usage=_usage_echo(participant["model"], result),
                tools=used,
            )
            if _append_messages(data, [message]):
                current["budget"]["used_tokens"] = int(current["budget"].get("used_tokens") or 0) + result.tokens

        committed = self.write(commit)
        run.update(committed)
        _ledger(self.adv.thread_id, run["id"], phase, participant["id"], participant["model"], result, "participant")
        done: dict[str, Any] = {"message_id": msg_id, "budget": committed["budget"]}
        if used:
            done["tools"] = used
        yield self.custom("discussion_message_committed", done)

    async def moderate(
        self, run: dict[str, Any], *, phase: str, log: list[dict[str, str]], final: bool
    ) -> AsyncIterator[str]:
        """The moderator organizes (or summarizes); its JSON becomes a card (D8)."""
        from ag_ui.core import EventType, TextMessageContentEvent, TextMessageEndEvent, TextMessageStartEvent

        msg_id = schema.message_id(run["id"], phase, schema.MODERATOR_ID)
        moderator = run["moderator"]
        messages = seats.moderator_messages(run=run, log=log, final=final)
        self._check_budget(run, messages, summary=final)
        yield self.custom(
            "discussion_speaker",
            {
                "message_id": msg_id,
                "speaker_id": schema.MODERATOR_ID,
                "role": schema.MODERATOR_ID,
                "display_name": moderator["display_name"],
                "color": moderator.get("color"),
                "phase": phase,
                "round": schema.phase_round(phase) or int(run.get("round") or 0),
                "kind": schema.KIND_MODERATOR,
                "thinking": True,
            },
        )
        parsed: dict[str, Any] | None = None
        spent = speakers.TurnResult()
        spent.usage = None
        attempt_messages = list(messages)
        for _attempt in range(MODERATOR_ATTEMPTS):
            result = speakers.TurnResult()
            if speakers.use_scripted_replies():
                source = speakers.scripted_turn(participant=None, phase=phase, log=log, result=result, final=final)
            else:
                source = speakers.model_turn(
                    model=moderator["model"],
                    messages=attempt_messages,
                    max_output_tokens=max(int(run["budget"]["turn_max_output_tokens"]), 1500),
                    result=result,
                    schema=seats.FINAL_SCHEMA if final else seats.STATE_SCHEMA,
                )
            async for _delta in self._stream(source):
                pass  # the raw JSON is not shown; the card is
            _ledger(self.adv.thread_id, run["id"], phase, schema.MODERATOR_ID, moderator["model"], result, "moderator")
            spent = _add_usage(spent, result)
            parsed = seats.parse_final(result.text) if final else seats.parse_state(result.text)
            if parsed is not None:
                break
            attempt_messages = [
                *messages,
                ("assistant", result.text[:4000]),
                ("user", "That was not the JSON object asked for. Answer again with ONLY that JSON object."),
            ]
        if parsed is None:
            raise _ModeratorUnreadable()
        text = seats.final_markdown(parsed) if final else seats.state_markdown(parsed)
        yield _encode(
            self.encoder, TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id=msg_id, role="assistant")
        )
        yield _encode(
            self.encoder, TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id=msg_id, delta=text)
        )
        yield _encode(self.encoder, TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=msg_id))

        def commit(data: dict[str, Any], current: dict[str, Any]) -> None:
            message = schema.build_message(
                msg_id=msg_id,
                role="assistant",
                text=text,
                run_id=current["id"],
                speaker_id=schema.MODERATOR_ID,
                speaker_role=schema.MODERATOR_ID,
                display_name=moderator["display_name"],
                phase=phase,
                round_no=schema.phase_round(phase) or int(current.get("round") or 0),
                sequence=_next_sequence(current),
                kind=schema.KIND_MODERATOR,
                color=moderator.get("color"),
                usage=_usage_echo(moderator["model"], spent),
            )
            if _append_messages(data, [message]):
                current["budget"]["used_tokens"] = int(current["budget"].get("used_tokens") or 0) + spent.tokens
            if final:
                current["final"] = parsed
            else:
                current["state"] = parsed

        committed = self.write(commit)
        run.update(committed)
        yield self.custom("discussion_moderator", {"message_id": msg_id, ("final" if final else "state"): parsed})
        yield self.custom("discussion_message_committed", {"message_id": msg_id, "budget": committed["budget"]})

    async def write_report(self, run: dict[str, Any], log: list[dict[str, str]]) -> AsyncIterator[str]:
        """The full report after the stored final summary (UDR-0217 D5).

        Its own call and output cap; skipped when the remaining budget cannot hold it; a failure
        is logged and skipped. Neither ever changes the stored final summary or the completion.
        A Pause or a closed stream propagates, so Continue runs only the report.
        """
        from ag_ui.core import EventType, TextMessageContentEvent, TextMessageEndEvent, TextMessageStartEvent

        msg_id = schema.message_id(run["id"], schema.SUMMARY, schema.REPORT_ID)
        moderator = run["moderator"]
        budget = run["budget"]
        max_out = int(budget.get("report_max_output_tokens") or 10_000)
        messages = seats.report_messages(run=run, log=log)
        estimate = speakers.estimate_tokens(messages, max_out)
        if int(budget.get("used_tokens") or 0) + estimate > int(budget["token_budget"]):
            self._skip_report(run, schema.REPORT_SKIPPED_BUDGET)
            return
        yield self.custom(
            "discussion_speaker",
            {
                "message_id": msg_id,
                "speaker_id": schema.MODERATOR_ID,
                "role": schema.MODERATOR_ID,
                "display_name": moderator["display_name"],
                "color": moderator.get("color"),
                "phase": schema.SUMMARY,
                "round": int(run.get("round") or 0),
                "kind": schema.KIND_REPORT,
            },
        )
        yield _encode(
            self.encoder, TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id=msg_id, role="assistant")
        )
        result = speakers.TurnResult()
        if speakers.use_scripted_replies():
            source = speakers.scripted_turn(participant=None, phase=schema.SUMMARY, log=log, result=result, report=True)
        else:
            source = speakers.model_turn(
                model=moderator["model"], messages=messages, max_output_tokens=max_out, result=result
            )
        self._start_deadline(run)
        try:
            async for delta in self._stream(source):
                yield _encode(
                    self.encoder,
                    TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id=msg_id, delta=delta),
                )
        except (_Stop, _LeaseLost):
            raise
        except Exception as exc:
            yield _encode(self.encoder, TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=msg_id))
            logger.warning(
                "AI discussion %s: the full report was skipped (%s)", run["id"], type(exc).__name__, exc_info=True
            )
            _ledger(
                self.adv.thread_id, run["id"], schema.SUMMARY, schema.REPORT_ID, moderator["model"], result, "report"
            )
            self._skip_report(run, schema.REPORT_SKIPPED_FAILED)
            return
        yield _encode(self.encoder, TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=msg_id))
        text = result.text.strip()
        if not text:
            self._skip_report(run, schema.REPORT_SKIPPED_FAILED)
            return

        def commit(data: dict[str, Any], current: dict[str, Any]) -> None:
            message = schema.build_message(
                msg_id=msg_id,
                role="assistant",
                text=text,
                run_id=current["id"],
                speaker_id=schema.MODERATOR_ID,
                speaker_role=schema.MODERATOR_ID,
                display_name=moderator["display_name"],
                phase=schema.SUMMARY,
                round_no=int(current.get("round") or 0),
                sequence=_next_sequence(current),
                kind=schema.KIND_REPORT,
                color=moderator.get("color"),
                usage=_usage_echo(moderator["model"], result),
            )
            if _append_messages(data, [message]):
                current["budget"]["used_tokens"] = int(current["budget"].get("used_tokens") or 0) + result.tokens

        committed = self.write(commit)
        run.update(committed)
        _ledger(self.adv.thread_id, run["id"], schema.SUMMARY, schema.REPORT_ID, moderator["model"], result, "report")
        yield self.custom("discussion_message_committed", {"message_id": msg_id, "budget": committed["budget"]})

    def _skip_report(self, run: dict[str, Any], reason: str) -> None:
        def change(_data: dict[str, Any], current: dict[str, Any]) -> None:
            current["report_skipped"] = reason

        run.update(self.write(change))

    async def advise(self, run: dict[str, Any], log: list[dict[str, str]]) -> AsyncIterator[str]:
        """Advisory signals only -- they never change the status (D2/D8)."""
        threshold = int(run["budget"].get("advisory_threshold_percent") or 70) / 100
        state = run.get("state") or {}
        text = seats.state_markdown(state) + "\n\n" + "\n\n".join(f"[{e['speaker']}] {e['text']}" for e in log)
        signals = await speakers.advisory(text)
        if signals is None:
            finish = state.get("suggest_finish") or {}
            signals = {"source": "moderator", "suggest": bool(finish.get("value")), "reason": finish.get("reason", "")}
        else:
            usage = signals.pop("usage", None)
            if usage and signals.get("source") == "decision":
                from app.usage.ledger import append_discussion_usage

                append_discussion_usage(
                    kind="helper",
                    purpose="discussion_advisory",
                    turn={**usage, "model_calls": 1},
                    model=signals.get("offering"),
                    provider="azure-openai",
                    thread_id=self.adv.thread_id,
                    run_id=run["id"],
                    node=run.get("phase"),
                    agent=schema.MODERATOR_ID,
                )
            signals["suggest"] = any(
                isinstance(signals.get(k), (int, float)) and signals[k] >= threshold
                for k in ("repeating", "sufficient")
            )
            signals["reason"] = (
                "The points are being repeated."
                if (signals.get("repeating") or 0) >= threshold
                else "The wanted outcome looks covered."
                if signals["suggest"]
                else ""
            )

        def store(_data: dict[str, Any], current: dict[str, Any]) -> None:
            current["advisory"] = signals

        run.update(self.write(store))
        yield self.custom("discussion_advisory", signals)

    # ---- the phases ----------------------------------------------------------

    def _phase_messages(
        self, data: dict[str, Any], run_id: str, *, phases: set[str], since: int = 0
    ) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for m in data.get("messages") or []:
            meta = m.get(schema.MESSAGE_KEY) if isinstance(m, dict) else None
            if not isinstance(meta, dict) or meta.get("run_id") != run_id:
                continue
            if meta.get("kind") != schema.KIND_PARTICIPANT:
                continue
            phase = str(meta.get("phase"))
            if phase in phases or (phase.startswith("ask:") and int(meta.get("sequence") or 0) > since):
                text = "".join(c.get("text", "") for c in m.get("contents") or [] if isinstance(c, dict))
                out.append({"speaker": str(meta.get("display_name")), "text": text})
        return out

    def _first_sequence(self, data: dict[str, Any], run_id: str, phase: str) -> int:
        for m in data.get("messages") or []:
            meta = m.get(schema.MESSAGE_KEY) if isinstance(m, dict) else None
            if isinstance(meta, dict) and meta.get("run_id") == run_id and meta.get("phase") == phase:
                return int(meta.get("sequence") or 0)
        return 0

    def _start_deadline(self, run: dict[str, Any]) -> None:
        self.deadline = time.monotonic() + int(run["budget"]["phase_timeout_seconds"])

    async def _speakers(self, run: dict[str, Any], phase: str, base_log: list[dict[str, str]]) -> AsyncIterator[str]:
        """Every participant in the fixed order; one already stored in this phase is skipped (D4/D6).

        The opening gives everyone the SAME snapshot without the others' replies (D7); a round
        gives each speaker the previous phase plus this round's earlier speakers.
        """
        for participant in run["participants"]:
            fresh, current = self.load()
            if schema.message_id(current["id"], phase, participant["id"]) in schema.stored_message_ids(fresh):
                continue
            log = (
                [] if phase == schema.OPENING else base_log + self._phase_messages(fresh, current["id"], phases={phase})
            )
            async for chunk in self.speak(current, participant=participant, phase=phase, log=log):
                yield chunk

    def _wait(self, run: dict[str, Any], *, next_phase: str) -> None:
        request = {
            "id": f"rq_{uuid.uuid4().hex[:12]}",
            "phase": next_phase,
            "question": seats.waiting_question(run.get("state")),
            "options": list(schema.ACTIONS),
        }

        def change(_data: dict[str, Any], current: dict[str, Any]) -> None:
            _transition(current, schema.WAITING_HUMAN)
            current["phase"] = next_phase
            current["pending_request"] = request
            current["ask"] = None
            current["lease"] = None

        run.update(self.write(change))

    def _to_summary(self, run: dict[str, Any], reason: str) -> None:
        def change(_data: dict[str, Any], current: dict[str, Any]) -> None:
            _transition(current, schema.SUMMARIZING)
            current["phase"] = schema.SUMMARY
            current["termination_reason"] = current.get("termination_reason") or reason

        run.update(self.write(change))

    def _set_phase(self, run: dict[str, Any], phase: str, round_no: int | None = None) -> None:
        def change(_data: dict[str, Any], current: dict[str, Any]) -> None:
            current["phase"] = phase
            if round_no is not None:
                current["round"] = round_no

        run.update(self.write(change))

    async def run(self) -> AsyncIterator[str]:
        """Run phases until the run waits, ends or pauses; returns the outcome via ``self.outcome``."""
        self.outcome = schema.PAUSED
        while True:
            data, run = self.load()
            yield self.state_event(run)
            phase = str(run.get("phase"))
            if run.get("status") == schema.SUMMARIZING or phase == schema.SUMMARY:
                self._start_deadline(run)
                log = self._phase_messages(
                    data,
                    run["id"],
                    phases={schema.OPENING, *(schema.round_phase(n) for n in range(1, int(run["rounds"]) + 1))},
                )
                if not run.get("final"):
                    async for chunk in self.moderate(run, phase=schema.SUMMARY, log=log, final=True):
                        yield chunk
                report_id = schema.message_id(run["id"], schema.SUMMARY, schema.REPORT_ID)
                if (
                    run.get("report")
                    and not run.get("report_skipped")
                    and report_id not in schema.stored_message_ids(self.load()[0])
                ):
                    async for chunk in self.write_report(run, log):
                        yield chunk

                def done(_data: dict[str, Any], current: dict[str, Any]) -> None:
                    _transition(current, schema.COMPLETED)
                    current["termination_reason"] = current.get("termination_reason") or schema.MAX_ROUNDS
                    current["lease"] = None

                run.update(self.write(done))
                yield self.state_event(run)
                self.outcome = schema.COMPLETED
                return

            ask = run.get("ask")
            if ask:
                self._start_deadline(run)
                participant = next(p for p in run["participants"] if p["id"] == ask["speaker_id"])
                ask_phase = schema.ask_phase(int(ask.get("n") or 1))
                if schema.message_id(run["id"], ask_phase, participant["id"]) not in schema.stored_message_ids(data):
                    log = self._phase_messages(data, run["id"], phases={_previous_phase(phase)})
                    try:
                        async for chunk in self.speak(
                            run, participant=participant, phase=ask_phase, log=log, question=ask["text"]
                        ):
                            yield chunk
                    except _BudgetExhausted:
                        self._to_summary(run, schema.BUDGET)
                        continue
                self._wait(run, next_phase=phase)
                yield self.state_event(run)
                yield self.custom("discussion_request", run["pending_request"])
                self.outcome = schema.WAITING_HUMAN
                return

            try:
                if phase == schema.OPENING:
                    self._start_deadline(run)
                    async for chunk in self._speakers(run, schema.OPENING, []):
                        yield chunk
                    self._set_phase(run, schema.FRAMING)
                    continue

                if phase == schema.FRAMING:
                    self._start_deadline(run)
                    log = self._phase_messages(data, run["id"], phases={schema.OPENING})
                    async for chunk in self.moderate(run, phase=schema.FRAMING, log=log, final=False):
                        yield chunk
                    async for chunk in self.advise(run, log):
                        yield chunk
                    if run.get("auto"):
                        self._set_phase(run, schema.round_phase(1))
                        continue
                    self._wait(run, next_phase=schema.round_phase(1))
                    yield self.state_event(run)
                    yield self.custom("discussion_request", run["pending_request"])
                    self.outcome = schema.WAITING_HUMAN
                    return

                n = schema.phase_round(phase)
                if n is None:
                    raise DiscussionError("invalid_state", f"unknown phase {phase!r}", 500)
                self._start_deadline(run)
                prev = _previous_phase(phase)
                since = self._first_sequence(data, run["id"], prev)
                base_log = self._phase_messages(data, run["id"], phases={prev}, since=since)
                async for chunk in self._speakers(run, phase, base_log):
                    yield chunk
                fresh, run = self.load()
                log = self._phase_messages(fresh, run["id"], phases={phase})
                if schema.message_id(run["id"], phase, schema.MODERATOR_ID) not in schema.stored_message_ids(fresh):
                    async for chunk in self.moderate(run, phase=phase, log=log, final=False):
                        yield chunk
                    async for chunk in self.advise(run, log):
                        yield chunk
                if n >= int(run["rounds"]):
                    self._set_phase(run, phase, n)
                    self._to_summary(run, schema.MAX_ROUNDS)
                    continue
                if run.get("auto"):
                    # A finish suggestion is only shown; the run keeps to its rounds (UDR-0217 D4).
                    self._set_phase(run, schema.round_phase(n + 1), n)
                    continue
                self._set_phase(run, phase, n)
                self._wait(run, next_phase=schema.round_phase(n + 1))
                yield self.state_event(run)
                yield self.custom("discussion_request", run["pending_request"])
                self.outcome = schema.WAITING_HUMAN
                return
            except _BudgetExhausted:
                self._to_summary(run, schema.BUDGET)
                continue
            except _PhaseTimeout:
                self._to_summary(run, schema.TIME)
                continue


def _previous_phase(phase: str) -> str:
    n = schema.phase_round(phase)
    if n is None or n <= 1:
        return schema.OPENING
    return schema.round_phase(n - 1)


def _add_usage(total: speakers.TurnResult, part: speakers.TurnResult) -> speakers.TurnResult:
    if not part.usage:
        return total
    merged = dict(total.usage or {})
    for key, value in part.usage.items():
        if isinstance(value, int) and not isinstance(value, bool):
            merged[key] = int(merged.get(key) or 0) + value
    total.usage = merged
    total.model_calls += part.model_calls
    total.scripted = total.scripted or part.scripted
    return total


def _usage_echo(model: str, result: speakers.TurnResult) -> dict[str, Any] | None:
    """The message's ``usage`` (model + the turn), so the chat shows who answered with what."""
    usage: dict[str, Any] = {"model": model, "run_target": "AI discussion"}
    if result.usage:
        usage["turn"] = {k: v for k, v in result.usage.items() if isinstance(v, int)}
        if "input_token_count" in result.usage:
            usage["input_token_count"] = result.usage["input_token_count"]
        if "output_token_count" in result.usage:
            usage["output_token_count"] = result.usage["output_token_count"]
    return usage


def _ledger(
    thread_id: str, run_id: str, phase: str, speaker_id: str, model: str, result: speakers.TurnResult, purpose: str
) -> None:
    if result.scripted or not result.usage:
        return
    from app.usage.ledger import append_discussion_usage

    append_discussion_usage(
        kind="turn",
        purpose=f"discussion_{purpose}",
        turn=result.usage,
        model=model,
        thread_id=thread_id,
        run_id=run_id,
        node=phase,
        agent=speaker_id,
    )


def _finish_interrupted(adv: Advance, *, reason: str, error: str | None = None) -> None:
    """Close an advance that did not reach a wait or an end: pause, or cancel on request.

    Synchronous on purpose: it also runs while the generator is being closed (GeneratorExit),
    where nothing may be awaited.
    """
    requested = _stop_requests.pop(adv.run_id, None)

    def change(_data: dict[str, Any], run: dict[str, Any]) -> Any:
        lease = run.get("lease")
        if not isinstance(lease, dict) or lease.get("owner") != adv.owner:
            return _UNCHANGED
        if requested == "cancel":
            _cancel(run)
            return None
        if run.get("status") in schema.ACTIVE:
            _transition(run, schema.PAUSED)
            run["pause_reason"] = schema.PAUSE_USER if requested == "pause" else reason
            if requested == "pause":
                _auto_off(run, schema.AUTO_STOPPED_PAUSED)
            if error:
                run["error"] = error[:500]
        run["lease"] = None
        return None

    try:
        _mutate_run(adv.thread_id, adv.run_id, change)
    except Exception:
        logger.warning("could not record the end of discussion %s", adv.run_id, exc_info=True)


def _release(adv: Advance) -> None:
    def change(_data: dict[str, Any], run: dict[str, Any]) -> Any:
        lease = run.get("lease")
        if isinstance(lease, dict) and lease.get("owner") == adv.owner:
            run["lease"] = None
            return None
        return _UNCHANGED

    try:
        _mutate_run(adv.thread_id, adv.run_id, change)
    except Exception:
        logger.warning("could not release discussion %s", adv.run_id, exc_info=True)


async def run_advance(adv: Advance) -> AsyncIterator[str]:
    """The phase stream of ``POST .../advance`` (CTR-0273)."""
    from ag_ui.core import EventType, RunErrorEvent, RunFinishedEvent, RunStartedEvent

    runner = _Runner(adv)
    advance_id = f"adv_{adv.owner[:12]}"
    yield _encode(
        runner.encoder, RunStartedEvent(type=EventType.RUN_STARTED, thread_id=adv.thread_id, run_id=advance_id)
    )

    async def refresh() -> None:
        while True:
            await asyncio.sleep(LEASE_REFRESH_SECONDS)
            try:
                runner.write(lambda _d, _r: None)
            except Exception:
                return

    refresher = asyncio.create_task(refresh())
    finished = False
    outcome = schema.PAUSED
    try:
        try:
            async for chunk in runner.run():
                yield chunk
            outcome = runner.outcome
            finished = True
        except _Stop:
            requested = _stop_requests.get(adv.run_id)
            _finish_interrupted(adv, reason=schema.PAUSE_USER)
            finished = True
            outcome = schema.CANCELLED if requested == "cancel" else schema.PAUSED
            _data, run = runner.load()
            yield runner.state_event(run)
        except _LeaseLost:
            finished = True
            outcome = "superseded"
        except (_ModeratorUnreadable, _PhaseTimeout) as exc:
            message = (
                "The moderator's answer could not be read."
                if isinstance(exc, _ModeratorUnreadable)
                else "The summary took longer than the time per phase."
            )
            _finish_interrupted(adv, reason=schema.PAUSE_ERROR, error=message)
            finished = True
            outcome = schema.PAUSED
            yield _encode(
                runner.encoder,
                RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message=f"{message} Continue to try again.",
                    code="moderator_unreadable" if isinstance(exc, _ModeratorUnreadable) else "phase_timeout",
                ),
            )
            _data, run = runner.load()
            yield runner.state_event(run)
        except DiscussionError as exc:
            _finish_interrupted(adv, reason=schema.PAUSE_ERROR, error=exc.message)
            finished = True
            yield _encode(runner.encoder, RunErrorEvent(type=EventType.RUN_ERROR, message=exc.message, code=exc.code))
        except Exception as exc:
            logger.exception("AI discussion %s phase failed", adv.run_id)
            _finish_interrupted(adv, reason=schema.PAUSE_ERROR, error=f"{type(exc).__name__}: {exc}")
            finished = True
            yield _encode(
                runner.encoder,
                RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message=f"The discussion stopped: {type(exc).__name__}. Continue to try again.",
                    code="phase_failed",
                ),
            )
            _data, run = runner.load()
            yield runner.state_event(run)
        yield _encode(
            runner.encoder,
            RunFinishedEvent(
                type=EventType.RUN_FINISHED, thread_id=adv.thread_id, run_id=advance_id, result={"outcome": outcome}
            ),
        )
    finally:
        refresher.cancel()
        if not finished:
            # The client closed the stream (or the server is stopping): interrupt (D6).
            _finish_interrupted(adv, reason=schema.PAUSE_INTERRUPTED)
        else:
            _release(adv)
        _active.discard(adv.run_id)
        _stop_events.pop(adv.run_id, None)
        _stop_requests.pop(adv.run_id, None)


__all__ = [
    "LEASE_SECONDS",
    "TRANSITIONS",
    "Advance",
    "DiscussionError",
    "apply_input",
    "begin_advance",
    "continue_discussion",
    "create_discussion",
    "get_run",
    "heal",
    "lease_valid",
    "limits",
    "list_runs",
    "phase_running",
    "record_decision",
    "request_stop",
    "run_advance",
]
