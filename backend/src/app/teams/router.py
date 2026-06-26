"""Teams integration wiring (CTR-0138, PRP-0092, UDR-0070 D2/D3/D10).

``register_teams(app, agent_registry)`` is a NO-OP unless ``TEAMS_ENABLED`` (D10):
when disabled, nothing is mounted and the ``microsoft-teams-apps`` SDK is never
imported, so the runtime is byte-for-byte unchanged.

When enabled it builds the Teams ``App`` over the EXISTING ChatWalaʻau FastAPI app
(the SDK's ``FastAPIAdapter`` registers onto our app -- ChatWalaʻau owns the HTTP
lifecycle, D2). The actual route registration + Bot Framework JWT validation
happens in ``initialize_teams()``, which the FastAPI lifespan calls at startup
(``app.initialize()`` registers ``POST /api/teams/messages`` without running the
SDK's own server). The endpoint is JWT-authenticated and exempt from CTR-0083
(CAP-009, D3 / UDR-0055 D5).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI  # noqa: TC002  (runtime import; annotation only but kept importable)

from app.core.config import settings

logger = logging.getLogger(__name__)


def register_teams(app: FastAPI, *, agent_registry: Any) -> None:
    """Build the Teams adapter over ``app`` when TEAMS_ENABLED (UDR-0070 D10).

    No-op (and no SDK import) when disabled. The messaging route is registered
    later by ``initialize_teams()`` from the FastAPI lifespan.
    """
    if not settings.teams_enabled:
        return
    from app.teams.adapter import TeamsAdapter

    adapter = TeamsAdapter(agent_registry=agent_registry)
    adapter.build(app)
    logger.info("Microsoft Teams integration enabled (PRP-0092); route registered at startup")


async def initialize_teams() -> None:
    """Initialize the Teams App at FastAPI startup (registers the route + JWT).

    No-op unless TEAMS_ENABLED and an adapter was built by ``register_teams``.
    """
    if not settings.teams_enabled:
        return
    from app.teams.adapter import initialize_active_adapter

    await initialize_active_adapter()
