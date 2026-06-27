"""In-memory active declarative-agent store (CTR-0142, PRP-0094, UDR-0072 D7).

Holds the single active agent id, process-global and NEVER persisted: on restart
the active agent re-initializes to CORE, so the default state reproduces
pre-PRP-0094 behavior byte-for-byte (UDR-0072 D7/D13). A small ``threading.Lock``
guards the id; the heavier apply path (agent rebuild) is serialized separately by
the AgentRegistry asyncio lock (CTR-0070).
"""

from __future__ import annotations

import logging
import threading

from app.agent.declarative.loader import core_spec, resolve_spec
from app.agent.declarative.spec import CORE_AGENT_ID, DeclarativeAgentSpec

logger = logging.getLogger(__name__)


class ActiveAgentStore:
    """Process-local record of the active declarative agent id (default CORE)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_id = CORE_AGENT_ID

    def active_id(self) -> str:
        with self._lock:
            return self._active_id

    def set_active(self, agent_id: str) -> None:
        """Set the active id. The caller validates the spec first (router)."""
        with self._lock:
            self._active_id = agent_id

    def reset(self) -> None:
        """Reset to CORE (used by tests and as the restart default)."""
        with self._lock:
            self._active_id = CORE_AGENT_ID

    def snapshot(self) -> str:
        with self._lock:
            return self._active_id


_store = ActiveAgentStore()


def get_active_store() -> ActiveAgentStore:
    """Return the process-wide active-agent store singleton."""
    return _store


def active_spec() -> DeclarativeAgentSpec:
    """Return the spec for the active agent, falling back to CORE on any failure.

    Called by the AgentRegistry build (CTR-0070), so it MUST NOT raise: if the
    active custom YAML went missing or stopped mapping, fall back to CORE and log a
    WARNING rather than break agent construction (UDR-0072 D7).
    """
    agent_id = _store.active_id()
    if agent_id == CORE_AGENT_ID:
        return core_spec()
    try:
        return resolve_spec(agent_id)
    except Exception:  # build path must never raise
        logger.warning(
            "Active declarative agent %r could not be resolved; falling back to CORE.",
            agent_id,
            exc_info=True,
        )
        _store.reset()
        return core_spec()


def log_active_agent() -> None:
    """Emit one INFO line naming the active declarative agent (UDR-0072 D12)."""
    spec = active_spec()
    logger.info(
        "Declarative agent active: id=%s name=%s source=%s",
        spec.id,
        spec.name,
        spec.source,
    )


__all__ = [
    "ActiveAgentStore",
    "active_spec",
    "get_active_store",
    "log_active_agent",
]
