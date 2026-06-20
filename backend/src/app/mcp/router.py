"""MCP Tool Management API (CTR-0121, PRP-0086, UDR-0064).

Two endpoints let an operator gate the active MCP tool set at runtime so a large
MCP surface does not waste tokens every turn:

    GET  /api/mcp/tools  -- inventory of configured MCP servers and their tools,
                            with each tool's / server's current enabled state.
    PUT  /api/mcp/tools  -- apply an enable/disable selection: update the in-memory
                            override store and REBUILD all per-model agents
                            (CTR-0070), responding only AFTER the rebuild completes
                            (this drives the SPA "rebuilding" indicator).

Both endpoints are gated by CTR-0083 (``verify_api_key``); loopback bypass keeps
localhost-first development zero-config (UDR-0064 D6). The override store is
in-memory only -- a restart re-enables every tool (UDR-0064 D4).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.mcp.lifecycle import get_mcp_tool_inventory
from app.mcp.overrides import get_override_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["MCP"])


class ToolSelection(BaseModel):
    """Desired enabled state for a single MCP tool."""

    name: str = Field(..., min_length=1, max_length=256)
    enabled: bool = True


class ServerSelection(BaseModel):
    """Desired enabled state for a server and (optionally) its tools."""

    name: str = Field(..., min_length=1, max_length=256)
    enabled: bool = True
    tools: list[ToolSelection] = Field(default_factory=list)


class McpSelection(BaseModel):
    """The full desired selection submitted by the management UI."""

    servers: list[ServerSelection] = Field(default_factory=list)


def register_mcp_management(app: FastAPI, *, agent_registry) -> None:
    """Mount the MCP Tool Management endpoints, closing over the AgentRegistry.

    The router needs the live ``AgentRegistry`` (CTR-0070) so PUT can rebuild it;
    the registry is the module-level singleton created in ``app.main`` and shared
    by reference with the AG-UI / OpenAI-API endpoints (UDR-0064 D2).
    """

    @router.get("/tools", dependencies=[Depends(verify_api_key)])
    async def list_mcp_tools() -> dict:
        """Return the MCP tool inventory with current enabled/disabled state."""
        return {"servers": get_mcp_tool_inventory()}

    @router.put("/tools", dependencies=[Depends(verify_api_key)])
    async def apply_mcp_tools(body: McpSelection) -> dict:
        """Apply a selection, rebuild the agents, and return the refreshed inventory.

        On rebuild failure the override store is rolled back to its prior snapshot so
        it stays consistent with the still-installed prior agents (UDR-0064 D5).
        """
        disabled_servers = {s.name for s in body.servers if not s.enabled}
        # A fully-disabled server's per-tool detail is irrelevant; only collect the
        # disabled tools of the servers that remain enabled.
        disabled_tools = {s.name: {t.name for t in s.tools if not t.enabled} for s in body.servers if s.enabled}

        store = get_override_store()
        prior = store.snapshot()
        store.set_selection(disabled_servers=disabled_servers, disabled_tools=disabled_tools)

        try:
            # Local import avoids a module-level import cycle
            # (agent_factory -> app.mcp.* -> ... ).
            from app.agui.agent_factory import rebuild_agent_registry

            await rebuild_agent_registry(agent_registry)
        except Exception:
            # Roll back so the store matches the prior agents that are still serving.
            store.set_selection(
                disabled_servers=prior["disabled_servers"],  # type: ignore[arg-type]
                disabled_tools=prior["disabled_tools"],  # type: ignore[arg-type]
            )
            logger.exception("MCP tool selection apply failed during agent rebuild")
            raise HTTPException(
                status_code=500,
                detail={"error": "agent_rebuild_failed"},
            ) from None

        logger.info(
            "MCP tool selection applied: %d disabled server(s), %d server(s) with disabled tools",
            len(disabled_servers),
            sum(1 for t in disabled_tools.values() if t),
        )
        return {"servers": get_mcp_tool_inventory()}

    app.include_router(router)


__all__ = ["register_mcp_management", "router"]
