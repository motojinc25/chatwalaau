"""Agent tool inventory: the selectable tool surface for authoring (CTR-0178).

Composes the tool surface a declarative agent may put in its ``tools:`` allow-list
into ONE read model for the authoring GUI (PRP-0117, FEAT-0061, UDR-0100 D3):

  * built-in FUNCTION tools -- enumerated from a static registry that mirrors the
    conditional blocks of ``app.agui.agent_factory._build_tools_and_instructions``,
    each with an ``available`` flag derived from the SAME settings / catalog gates
    (so the picker never advertises a function tool the runtime would not build);
  * MCP servers and tools -- reusing ``get_mcp_tool_inventory`` (CTR-0121);
  * Skills -- reusing ``get_skills_inventory`` (CTR-0123).

``known_tool_ids()`` is the synchronous set of RECOGNIZED identifiers (typo
detection) the loader validates a ``tool_allowlist`` against (UDR-0100 D3), mirroring
the model-id warning path. Web search is intentionally NOT listed: it is
provider-supplied per-model, not a selectable base tool (CTR-0178 boundary).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from app import models_catalog
from app.agent.declarative.tool_ids import (
    function_id,
    mcp_server_id,
    mcp_tool_id,
    skill_id,
)
from app.core.config import settings
from app.demo import is_demo_mode


@dataclass(frozen=True)
class BuiltinFunctionTool:
    """A built-in function tool advertised to the picker (CTR-0178)."""

    name: str
    category: str
    description: str
    available: Callable[[], bool]


def _rag_available() -> bool:
    return bool(settings.chroma_dir) and (models_catalog.embedding_config() is not None or is_demo_mode())


def _image_available() -> bool:
    return models_catalog.image_config() is not None or is_demo_mode()


# Static registry mirroring the conditional blocks of the assembly chokepoint. When
# a new built-in function tool is added to _build_tools_and_instructions, add it here
# too (the one maintenance point called out in UDR-0100 Consequences).
BUILTIN_FUNCTION_TOOLS: tuple[BuiltinFunctionTool, ...] = (
    BuiltinFunctionTool("get_coords_by_city", "weather", "Geocode a city name to coordinates.", lambda: True),
    BuiltinFunctionTool("get_current_weather_by_coords", "weather", "Current weather for coordinates.", lambda: True),
    BuiltinFunctionTool("get_weather_next_week", "weather", "7-day weather forecast for coordinates.", lambda: True),
    BuiltinFunctionTool("file_read", "coding", "Read a file in the workspace.", lambda: settings.coding_enabled),
    BuiltinFunctionTool("file_write", "coding", "Create or modify a workspace file.", lambda: settings.coding_enabled),
    BuiltinFunctionTool("bash_execute", "coding", "Run a shell command in the workspace.", lambda: settings.coding_enabled),
    BuiltinFunctionTool("file_glob", "coding", "Find workspace files by glob pattern.", lambda: settings.coding_enabled),
    BuiltinFunctionTool("file_grep", "coding", "Search workspace file contents.", lambda: settings.coding_enabled),
    BuiltinFunctionTool("rag_search", "rag", "Search the local document knowledge base.", _rag_available),
    BuiltinFunctionTool("generate_image", "image", "Generate an image from a text prompt.", _image_available),
    BuiltinFunctionTool("edit_image", "image", "Edit an uploaded or generated image.", _image_available),
    BuiltinFunctionTool("manage_user_memory", "memory", "Curate the durable user profile.", lambda: settings.user_profile_enabled),
    BuiltinFunctionTool("manage_memory", "memory", "Curate durable agent memory.", lambda: settings.agent_memory_enabled),
    BuiltinFunctionTool("manage_cron", "automation", "Schedule and manage cron jobs.", lambda: settings.cron_enabled),
    BuiltinFunctionTool("manage_pipeline", "automation", "Submit and manage pipeline jobs.", lambda: settings.pipeline_enabled),
    BuiltinFunctionTool("manage_webhook", "automation", "Manage webhook subscriptions and runs.", lambda: settings.webhook_enabled),
    BuiltinFunctionTool("query_ontology", "knowledge", "Query the RDF concept models.", lambda: settings.ontology_enabled),
)

_BUILTIN_BY_NAME = {t.name: t for t in BUILTIN_FUNCTION_TOOLS}


def _function_entries() -> list[dict[str, Any]]:
    return [
        {
            "identifier": function_id(t.name),
            "name": t.name,
            "kind": "function",
            "category": t.category,
            "description": t.description,
            "available": t.available(),
        }
        for t in BUILTIN_FUNCTION_TOOLS
    ]


def _mcp_entries() -> list[dict[str, Any]]:
    """MCP servers + tools with identifiers, reusing the CTR-0121 inventory shape."""
    from app.mcp.lifecycle import get_mcp_tool_inventory

    out: list[dict[str, Any]] = []
    for server in get_mcp_tool_inventory():
        name = server.get("name", "")
        server_available = bool(server.get("enabled")) and bool(server.get("loaded"))
        out.append(
            {
                "identifier": mcp_server_id(name),
                "name": name,
                "kind": "mcp",
                "transport": server.get("transport", ""),
                "status": server.get("status", ""),
                "loaded": server.get("loaded", False),
                "available": server_available,
                "tools": [
                    {
                        "identifier": mcp_tool_id(name, t.get("name", "")),
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "available": server_available and bool(t.get("enabled")),
                    }
                    for t in server.get("tools", [])
                ],
            }
        )
    return out


async def _skill_entries() -> list[dict[str, Any]]:
    """Skills (flat, with group) with identifiers, reusing the CTR-0123 inventory."""
    from app.skills.inventory import get_skills_inventory

    inv = await get_skills_inventory()
    out: list[dict[str, Any]] = []
    for group in inv.get("groups", []):
        gname = group.get("name", "")
        for skill in group.get("skills", []):
            sname = skill.get("name", "")
            out.append(
                {
                    "identifier": skill_id(sname),
                    "name": sname,
                    "kind": "skill",
                    "group": gname,
                    "description": skill.get("description", ""),
                    "available": bool(skill.get("enabled")) and bool(skill.get("loaded")),
                }
            )
    return out


async def get_tool_inventory() -> dict[str, Any]:
    """Return the unified selectable tool inventory for the authoring GUI (CTR-0178)."""
    return {
        "function_tools": _function_entries(),
        "mcp_servers": _mcp_entries(),
        "skills": await _skill_entries(),
    }


def known_tool_ids() -> set[str]:
    """Return every RECOGNIZED tool identifier (typo detection for the allow-list).

    Synchronous, so the loader can validate a ``tool_allowlist`` against live state
    the same way it validates ``model.id`` (UDR-0100 D3). "Recognized" means the
    tool EXISTS (a known built-in name / a connected-or-configured MCP server+tool /
    a discovered skill), independent of whether it is currently enabled -- an
    identifier that is recognized but gated off simply contributes no tool at build
    time, whereas an UNrecognized identifier is a typo and is warned.
    """
    ids: set[str] = {function_id(name) for name in _BUILTIN_BY_NAME}

    from app.mcp.lifecycle import get_mcp_tool_inventory

    for server in get_mcp_tool_inventory():
        name = server.get("name", "")
        if not name:
            continue
        ids.add(mcp_server_id(name))
        for t in server.get("tools", []):
            tname = t.get("name", "")
            if tname:
                ids.add(mcp_tool_id(name, tname))

    from app.skills.provider import discover_skill_names

    for sname in discover_skill_names():
        ids.add(skill_id(sname))

    return ids


__all__ = [
    "BUILTIN_FUNCTION_TOOLS",
    "get_tool_inventory",
    "known_tool_ids",
]
