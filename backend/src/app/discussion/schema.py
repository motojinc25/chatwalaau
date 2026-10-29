"""AI Discussion run schema (CTR-0271, PRP-0232, UDR-0215 D1/D2/D4).

A discussion lives in the chat's session file next to its messages:

- ``discussion_runs[]`` -- one record per run (this module's ``new_run``);
- ``messages[].discussion`` -- the attribution of a message written by a run
  (``build_message``). Only the backend writes these messages (D4).

Everything here is a pure function of plain dicts: no I/O, no model, no MAF.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
import uuid

#: The flow this code runs. A run records the version it was created with; a run with
#: another version is read-only (UDR-0215 D1), never advanced by code that does not know it.
FLOW_VERSION = 1

RUNS_KEY = "discussion_runs"
MESSAGE_KEY = "discussion"

# ---- statuses (D2) --------------------------------------------------------------------
CREATED = "created"
RUNNING = "running"
WAITING_HUMAN = "waiting_human"
PAUSED = "paused"
SUMMARIZING = "summarizing"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"

STATUSES: tuple[str, ...] = (CREATED, RUNNING, WAITING_HUMAN, PAUSED, SUMMARIZING, COMPLETED, CANCELLED, FAILED)
TERMINAL: frozenset[str] = frozenset({COMPLETED, CANCELLED, FAILED})
#: A phase may be running (a lease is meaningful) only in these.
ACTIVE: frozenset[str] = frozenset({RUNNING, SUMMARIZING})

# ---- phases (D2) ----------------------------------------------------------------------
OPENING = "opening"
FRAMING = "framing"
SUMMARY = "summary"


def round_phase(n: int) -> str:
    return f"round:{n}"


def ask_phase(n: int) -> str:
    return f"ask:{n}"


def phase_round(phase: str) -> int | None:
    """The round number of a ``round:<n>`` phase, else None."""
    if phase.startswith("round:"):
        try:
            return int(phase.split(":", 1)[1])
        except ValueError:
            return None
    return None


# ---- termination and pause reasons (D2) -----------------------------------------------
HUMAN_FINISHED = "human_finished"
MAX_ROUNDS = "max_rounds"
BUDGET = "budget"
TIME = "time"
REASON_CANCELLED = "cancelled"
REASON_FAILED = "failed"
TERMINATION_REASONS: tuple[str, ...] = (HUMAN_FINISHED, MAX_ROUNDS, BUDGET, TIME, REASON_CANCELLED, REASON_FAILED)

PAUSE_USER = "user"
PAUSE_INTERRUPTED = "interrupted"
PAUSE_ERROR = "error"

# ---- human input actions and decisions (D5 / D10) -------------------------------------
ACTION_ANSWER = "answer"
ACTION_CONTINUE = "continue"
ACTION_ASK = "ask"
ACTION_FINISH = "finish"
#: PRP-0234 (UDR-0217 D4): continue, then run the remaining rounds without waiting.
ACTION_AUTO = "auto"
ACTIONS: tuple[str, ...] = (ACTION_ANSWER, ACTION_CONTINUE, ACTION_ASK, ACTION_FINISH, ACTION_AUTO)

# ---- auto mode stop (Pause) and report skips (UDR-0217 D4 / D5) --------------------------------
AUTO_STOPPED_PAUSED = "paused"
REPORT_SKIPPED_BUDGET = "budget"
REPORT_SKIPPED_FAILED = "failed"

VERDICTS: tuple[str, ...] = ("adopt", "hold", "rediscuss")

# ---- message kinds ----------------------------------------------------------------------
KIND_PARTICIPANT = "participant"
KIND_MODERATOR = "moderator"
KIND_HUMAN = "human"
KIND_BRIEF = "brief"
#: The full report after the final summary (UDR-0217 D5); stored under REPORT_ID in the summary phase.
KIND_REPORT = "report"
REPORT_ID = "report"

MODERATOR_ID = "moderator"
HUMAN_ID = "human"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_run_id() -> str:
    return f"dr_{uuid.uuid4().hex[:16]}"


def message_id(run_id: str, phase: str, speaker_id: str) -> str:
    """The deterministic id of a speaker's message in one phase (UDR-0215 D4).

    A message already stored under this id means the speaker has spoken in this phase, so
    Continue skips it and a retry can never post it twice.
    """
    return f"{run_id}:{phase}:{speaker_id}"


def new_run(
    *,
    brief: dict[str, str],
    participants: list[dict[str, Any]],
    moderator: dict[str, Any],
    rounds: int,
    budget: dict[str, int],
    initiated_by: str,
    parent_run_id: str | None = None,
    seed_state: dict[str, Any] | None = None,
    focus: str = "",
    skip_opening: bool = False,
    auto: bool = False,
    report: bool = False,
) -> dict[str, Any]:
    """A run record in status ``created`` (CTR-0271).

    A continuation (PRP-0233, UDR-0216) keeps the seed twice: ``state`` is the moderator's
    working state, which round summaries replace, and ``carried`` is the previous conclusion,
    which every turn of the run sees. ``skip_opening`` creates the run at ``round:1``.

    PRP-0234 (UDR-0217 D4 / D5): ``auto`` skips the waits after rounds, ``report`` asks for the
    full report after the final summary.
    """
    now = now_iso()
    run: dict[str, Any] = {
        "id": new_run_id(),
        "flow_version": FLOW_VERSION,
        "status": CREATED,
        "phase": round_phase(1) if skip_opening else OPENING,
        "round": 0,
        "rounds": rounds,
        "brief": dict(brief),
        "participants": [dict(p) for p in participants],
        "moderator": dict(moderator),
        "human_inputs": [],
        "context_revision": 0,
        "pending_request": None,
        "ask": None,
        "asks": 0,
        "lease": None,
        "budget": {**budget, "used_tokens": 0},
        "state": dict(seed_state) if seed_state else None,
        "advisory": None,
        "final": None,
        "termination_reason": None,
        "pause_reason": None,
        "error": None,
        "human_decision": None,
        "parent_run_id": parent_run_id,
        "auto": bool(auto),
        "auto_stopped": None,
        "report": bool(report),
        "report_skipped": None,
        "initiated_by": initiated_by,
        "created_at": now,
        "updated_at": now,
    }
    if parent_run_id is not None:
        run["carried"] = dict(seed_state) if seed_state else None
        run["focus"] = focus
        run["skip_opening"] = skip_opening
    return run


def runs_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The session's run list (created on demand so a mutator can append to it)."""
    runs = data.get(RUNS_KEY)
    if not isinstance(runs, list):
        runs = []
        data[RUNS_KEY] = runs
    return runs


def find_run(data: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    for run in data.get(RUNS_KEY) or []:
        if isinstance(run, dict) and run.get("id") == run_id:
            return run
    return None


def open_run(data: dict[str, Any]) -> dict[str, Any] | None:
    """The chat's non-terminal run, if any (at most one exists, D11)."""
    for run in data.get(RUNS_KEY) or []:
        if isinstance(run, dict) and run.get("status") not in TERMINAL:
            return run
    return None


def has_open_run(data: dict[str, Any]) -> bool:
    return open_run(data) is not None


def build_message(
    *,
    msg_id: str,
    role: str,
    text: str,
    run_id: str,
    speaker_id: str,
    speaker_role: str,
    display_name: str,
    phase: str,
    round_no: int,
    sequence: int,
    kind: str,
    color: str | None = None,
    usage: dict[str, Any] | None = None,
    tools: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A stored message in the CTR-0014 shape plus the CTR-0271 ``discussion`` field.

    ``tools`` is what a participant turn used (name and query, never the result; UDR-0217 D3).
    """
    from app.session.provider import MESSAGE_TYPE_ID

    attribution: dict[str, Any] = {
        "run_id": run_id,
        "speaker_id": speaker_id,
        "role": speaker_role,
        "display_name": display_name,
        "phase": phase,
        "round": round_no,
        "sequence": sequence,
        "kind": kind,
        "status": "completed",
    }
    if color:
        attribution["color"] = color
    if tools:
        attribution["tools"] = [dict(t) for t in tools]
    msg: dict[str, Any] = {
        "type": MESSAGE_TYPE_ID,
        "role": role,
        "contents": [{"type": "text", "text": text}],
        "message_id": msg_id,
        MESSAGE_KEY: attribution,
    }
    if usage:
        msg["usage"] = usage
    return msg


def stored_message_ids(data: dict[str, Any]) -> set[str]:
    return {str(m.get("message_id")) for m in data.get("messages") or [] if isinstance(m, dict) and m.get("message_id")}


def public_run(run: dict[str, Any]) -> dict[str, Any]:
    """The run as the API returns it: the lease owner is internal and never leaves."""
    out = dict(run)
    lease = run.get("lease")
    out["lease"] = {"expires_at": lease.get("expires_at")} if isinstance(lease, dict) else None
    return out


__all__ = [
    "ACTIONS",
    "ACTIVE",
    "FLOW_VERSION",
    "MESSAGE_KEY",
    "RUNS_KEY",
    "STATUSES",
    "TERMINAL",
    "TERMINATION_REASONS",
    "VERDICTS",
    "build_message",
    "find_run",
    "has_open_run",
    "message_id",
    "new_run",
    "open_run",
    "phase_round",
    "public_run",
    "round_phase",
    "runs_of",
]
