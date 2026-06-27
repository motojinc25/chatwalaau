"""Declarative agent configuration (FEAT-0051, CTR-0142, PRP-0094, UDR-0072).

YAML is a SPECIFICATION; ChatWalaʻau OWNS construction. This package parses /
validates declarative agent YAML (the MAF ``AgentFactory`` front end when
available), maps the compatible subset onto the ChatWalaʻau build path
(CTR-0070 / CTR-0102 / Identity slot #1), rejects incompatible model options, and
holds the in-memory active-agent selection. The bundled CORE agent reproduces
current behavior and is active by default.
"""

from __future__ import annotations

from app.agent.declarative.loader import (
    CORE_AGENT_YAML,
    core_spec,
    load_inventory,
    resolve_spec,
)
from app.agent.declarative.spec import (
    CORE_AGENT_ID,
    DeclarativeAgentError,
    DeclarativeAgentSpec,
)
from app.agent.declarative.store import (
    active_spec,
    get_active_store,
    log_active_agent,
)

__all__ = [
    "CORE_AGENT_ID",
    "CORE_AGENT_YAML",
    "DeclarativeAgentError",
    "DeclarativeAgentSpec",
    "active_spec",
    "core_spec",
    "get_active_store",
    "load_inventory",
    "log_active_agent",
    "resolve_spec",
]
