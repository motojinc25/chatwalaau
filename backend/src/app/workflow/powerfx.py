"""Power Fx expression syntax validation for workflow authoring (v0.115.2, CTR-0180).

A declarative-workflow action value beginning with ``=`` is a Power Fx expression. The
MAF workflow compiler does NOT syntax-check these at build time, so a broken expression
(unbalanced parentheses, a dangling operator) compiles cleanly and only fails at run
time. This module parses each ``=`` expression with the bundled Microsoft Power Fx
engine and returns a warning per broken one.

Syntax-only: it uses ``Engine.Parse`` (a parse, not a bind), so a reference to an
as-yet-undefined workflow variable (``=Local.x``, ``=Workflow.Inputs.q``) is NOT a false
positive -- only genuine syntax errors are reported.

Power Fx runs on .NET (the ``powerfx`` package loads Microsoft.PowerFx via pythonnet).
The backend already loads .NET to COMPILE a workflow, so this adds no new runtime
requirement; but if the runtime is unavailable (e.g. a Linux image without .NET) this
degrades gracefully to a no-op rather than breaking authoring.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_engine: Any = None
_engine_unavailable = False


def _get_engine() -> Any:
    """Return a cached Power Fx ``Engine`` (loads .NET once), or None if unavailable."""
    global _engine, _engine_unavailable
    if _engine is not None or _engine_unavailable:
        return _engine
    try:
        import powerfx

        _engine = powerfx.Engine()
    except Exception:
        # No .NET runtime / powerfx not importable -> skip Power Fx validation entirely.
        logger.info("Power Fx engine unavailable; skipping expression validation", exc_info=True)
        _engine_unavailable = True
    return _engine


def _syntax_error(engine: Any, expr: str) -> str | None:
    """Return a syntax-error message for a Power Fx expression body, or None if valid."""
    try:
        parsed = engine._engine.Parse(expr)
        if parsed.IsSuccess:
            return None
        msgs = [str(err.Message) for err in parsed.Errors]
        return "; ".join(msgs[:2]) if msgs else "invalid expression"
    except Exception:
        # A crash in the .NET parse must never block authoring; treat as valid.
        logger.debug("Power Fx parse raised for %r", expr, exc_info=True)
        return None


def _iter_expressions(value: Any):
    """Yield every ``=``-prefixed Power Fx string reachable in an action value (recursive)."""
    if isinstance(value, str):
        if value.startswith("="):
            yield value
    elif isinstance(value, dict):
        for inner in value.values():
            yield from _iter_expressions(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _iter_expressions(inner)


def powerfx_warnings(actions: Any) -> list[str]:
    """Return a warning for each ``=`` expression in ``actions`` with a Power Fx syntax error.

    A no-op (empty list) when the Power Fx engine is unavailable.
    """
    engine = _get_engine()
    if engine is None:
        return []
    warnings: list[str] = []
    seen: set[str] = set()
    for expr in _iter_expressions(actions):
        if expr in seen:
            continue
        seen.add(expr)
        err = _syntax_error(engine, expr[1:])  # strip the leading '='
        if err:
            warnings.append(f"Power Fx expression {expr!r} has a syntax error: {err}")
    return warnings


__all__ = ["powerfx_warnings"]
