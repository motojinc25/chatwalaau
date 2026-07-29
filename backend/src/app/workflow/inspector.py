"""Workflow run state inspector -- the guarded read of MAF declarative variables (CTR-0189).

PRP-0123 / UDR-0106 D7/D8. The `Local.` / `Workflow.Inputs.*` / `Workflow.Outputs.*` /
`System.*` / `Agent.*` / `Custom.*` namespaces a declarative workflow accumulates live in
the Microsoft Agent Framework shared ``State`` under the key
``_declarative_workflow_state`` (``agent_framework_declarative._workflows._declarative_base``).
That ``State`` is owned by the workflow's runner and reachable as ``Workflow._runner.state``
-- a PUBLIC ``state`` property on a PRIVATE ``_runner`` attribute.

Four rules govern this module, all normative (UDR-0106 D7):

1. OFF by default. Without ``WORKFLOW_STATE_INSPECTOR`` nothing is read, serialized, or
   returned.
2. SUPERSTEP granularity only. MAF commits pending state at the superstep boundary
   (``_runner.py``), so a reading taken mid-superstep is not guaranteed to be committed.
   The caller (CTR-0181) invokes this only on ``superstep_completed``.
3. Redacted and bounded before it leaves the process.
4. DIAGNOSTIC ONLY. Nothing in ChatWalaʻau may branch on the result, so turning the key
   off can never change what a workflow does.

UDR-0106 D8: this is the ONE function in CAP-002 permitted to traverse the private
framework path, it never raises, and an architecture invariant test pins the path against
the installed package so a framework upgrade fails CI instead of silently disabling the
inspector.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# The MAF shared-state key holding the declarative namespaces (verified against
# agent-framework-declarative 1.0.0rc2, _declarative_base.DECLARATIVE_STATE_KEY).
DECLARATIVE_STATE_KEY = "_declarative_workflow_state"

# The namespaces surfaced to the operator, in display order.
NAMESPACES = ("Inputs", "Outputs", "Local", "System", "Agent", "Conversation", "Custom")

# Key-name redaction (UDR-0106 D7.3). A heuristic by design; default-off is the real
# protection. Mirrors the vocabulary used elsewhere in the codebase for secret-like keys.
_SECRET_KEY_RE = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|apikey|credential|authorization|auth[_-]?header|"
    r"client[_-]?secret|private[_-]?key|access[_-]?key|connection[_-]?string|sas|bearer)",
    re.IGNORECASE,
)
REDACTED = "***"

# Size budgets. Fixed constants, not configuration: they protect the SSE transport.
MAX_VALUE_CHARS = 512
MAX_SNAPSHOT_CHARS = 16000

# The private-attribute traversal is logged at most once so a framework change is
# discoverable without flooding the log on every superstep of every run.
_warned = False


def _warn_once(message: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        logger.warning(message)


def declarative_state_of(workflow: Any) -> dict[str, Any] | None:
    """Return the raw declarative state dict from a compiled workflow, or ``None``.

    The ONE place in CAP-002 that traverses ``Workflow._runner.state`` (UDR-0106 D8).
    Returns ``None`` -- never raises -- when the runner, the ``state`` property, or the
    declarative key is absent, so an ``agent-framework`` upgrade degrades the inspector
    to silence instead of breaking runs.
    """
    runner = getattr(workflow, "_runner", None)
    if runner is None:
        _warn_once(
            "Workflow state inspector: Workflow._runner is unavailable in the installed "
            "agent-framework; the variable inspector is inactive (CTR-0189)."
        )
        return None
    state = getattr(runner, "state", None)
    exporter = getattr(state, "export_state", None)
    if not callable(exporter):
        _warn_once(
            "Workflow state inspector: Workflow._runner.state.export_state() is unavailable "
            "in the installed agent-framework; the variable inspector is inactive (CTR-0189)."
        )
        return None
    try:
        exported = exporter()
    except Exception:
        _warn_once("Workflow state inspector: export_state() raised; the inspector is inactive (CTR-0189).")
        return None
    if not isinstance(exported, dict):
        return None
    data = exported.get(DECLARATIVE_STATE_KEY)
    return data if isinstance(data, dict) else None


def _redact(key: str, value: Any, budget: list[int]) -> Any:
    """Redact secret-like keys and clip large values, recursively; never raises."""
    if _SECRET_KEY_RE.search(str(key)):
        return REDACTED
    return _clip(value, budget)


def _clip(value: Any, budget: list[int]) -> Any:
    if budget[0] <= 0:
        return "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        budget[0] -= len(value)
        return value if len(value) <= MAX_VALUE_CHARS else value[:MAX_VALUE_CHARS] + "...(truncated)"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if budget[0] <= 0:
                out["..."] = "(truncated)"
                break
            out[str(k)] = _redact(str(k), v, budget)
        return out
    if isinstance(value, (list, tuple, set)):
        out_list: list[Any] = []
        for item in value:
            if budget[0] <= 0:
                out_list.append("...(truncated)")
                break
            out_list.append(_clip(item, budget))
        return out_list
    text = f"<{type(value).__name__}>"
    budget[0] -= len(text)
    return text


def snapshot_workflow_state(workflow: Any, iteration: Any = None) -> dict[str, Any] | None:
    """Return a redacted, bounded namespace snapshot, or ``None`` when unavailable.

    ``None`` is returned when the inspector is disabled (the default), when the workflow
    state has not been initialized yet, or when the framework path no longer resolves.
    Never raises: a failure to inspect MUST NOT fail the run (CTR-0189).
    """
    try:
        from app.core.config import settings

        if not getattr(settings, "workflow_state_inspector", False):
            return None

        data = declarative_state_of(workflow)
        if not data:
            return None

        budget = [MAX_SNAPSHOT_CHARS]
        namespaces: dict[str, Any] = {}
        redacted_keys = 0
        for name in NAMESPACES:
            raw = data.get(name)
            if not isinstance(raw, dict) or not raw:
                continue
            rendered: dict[str, Any] = {}
            for key, value in raw.items():
                rendered[str(key)] = _redact(str(key), value, budget)
                if rendered[str(key)] == REDACTED:
                    redacted_keys += 1
            namespaces[name] = rendered

        return {
            "iteration": iteration,
            "namespaces": namespaces,
            "truncated": budget[0] <= 0,
            "redacted_keys": redacted_keys,
        }
    except Exception:
        _warn_once("Workflow state inspector: snapshot failed; the inspector is inactive (CTR-0189).")
        return None


__all__ = [
    "DECLARATIVE_STATE_KEY",
    "NAMESPACES",
    "REDACTED",
    "declarative_state_of",
    "snapshot_workflow_state",
]
