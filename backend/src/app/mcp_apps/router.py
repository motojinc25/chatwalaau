"""MCP Apps router (CTR-0067, PRP-0034).

Provides RPC proxy endpoint for View-to-Server communication
and HTML serving endpoint for stored UI Resource files.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.auth import verify_api_key
from app.mcp_apps.manager import read_app_html

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp-apps", tags=["MCP Apps"])


class RpcRequest(BaseModel):
    """JSON-RPC request from a View."""

    method: str
    params: dict[str, Any] = {}


@router.post("/{server_name}/rpc", dependencies=[Depends(verify_api_key)])
async def rpc_proxy(server_name: str, request: RpcRequest):
    """Proxy JSON-RPC requests from Views to MCP servers.

    Supports:
    - tools/call: Call an MCP server tool
    - resources/read: Read an MCP server resource
    """
    from app.mcp.lifecycle import _mcp_server_status, _mcp_tools

    # Find the MCP tool instance for this server
    tool = None
    for i, status in enumerate(_mcp_server_status):
        if status["name"] == server_name and status["status"] == "connected":
            if i < len(_mcp_tools):
                tool = _mcp_tools[i]
            break

    if tool is None:
        raise HTTPException(status_code=404, detail=f"MCP server not found: {server_name}")

    # Access the underlying MCP session for direct server communication.
    # MAF's call_tool() expects (tool_name, **kwargs) not dict, and has no
    # read_resource(). Using session directly is correct for RPC proxying.
    session = getattr(tool, "session", None)
    if session is None:
        raise HTTPException(status_code=502, detail=f"MCP server not connected: {server_name}")

    try:
        if request.method == "tools/call":
            name = request.params.get("name", "")
            arguments = request.params.get("arguments", {})
            result = await session.call_tool(name, arguments=arguments)
            # exclude_none removes null fields (e.g. annotations: None)
            # that the View SDK's strict Zod validation rejects
            result_data = result.model_dump(exclude_none=True) if hasattr(result, "model_dump") else result
            return {"result": result_data}

        elif request.method == "resources/read":
            uri = request.params.get("uri", "")
            from pydantic import AnyUrl

            result = await session.read_resource(AnyUrl(uri))
            result_data = result.model_dump(exclude_none=True) if hasattr(result, "model_dump") else result
            return {"result": result_data}

        else:
            raise HTTPException(status_code=400, detail=f"Unsupported RPC method: {request.method}")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("RPC proxy error for %s", server_name)
        raise HTTPException(status_code=502, detail=f"MCP server error: {e!s}") from e


@router.get("/html/{thread_id}/{filename}")
async def serve_app_html(thread_id: str, filename: str):
    """Serve stored MCP App HTML for iframe loading."""
    html = read_app_html(thread_id, filename)
    if html is None:
        raise HTTPException(status_code=404, detail="HTML file not found")
    return HTMLResponse(content=html)
