"""Cycle traces and the operator's formula (PRP-0189 Section 2.8, UDR-0171 D9).

One JSONL line per ``computer_*`` call, in ``COMPUTER_USE_DIR/trace-YYYY-MM.jsonl``
(default ``.computeruse``, relative to the backend working directory; no sub-folder). A line carries timings, counts and sizes ONLY:
no image, no typed text, no secret (D4, D8) -- ``TRACE_FIELDS`` is the closed set.

``summarize`` answers the operator's formula::

    Tcycle   = model + capture + encode + act + settle      (per perform_actions call)
    capacity = 60 / p95(Tcycle)                               (cycles per minute)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import threading
from typing import Any

logger = logging.getLogger(__name__)

TRACE_FIELDS = frozenset(
    {
        "ts",
        "thread",
        "run",
        "tool",
        "status",
        "model",
        "model_ms",
        "capture_ms",
        "encode_ms",
        "act_ms",
        "settle_ms",
        "steps",
        "image_sent",
        "image_bytes",
    }
)
PHASES = ("model_ms", "capture_ms", "encode_ms", "act_ms", "settle_ms")

_lock = threading.Lock()


def trace_dir() -> Path:
    """``COMPUTER_USE_DIR`` -- the traces sit directly in it (PRP-0189 amendment A2)."""
    from app.core.config import settings

    return Path(settings.computer_use_dir or ".computeruse")


def trace_path(month: str) -> Path:
    return trace_dir() / f"trace-{month}.jsonl"


def write(record: dict[str, Any]) -> None:
    """Append one trace line. Never raises: a trace must not fail a tool call."""
    line = {k: v for k, v in record.items() if k in TRACE_FIELDS}
    line.setdefault("ts", datetime.now(UTC).isoformat(timespec="milliseconds"))
    try:
        path = trace_path(f"{datetime.now(UTC):%Y-%m}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("Computer Use trace could not be written", exc_info=True)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


def read(days: int, *, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    months = {f"{(since + timedelta(days=d)):%Y-%m}" for d in range(days + 1)}
    out: list[dict[str, Any]] = []
    for month in sorted(months):
        path = trace_path(month)
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(raw)
                ts = datetime.fromisoformat(row["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            if ts >= since:
                out.append(row)
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-phase p50/p95, the p95 cycle and capacity, and actions per decision."""
    decisions = [r for r in rows if r.get("tool") == "computer_perform_actions"]
    phases: dict[str, Any] = {}
    for phase in PHASES:
        values = [float(r[phase]) for r in decisions if isinstance(r.get(phase), int | float)]
        phases[phase] = {"count": len(values), "p50": _percentile(values, 0.5), "p95": _percentile(values, 0.95)}
    cycles = [
        float(sum(r.get(p) or 0 for p in PHASES))
        for r in decisions
        if isinstance(r.get("model_ms"), int | float)  # the first call of a run has no model phase
    ]
    p95_cycle = _percentile(cycles, 0.95)
    steps = [int(r.get("steps") or 0) for r in decisions]
    return {
        "calls": len(rows),
        "decisions": len(decisions),
        "phases": phases,
        "p50_cycle_ms": _percentile(cycles, 0.5),
        "p95_cycle_ms": p95_cycle,
        "capacity_per_min": round(60_000 / p95_cycle, 1) if p95_cycle else None,
        "actions_per_decision": round(sum(steps) / len(steps), 2) if steps else None,
        "images_sent": sum(1 for r in rows if r.get("image_sent")),
        "statuses": _count(r.get("status", "") for r in rows),
    }


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


__all__ = ["PHASES", "TRACE_FIELDS", "read", "summarize", "trace_dir", "trace_path", "write"]
