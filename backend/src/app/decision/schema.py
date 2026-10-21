"""Decision questions: validation and answer normalization (PRP-0219, UDR-0201 D5-D7).

Two pure halves, no I/O:

* :func:`validate_request` checks ``state`` and ``questions`` BEFORE any request
  (UDR-0201 D6), so an oversized or malformed call becomes a list of problems the
  calling model can fix, rather than a 400 it cannot interpret. The bounds come from
  App Settings (group ``decision``) through :class:`DecisionLimits`.
* :func:`read_answers` turns the service's ``answers`` object into the normalized
  result shape of UDR-0201 D7.

The service documents only ``answers.<name>.type`` and ``answers.<name>.choice``
(Microsoft Learn, 2026-10-07). Every other wire field is read through the ONE table
below (``_PROBABILITY_KEYS`` and friends), pinned by recorded fixtures; an answer it
cannot read is returned as ``{"type": ..., "raw": ...}`` rather than failing the call.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any

#: The service's question types (Microsoft-Decision-1). ``noul`` is the official name.
QUESTION_TYPES: tuple[str, ...] = ("noul", "choice", "score")

#: Spellings a calling model is likely to write for ``noul`` (UDR-0201 D6, operator Q2).
TYPE_ALIASES: dict[str, str] = {"bool": "noul", "boolean": "noul", "yes_no": "noul"}

QUESTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

#: Fixed, not tunable: an instruction is one question, never a document.
MAX_INSTRUCTIONS_CHARS = 2_000
#: A choice label is an identifier the agent reads back; a description is a sentence.
MAX_LABEL_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000
MIN_CHOICE_OPTIONS = 2
MIN_SCORE_LEVELS = 2

_QUESTION_KEYS = frozenset({"type", "instructions", "criteria"})


@dataclass(frozen=True)
class DecisionLimits:
    """The App Settings bounds (group ``decision``, operator Q1 defaults)."""

    max_questions: int = 20
    max_choice_options: int = 50
    max_score_levels: int = 11
    max_state_chars: int = 100_000

    @classmethod
    def from_settings(cls) -> DecisionLimits:
        from app.core.config import settings

        return cls(
            max_questions=int(settings.decision_max_questions),
            max_choice_options=int(settings.decision_max_choice_options),
            max_score_levels=int(settings.decision_max_score_levels),
            max_state_chars=int(settings.decision_max_state_chars),
        )


def state_length(state: Any) -> int:
    """Characters of ``state`` as sent: the text itself, or the JSON serialization."""
    if isinstance(state, str):
        return len(state)
    return len(json.dumps(state, ensure_ascii=False, separators=(",", ":")))


def normalize_type(value: Any) -> str | None:
    """The canonical question type, or None when ``value`` is not one."""
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    key = TYPE_ALIASES.get(key, key)
    return key if key in QUESTION_TYPES else None


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_state(state: Any, limits: DecisionLimits, problems: list[str]) -> None:
    if isinstance(state, str):
        if not state.strip():
            problems.append("state: must not be empty")
            return
    elif isinstance(state, dict):
        if not state:
            problems.append("state: must not be an empty object")
            return
    else:
        problems.append("state: must be text or a JSON object")
        return
    length = state_length(state)
    if length > limits.max_state_chars:
        problems.append(f"state: {length} characters is more than the limit of {limits.max_state_chars}")


def _validate_noul(name: str, criteria: Any, problems: list[str]) -> Any:
    if criteria is None:
        return None
    if not isinstance(criteria, dict):
        problems.append(f'{name}: \'noul\' criteria must be {{"true": "...", "false": "..."}} or omitted')
        return None
    lowered = {str(k).strip().lower(): v for k, v in criteria.items()}
    if set(lowered) != {"true", "false"} or not all(_nonempty_str(v) for v in lowered.values()):
        problems.append(f'{name}: \'noul\' criteria must have exactly the keys "true" and "false" with descriptions')
        return None
    return {"true": lowered["true"].strip(), "false": lowered["false"].strip()}


def _validate_choice(name: str, criteria: Any, limits: DecisionLimits, problems: list[str]) -> Any:
    # A list of labels is accepted and read as labels that describe themselves: the
    # calling model often writes the options that way, and nothing is lost.
    if isinstance(criteria, list) and all(_nonempty_str(c) for c in criteria):
        criteria = {c.strip(): c.strip() for c in criteria}
    if not isinstance(criteria, dict):
        problems.append(f"{name}: 'choice' needs criteria as an object {{label: description}}")
        return None
    count = len(criteria)
    if count < MIN_CHOICE_OPTIONS:
        problems.append(f"{name}: 'choice' needs at least {MIN_CHOICE_OPTIONS} options in criteria")
        return None
    if count > limits.max_choice_options:
        problems.append(f"{name}: {count} options is more than the limit of {limits.max_choice_options}")
        return None
    out: dict[str, str] = {}
    for label, description in criteria.items():
        if not _nonempty_str(label) or len(label) > MAX_LABEL_CHARS:
            problems.append(f"{name}: option label {label!r} must be non-empty text of at most {MAX_LABEL_CHARS}")
            continue
        if not _nonempty_str(description) or len(description) > MAX_DESCRIPTION_CHARS:
            problems.append(
                f"{name}: option {label!r} needs a description of at most {MAX_DESCRIPTION_CHARS} characters"
            )
            continue
        out[label.strip()] = description.strip()
    if len(out) != count:
        return None
    return out


def _validate_score(name: str, criteria: Any, limits: DecisionLimits, problems: list[str]) -> Any:
    if not isinstance(criteria, list):
        problems.append(f"{name}: 'score' needs criteria as a list of levels, lowest first")
        return None
    count = len(criteria)
    if count < MIN_SCORE_LEVELS:
        problems.append(f"{name}: 'score' needs at least {MIN_SCORE_LEVELS} levels in criteria")
        return None
    if count > limits.max_score_levels:
        problems.append(f"{name}: {count} levels is more than the limit of {limits.max_score_levels}")
        return None
    if not all(_nonempty_str(c) and len(c) <= MAX_DESCRIPTION_CHARS for c in criteria):
        problems.append(f"{name}: every 'score' level must be non-empty text of at most {MAX_DESCRIPTION_CHARS}")
        return None
    return [c.strip() for c in criteria]


def validate_request(
    state: Any, questions: Any, limits: DecisionLimits | None = None
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate ``state`` and ``questions``; return (normalized questions, problems).

    The normalized questions carry the canonical ``type`` (aliases resolved), trimmed
    text, and ``criteria`` only when present. A non-empty problem list means the call
    MUST NOT be sent (UDR-0201 D6).
    """
    limits = limits or DecisionLimits()
    problems: list[str] = []
    _validate_state(state, limits, problems)

    if not isinstance(questions, dict) or not questions:
        problems.append("questions: must be an object with at least one question")
        return {}, problems
    if len(questions) > limits.max_questions:
        problems.append(f"questions: {len(questions)} questions is more than the limit of {limits.max_questions}")
        return {}, problems

    normalized: dict[str, dict[str, Any]] = {}
    for name, question in questions.items():
        if not isinstance(name, str) or not QUESTION_NAME_RE.match(name):
            problems.append(f"{name}: invalid question name (letters, digits and _, starting with a letter or _)")
            continue
        if not isinstance(question, dict):
            problems.append(f"{name}: must be an object {{type, instructions, criteria}}")
            continue
        unknown = sorted(set(question) - _QUESTION_KEYS)
        if unknown:
            problems.append(f"{name}: unknown field(s) {', '.join(unknown)}")
        qtype = normalize_type(question.get("type"))
        if qtype is None:
            problems.append(f"{name}: type must be one of noul, choice, score (got {question.get('type')!r})")
            continue
        instructions = question.get("instructions")
        if not _nonempty_str(instructions):
            problems.append(f"{name}: instructions must be non-empty text")
            continue
        if len(instructions) > MAX_INSTRUCTIONS_CHARS:
            problems.append(f"{name}: instructions are longer than {MAX_INSTRUCTIONS_CHARS} characters")
            continue
        raw_criteria = question.get("criteria")
        if qtype == "noul":
            criteria = _validate_noul(name, raw_criteria, problems)
        elif qtype == "choice":
            criteria = _validate_choice(name, raw_criteria, limits, problems)
        else:
            criteria = _validate_score(name, raw_criteria, limits, problems)
        entry: dict[str, Any] = {"type": qtype, "instructions": instructions.strip()}
        if criteria is not None:
            entry["criteria"] = criteria
        normalized[name] = entry
    return normalized, problems


# ---- Answers: wire -> normalized (UDR-0201 D7) ---------------------------------

# The one mapping table for the wire fields the documentation does not name (R1).
_PROBABILITY_KEYS = ("probability", "p", "value", "true")
_DISTRIBUTION_KEYS = ("probabilities", "scores", "distribution", "options", "levels")
_SCORE_KEYS = ("score", "value", "expected")
_LABEL_KEYS = ("label", "option", "name", "choice", "key")
_PROB_ITEM_KEYS = ("probability", "p", "score", "value")


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _round(value: float) -> float:
    return round(value, 4)


def _distribution(answer: dict[str, Any]) -> Any:
    for key in _DISTRIBUTION_KEYS:
        if key in answer and isinstance(answer[key], (dict, list)):
            return answer[key]
    return None


def _label_probs(raw: Any) -> dict[str, float] | None:
    """``{label: p}`` from a mapping or a list of ``{label, probability}`` items."""
    out: dict[str, float] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            p = _num(v)
            if p is None and isinstance(v, dict):
                p = next((_num(v[k2]) for k2 in _PROB_ITEM_KEYS if _num(v.get(k2)) is not None), None)
            if p is None:
                return None
            out[str(k)] = p
        return out or None
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                return None
            label = next((item[k] for k in _LABEL_KEYS if isinstance(item.get(k), str)), None)
            p = next((_num(item[k]) for k in _PROB_ITEM_KEYS if _num(item.get(k)) is not None), None)
            if label is None or p is None:
                return None
            out[label] = p
        return out or None
    return None


def _level_probs(raw: Any, levels: int) -> list[float] | None:
    """Per-level probabilities as a list indexed by level, from a list or an index map."""
    if isinstance(raw, list):
        if all(_num(v) is not None for v in raw):
            values = [float(v) for v in raw]
        else:
            labelled = _label_probs(raw)
            if labelled is None:
                return None
            values = list(labelled.values())
        return values if len(values) == levels else None
    if isinstance(raw, dict):
        values = [0.0] * levels
        for k, v in raw.items():
            p = _num(v)
            try:
                index = int(k)
            except (TypeError, ValueError):
                return None
            if p is None or not 0 <= index < levels:
                return None
            values[index] = p
        return values
    return None


def _noul(answer: dict[str, Any]) -> dict[str, Any] | None:
    for key in _PROBABILITY_KEYS:
        p = _num(answer.get(key))
        if p is not None and 0.0 <= p <= 1.0:
            return {"type": "noul", "probability": _round(p)}
    dist = _label_probs(_distribution(answer))
    if dist and "true" in {k.lower() for k in dist}:
        p = next(v for k, v in dist.items() if k.lower() == "true")
        return {"type": "noul", "probability": _round(p)}
    return None


def _choice(answer: dict[str, Any], question: dict[str, Any]) -> dict[str, Any] | None:
    probs = _label_probs(_distribution(answer))
    choice = answer.get("choice") if isinstance(answer.get("choice"), str) else None
    if choice is None and probs:
        choice = max(probs, key=probs.__getitem__)
    if choice is None:
        return None
    options = question.get("criteria") or {}
    if options and choice not in options:
        return None
    out: dict[str, Any] = {"type": "choice", "choice": choice}
    if probs:
        ordered = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
        out["probabilities"] = {k: _round(v) for k, v in ordered}
    return out


def nearest_level(score: float, levels: int) -> int:
    """``score`` rounded half up, clamped to ``0..levels-1`` (UDR-0201 D7)."""
    return max(0, min(levels - 1, math.floor(score + 0.5)))


def _score(answer: dict[str, Any], question: dict[str, Any]) -> dict[str, Any] | None:
    labels: list[str] = list(question.get("criteria") or [])
    levels = len(labels)
    if levels < MIN_SCORE_LEVELS:
        return None
    probs = _level_probs(_distribution(answer), levels)
    score = next((_num(answer[k]) for k in _SCORE_KEYS if _num(answer.get(k)) is not None), None)
    if score is None and probs is not None and sum(probs) > 0:
        score = sum(i * p for i, p in enumerate(probs)) / sum(probs)
    if score is None:
        return None
    level = nearest_level(score, levels)
    out: dict[str, Any] = {
        "type": "score",
        "score": _round(score),
        "levels": levels,
        "nearest_level": level,
        "nearest_label": labels[level],
    }
    if probs is not None:
        out["probabilities"] = [_round(p) for p in probs]
    return out


def read_answer(answer: Any, question: dict[str, Any]) -> dict[str, Any]:
    """One normalized answer, or ``{"type", "raw"}`` when the wire shape is unreadable."""
    qtype = question.get("type")
    if isinstance(answer, dict):
        if qtype == "noul":
            out = _noul(answer)
        elif qtype == "choice":
            out = _choice(answer, question)
        else:
            out = _score(answer, question)
        if out is not None:
            return out
    return {"type": qtype, "raw": answer}


def read_answers(raw_answers: Any, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Normalize every asked question; a question the service did not answer is ``raw: null``."""
    source = raw_answers if isinstance(raw_answers, dict) else {}
    return {name: read_answer(source.get(name), question) for name, question in questions.items()}


def read_usage(raw: Any) -> dict[str, int | None]:
    """``{input_tokens, output_tokens}`` from the response's usage, tolerant of naming."""
    usage = raw if isinstance(raw, dict) else {}

    def pick(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    return {
        "input_tokens": pick("input_tokens", "prompt_tokens", "input_token_count"),
        "output_tokens": pick("output_tokens", "completion_tokens", "output_token_count"),
    }


__all__ = [
    "QUESTION_TYPES",
    "TYPE_ALIASES",
    "DecisionLimits",
    "nearest_level",
    "normalize_type",
    "read_answer",
    "read_answers",
    "read_usage",
    "state_length",
    "validate_request",
]
