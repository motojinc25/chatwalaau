"""Speaker selection for an AI discussion round (CTR-0274, PRP-0235, UDR-0218 D4 / D5).

The decision model only SCORES each seat ("would this seat add needed, new expertise next
round?"); the rule below, which is code, chooses who speaks. The moderator never takes part
(UDR-0215 D8). Everything here is a pure function of plain dicts: no I/O, no model.

Rule, in this order:

1. chosen  -- seats whose score reaches the threshold;
2. forced  -- seats left out of the last ``max_skipped_rounds`` rounds in a row;
3. filled  -- while fewer than ``min_speakers``, the highest-scored seats not yet in (ties: seat order);
4. unread  -- a seat whose answer could not be read is kept in (fail open);

and the speakers are those, in seat order. Without scores (no decision model, a failed call)
every seat speaks and the record says why.
"""

from __future__ import annotations

from typing import Any

from app.discussion import schema

MODE_ALL = "all"
MODE_DECISION = "decision"
MODES: tuple[str, ...] = (MODE_ALL, MODE_DECISION)

SOURCE_DECISION = "decision"
SOURCE_PERSON = "person"
SOURCE_FALLBACK = "fallback"

#: Why a round fell back to every seat.
FALLBACK_NO_ANSWER = "The decision model did not answer -- everyone speaks."

DEFAULT_THRESHOLD_PERCENT = 40
DEFAULT_MIN_SPEAKERS = 2
DEFAULT_MAX_SKIPPED_ROUNDS = 1


def mode_of(run: dict[str, Any]) -> str:
    """The run's mode; a run made before PRP-0235 has none and lets everyone speak."""
    mode = run.get("speaker_selection")
    return mode if mode in MODES else MODE_ALL


def question_name(index: int) -> str:
    """The decision question of a seat (seat ids hold '-', which question names may not)."""
    return f"seat_{index}"


def seat_questions(participants: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One ``noul`` question per seat, in seat order."""
    out: dict[str, dict[str, Any]] = {}
    for index, p in enumerate(participants):
        charter = " ".join(str(p.get("charter") or "").split())[:400]
        out[question_name(index)] = {
            "type": "noul",
            "instructions": (
                f"Would {p.get('display_name')} ({charter}) add needed, new expertise on the open points "
                "in the next round, rather than repeat what was already said?"
            ),
        }
    return out


def scores_from_answers(participants: list[dict[str, Any]], answers: dict[str, Any]) -> dict[str, float | None]:
    """``{seat_id: probability | None}`` from the decision answers (None = unreadable)."""
    out: dict[str, float | None] = {}
    for index, p in enumerate(participants):
        answer = answers.get(question_name(index)) or {}
        value = answer.get("probability") if isinstance(answer, dict) else None
        out[p["id"]] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return out


def _limits(run: dict[str, Any]) -> tuple[float, int, int]:
    budget = run.get("budget") or {}
    threshold = int(budget.get("selection_threshold_percent") or DEFAULT_THRESHOLD_PERCENT) / 100
    minimum = int(budget.get("selection_min_speakers") or DEFAULT_MIN_SPEAKERS)
    max_skipped = int(budget.get("selection_max_skipped_rounds") or DEFAULT_MAX_SKIPPED_ROUNDS)
    return threshold, max(minimum, 1), max(max_skipped, 1)


def _skipped_streak(run: dict[str, Any], seat_id: str, round_no: int) -> int:
    """Rounds in a row (just before ``round_no``) this seat did not speak in."""
    streak = 0
    selections = run.get("selections") or {}
    for n in range(round_no - 1, 0, -1):
        record = selections.get(str(n))
        if not isinstance(record, dict) or seat_id in (record.get("speakers") or []):
            break
        streak += 1
    return streak


def choose(run: dict[str, Any], round_no: int, scores: dict[str, float | None] | None) -> dict[str, Any]:
    """The selection record of ``round_no`` (CTR-0271 ``selections[n]``)."""
    participants = run["participants"]
    order = [p["id"] for p in participants]
    threshold, minimum, max_skipped = _limits(run)
    base: dict[str, Any] = {
        "round": round_no,
        "threshold_percent": round(threshold * 100),
        "at": schema.now_iso(),
    }
    if scores is None:
        return {
            **base,
            "source": SOURCE_FALLBACK,
            "speakers": order,
            "skipped": [],
            "scores": {},
            "forced": [],
            "filled": [],
            "fallback_reason": FALLBACK_NO_ANSWER,
        }
    chosen = [sid for sid in order if isinstance(scores.get(sid), float) and scores[sid] >= threshold]
    forced = [sid for sid in order if sid not in chosen and _skipped_streak(run, sid, round_no) >= max_skipped]
    unread = [sid for sid in order if scores.get(sid) is None and sid not in chosen and sid not in forced]
    picked = set(chosen) | set(forced) | set(unread)
    filled: list[str] = []
    want = min(minimum, len(order))
    rest = sorted(
        (sid for sid in order if sid not in picked), key=lambda sid: (-(scores.get(sid) or 0.0), order.index(sid))
    )
    for sid in rest:
        if len(picked) >= want:
            break
        picked.add(sid)
        filled.append(sid)
    speakers = [sid for sid in order if sid in picked]
    return {
        **base,
        "source": SOURCE_DECISION,
        "speakers": speakers,
        "skipped": [sid for sid in order if sid not in picked],
        "scores": {sid: (None if scores.get(sid) is None else round(float(scores[sid]), 3)) for sid in order},
        "forced": forced,
        "filled": filled,
        "unread": unread,
        "fallback_reason": None,
    }


def by_person(run: dict[str, Any], round_no: int, speaker_ids: list[str]) -> dict[str, Any]:
    """The person's own choice for the next round (it replaces any computed selection)."""
    order = [p["id"] for p in run["participants"]]
    wanted = set(speaker_ids)
    previous = (run.get("selections") or {}).get(str(round_no)) or {}
    threshold, _minimum, _max = _limits(run)
    return {
        "round": round_no,
        "source": SOURCE_PERSON,
        "speakers": [sid for sid in order if sid in wanted],
        "skipped": [sid for sid in order if sid not in wanted],
        "scores": dict(previous.get("scores") or {}),
        "forced": [],
        "filled": [],
        "fallback_reason": None,
        "threshold_percent": round(threshold * 100),
        "at": schema.now_iso(),
    }


def speakers_of(run: dict[str, Any], round_no: int) -> list[str] | None:
    """The seats that speak in ``round_no``, or None for everyone (mode all, or no record)."""
    if mode_of(run) != MODE_DECISION:
        return None
    record = (run.get("selections") or {}).get(str(round_no))
    if not isinstance(record, dict):
        return None
    return list(record.get("speakers") or [])


def summary_line(run: dict[str, Any], record: dict[str, Any]) -> str:
    """One line for the log and the export: who speaks, who not, and why."""
    names = {p["id"]: p["display_name"] for p in run["participants"]}
    scores = record.get("scores") or {}

    def label(sid: str) -> str:
        score = scores.get(sid)
        tags = []
        if score is not None:
            tags.append(f"{score:.2f}")
        if sid in (record.get("forced") or []):
            tags.append("forced: left out last round")
        if sid in (record.get("filled") or []):
            tags.append("added to reach the minimum")
        if sid in (record.get("unread") or []):
            tags.append("score unreadable")
        return names.get(sid, sid) + (f" ({', '.join(tags)})" if tags else "")

    source = record.get("source")
    head = {
        SOURCE_DECISION: "chosen by the decision model",
        SOURCE_PERSON: "chosen by you",
        SOURCE_FALLBACK: "everyone",
    }.get(str(source), str(source))
    line = f"Round {record.get('round')} -- {head}: " + ", ".join(label(s) for s in record.get("speakers") or [])
    skipped = record.get("skipped") or []
    if skipped:
        line += ". Not this round: " + ", ".join(label(s) for s in skipped)
    if record.get("fallback_reason"):
        line += f". {record['fallback_reason']}"
    return line


def participation(data: dict[str, Any], run: dict[str, Any]) -> list[dict[str, Any]]:
    """Per seat: rounds spoken / rounds held, messages and tool uses (code-computed)."""
    rounds_held: set[int] = set()
    spoken: dict[str, set[int]] = {p["id"]: set() for p in run["participants"]}
    messages: dict[str, int] = dict.fromkeys(spoken, 0)
    tool_uses: dict[str, int] = dict.fromkeys(spoken, 0)
    for m in data.get("messages") or []:
        meta = m.get(schema.MESSAGE_KEY) if isinstance(m, dict) else None
        if not isinstance(meta, dict) or meta.get("run_id") != run["id"]:
            continue
        n = schema.phase_round(str(meta.get("phase") or ""))
        if n is not None:
            rounds_held.add(n)
        sid = meta.get("speaker_id")
        if meta.get("kind") != schema.KIND_PARTICIPANT or sid not in spoken:
            continue
        messages[sid] += 1
        tool_uses[sid] += len(meta.get("tools") or [])
        if n is not None:
            spoken[sid].add(n)
    return [
        {
            "speaker_id": p["id"],
            "display_name": p["display_name"],
            "rounds_spoken": len(spoken[p["id"]]),
            "rounds_held": len(rounds_held),
            "messages": messages[p["id"]],
            "tool_uses": tool_uses[p["id"]],
        }
        for p in run["participants"]
    ]


__all__ = [
    "MODES",
    "MODE_ALL",
    "MODE_DECISION",
    "by_person",
    "choose",
    "mode_of",
    "participation",
    "scores_from_answers",
    "seat_questions",
    "speakers_of",
    "summary_line",
]
