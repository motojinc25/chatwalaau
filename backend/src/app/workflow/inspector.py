"""Workflow run state inspector -- the guarded read of MAF declarative variables (CTR-0189).

PRP-0123 / UDR-0106 D7/D8. The `Local.` / `Workflow.Inputs.*` / `Workflow.Outputs.*` /
`System.*` / `Agent.*` / `Custom.*` namespaces a declarative workflow accumulates live in
the Microsoft Agent Framework shared ``State`` under the key
``_declarative_workflow_state`` (``agent_framework_declarative._workflows._declarative_base``).
That ``State`` is owned by the workflow's runner and reachable as ``Workflow._runner.state``
-- a PUBLIC ``state`` property on a PRIVATE ``_runner`` attribute.

Three rules govern this module, all normative (UDR-0106 D7, amended by v0.117.1):

1. SUPERSTEP granularity only. MAF commits pending state at the superstep boundary
   (``_runner.py``), so a reading taken mid-superstep is not guaranteed to be committed.
   The caller (CTR-0181) invokes this only on ``superstep_completed``.
2. Redacted and bounded before it leaves the process. Since the inspector is now ALWAYS
   active these are the only protections left, so they are load-bearing rather than
   defence in depth: redaction runs at every nesting depth and both budgets are enforced.
3. DIAGNOSTIC ONLY. Nothing in ChatWalaʻau may branch on the result, so the snapshot can
   never change what a workflow does.

v0.117.1 removed the ``WORKFLOW_STATE_INSPECTOR`` gate: the variable pane is part of
watching a run, not an opt-in debug mode. Operators who must not surface workflow
variables at all should not expose the run canvas.

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

# The MAF shared-state key holding the declarative namespaces.
#
# IMPORTED, not duplicated (PRP-0127, UDR-0110 D3). A copied literal fails SILENTLY
# when upstream renames the key -- the inspector would simply stop finding state and
# every degradation test would keep passing -- whereas an import failure is loud and
# lands on the fallback below with a log line. There is no public equivalent at
# agent-framework-declarative 1.0.1, so this is an enumerated private reference.
_FALLBACK_DECLARATIVE_STATE_KEY = "_declarative_workflow_state"

try:
    from agent_framework_declarative._workflows._declarative_base import (
        DECLARATIVE_STATE_KEY,
    )
except (ImportError, AttributeError):  # pragma: no cover - upstream removal path
    DECLARATIVE_STATE_KEY = _FALLBACK_DECLARATIVE_STATE_KEY
    logging.getLogger(__name__).warning(
        "Workflow state inspector: agent-framework-declarative no longer exposes "
        "DECLARATIVE_STATE_KEY; falling back to the last known value %r. The variable "
        "inspector may report nothing (CTR-0189).",
        _FALLBACK_DECLARATIVE_STATE_KEY,
    )

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

    PRP-0127 O1, resolved by measurement: the declarative GA's public ``WorkflowState``
    CANNOT replace this traversal. They are different objects at different layers --
    ``WorkflowState`` is the declarative namespace container, while the state this
    reaches is the CORE workflow runner's, which is where the declarative container is
    stored under ``DECLARATIVE_STATE_KEY``. Only the key constant moved to an import;
    the traversal stays private and is an enumerated residue entry (UDR-0110 D2).

    Upstream now raises a DeprecationWarning on ``agent_framework._workflows._runner``
    ("intended for internal use only"), so this path is on notice: the degradation
    branch above is the plan for the release that removes it, and UDR-0110 D4 requires
    the next MAF bump to re-check for a public replacement.
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

    ``None`` is returned when the workflow state has not been initialized yet, or when the
    framework path no longer resolves. Never raises: a failure to inspect MUST NOT fail the
    run (CTR-0189).
    """
    try:
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
