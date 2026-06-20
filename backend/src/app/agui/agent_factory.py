"""Agent factory for Multi-Model Agent Registry (CTR-0026, CTR-0070, PRP-0035, PRP-0046).

Creates an AgentRegistry maintaining one Agent instance per configured
deployment name. All agents share the same Tools, Skills, MCP tools,
and context_providers. Only the underlying client differs.

Weather tools (CTR-0027, PRP-0017) are registered as AI functions.
Coding tools (CTR-0031, CTR-0032, PRP-0019) are conditionally registered.
Agent Skills (CTR-0043, PRP-0024) are conditionally loaded via SkillsProvider.
MCP tools (CTR-0060, PRP-0031) are dynamically loaded from config file.

PRP-0046 adds ``include_mcp`` / ``include_rag`` parameters so that DevUI,
which runs in a separate asyncio event loop on a daemon thread, can
construct an agent that does not share MCP tool async contexts or the
ChromaDB client with the main FastAPI loop.
"""

import logging
from pathlib import Path
import platform
from typing import Any

from agent_framework import Agent

from app import providers
from app.agent.approval import resolve_require_set, wrap_with_approval
from app.agent.compaction import resolve_compaction_strategy
from app.agent.identity import build_system_prompt
from app.agui.agent_registry import WEB_SEARCH_INSTRUCTION, AgentRegistry, _build_chat_client
from app.core.config import settings
from app.demo import is_demo_mode, resolve_demo_models
from app.mcp.lifecycle import get_mcp_tools, get_server_tool_names
from app.mcp.overrides import get_override_store
from app.session.provider import FileHistoryProvider
from app.skills.provider import create_skills_provider
from app.weather.tools import get_coords_by_city, get_current_weather_by_coords, get_weather_next_week

logger = logging.getLogger(__name__)


def _build_coding_instructions() -> str:
    """Build platform-aware coding tool instructions."""
    os_name = platform.system()  # "Windows", "Darwin", "Linux"
    shell = "cmd.exe (Windows)" if os_name == "Windows" else "bash"
    os_label = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(os_name, os_name)

    platform_note = (
        f"The current platform is {os_label} with {shell}. Use platform-appropriate commands for bash_execute. "
    )
    if os_name == "Windows":
        platform_note += (
            "Use 'dir' instead of 'ls', 'type' instead of 'cat', "
            "'findstr' instead of 'grep', 'where' instead of 'which'. "
            "Use backslash for paths in commands or quote forward-slash paths. "
            "Prefer file_glob/file_grep tools over shell find/grep commands for cross-platform safety."
        )

    return (
        "You have access to coding tools for working with files in the workspace directory. "
        "Use file_glob to find files by pattern before reading them. "
        "Use file_grep to search for specific content across files. "
        "Use file_read to read file content. Use offset/limit for large files. "
        "Use file_write to create or modify files. "
        "Use bash_execute to run shell commands (build, test, git, etc.). "
        "All file paths are relative to the workspace directory. " + platform_note
    )


def _validate_coding_config() -> None:
    """Validate coding configuration at startup (CTR-0032)."""
    workspace = settings.coding_workspace_dir
    if not workspace:
        msg = "CODING_WORKSPACE_DIR must be set when CODING_ENABLED=true"
        raise ValueError(msg)
    if not Path(workspace).is_absolute():
        msg = f"CODING_WORKSPACE_DIR must be an absolute path: {workspace}"
        raise ValueError(msg)
    if not Path(workspace).is_dir():
        msg = f"CODING_WORKSPACE_DIR does not exist: {workspace}"
        raise ValueError(msg)


def _build_tools_and_instructions(
    *,
    include_mcp: bool,
    include_rag: bool,
    apply_approval: bool = True,
) -> tuple[list[Any], list[Any], str]:
    """Assemble (tools, context_providers, instructions) from current settings.

    PRP-0046 introduces the ``include_mcp`` / ``include_rag`` flags so
    DevUI can build an agent without the loop-bound MCP tools and
    ChromaDB-backed rag_search tool.

    PRP-0067 / UDR-0043 D1+D5 adds ``apply_approval``: when ``True``
    (default), tools whose ``__name__`` is in
    ``app.agent.approval.resolve_require_set()`` are wrapped with
    ``@tool(approval_mode="always_require")``. When ``False``, no tool
    is wrapped -- used by ``build_devui_agent()`` so the DevUI loop
    (which has no human-in-the-loop UI) keeps the pre-PRP-0067
    behaviour (UDR-0043 D5 DevUI clause).
    """
    history_provider = FileHistoryProvider(
        sessions_dir=Path(settings.sessions_dir),
    )

    # Web search is provider-supplied and added per-model in the AgentRegistry /
    # build_devui_agent (PRP-0069, UDR-0045 D5), so it is NOT part of this shared
    # base tool list and the web search guidance lives in WEB_SEARCH_INSTRUCTION
    # (appended only for models whose provider supplies a web search tool).
    tools: list[Any] = [get_coords_by_city, get_current_weather_by_coords, get_weather_next_week]
    # Capability / tool guidance only. The Global Agent Identity (Prompt Assembly
    # slot #1) is prepended by build_system_prompt() at the end of assembly,
    # replacing the former anonymous persona sentence (PRP-0073, CTR-0104,
    # UDR-0049 D4).
    instructions = (
        "You can look up weather information for any city worldwide. "
        "For weather queries: first use get_coords_by_city to get coordinates, "
        "then use get_current_weather_by_coords or get_weather_next_week. "
        "After calling weather tools, provide a clear summary of the weather information."
    )

    # Conditionally register coding tools (CTR-0032, PRP-0019)
    if settings.coding_enabled:
        _validate_coding_config()
        from app.coding.tools import bash_execute, file_glob, file_grep, file_read, file_write

        tools.extend([file_read, file_write, bash_execute, file_glob, file_grep])
        instructions += " " + _build_coding_instructions()
        logger.info(
            "Coding tools enabled (workspace=%s, max_turns=%d)",
            settings.coding_workspace_dir,
            settings.coding_max_turns,
        )

    # RAG Search tool (CTR-0077, PRP-0037) -- excluded when include_rag=False
    if include_rag and settings.chroma_dir:
        try:
            from app.rag.tools import init_rag_search, rag_search

            init_rag_search(
                chroma_dir=settings.chroma_dir,
                collection_name=settings.rag_collection_name,
                top_k=settings.rag_top_k,
            )
            tools.append(rag_search)
            instructions += (
                "\n\n## RAG (Retrieval-Augmented Generation) - IMPORTANT\n"
                "You have a local document knowledge base powered by rag_search. "
                "ALWAYS use rag_search FIRST (before web search) when:\n"
                "- The user asks about content from uploaded/ingested documents or PDFs\n"
                "- The user references a specific document, report, or file by name\n"
                "- The user says 'this document', 'the PDF', 'the report', 'the file'\n"
                "- The conversation previously involved PDF ingestion\n"
                "- The user asks to 'search documents', 'find in documents', or 'look up in the knowledge base'\n\n"
                "To ingest a PDF: use submit_job with job_type='rag-ingest' and "
                "params={'file_path': '<path from [Attached PDF: ...] reference>'}.\n"
                "To search documents: use rag_search with the user's question as the query.\n"
                "Include source citations (filename, page number) when presenting RAG results.\n"
                "If rag_search returns no results, inform the user and optionally fall back to web search."
            )
            logger.info(
                "RAG search tool enabled (chroma_dir=%s, collection=%s)",
                settings.chroma_dir,
                settings.rag_collection_name,
            )
        except ImportError:
            logger.info("chromadb not installed, RAG search tool skipped")
        except Exception:
            logger.exception("Failed to initialize RAG search tool")

    # Conditionally register image generation tools (CTR-0050, PRP-0027).
    # PRP-0066 / UDR-0041: also enabled in demo mode (no deployment name
    # is required because the tools route to DemoImageProvider).
    if settings.image_deployment_name or is_demo_mode():
        from app.image_gen.tools import edit_image, generate_image

        tools.extend([generate_image, edit_image])
        instructions += (
            " You can generate images from text descriptions using generate_image. "
            "You can also edit existing images using edit_image by providing the filename "
            "of an uploaded or previously generated image. "
            "After generating or editing an image, describe what was created."
        )
        logger.info(
            "Image generation tools enabled (deployment=%s, demo=%s)",
            settings.image_deployment_name or "<demo>",
            is_demo_mode(),
        )

    # MCP tools (CTR-0060, PRP-0031) -- excluded when include_mcp=False.
    # PRP-0086 / UDR-0064: the active MCP tool set is gated at runtime by the
    # in-memory override store. A fully-disabled server is OMITTED from this
    # agent's tool list; a partially-disabled server has ``allowed_tools`` set to
    # its enabled subset (the only MAF primitive that subsets one server's
    # functions). MCP connections are left untouched -- gating is at tool exposure,
    # not process lifecycle. With an empty override store this is byte-for-byte the
    # pre-PRP-0086 behaviour (every tool exposed).
    if include_mcp:
        mcp_tools = get_mcp_tools()
        if mcp_tools:
            store = get_override_store()
            enabled_mcp_tools: list[Any] = []
            enabled_servers: list[str] = []
            for tool in mcp_tools:
                server = getattr(tool, "name", "") or ""
                if store.server_disabled(server):
                    tool.allowed_tools = None  # reset so a later re-enable is clean
                    continue
                full_names = get_server_tool_names(server)
                disabled = store.disabled_tools_for(server)
                if full_names and disabled:
                    allowed = [n for n in full_names if n not in disabled]
                    if not allowed:
                        # Every tool of this server is disabled -> drop the server.
                        tool.allowed_tools = None
                        continue
                    # None when nothing is filtered, so the unmodified case stays
                    # byte-for-byte identical to pre-PRP-0086.
                    tool.allowed_tools = allowed if len(allowed) != len(full_names) else None
                else:
                    tool.allowed_tools = None
                enabled_mcp_tools.append(tool)
                enabled_servers.append(server)
            if enabled_mcp_tools:
                tools.extend(enabled_mcp_tools)
                servers_list = ", ".join(enabled_servers)
                instructions += (
                    f" You have MCP (Model Context Protocol) tools available from the following "
                    f"connected servers: {servers_list}. "
                    "When the user's request can be fulfilled by an MCP tool, ALWAYS prefer "
                    "using the MCP tool over web search or other built-in tools. "
                    "MCP tools provide direct, structured access to external services and "
                    "are more reliable than general web search for their specific domains. "
                    "After using an MCP tool, summarize the result clearly for the user."
                )
                logger.info(
                    "MCP tools added to agent: %d active server(s): %s",
                    len(enabled_mcp_tools),
                    servers_list,
                )

    # User Preference Memory tool (PRP-0075, CTR-0105, UDR-0051 D5/D10).
    # Registered on the shared agent at this single chokepoint when enabled, so
    # it is available to every consumer. It is NOT in the approval require-set
    # (UDR-0051 D9), so the wrap below is a no-op for it. The Memory Block itself
    # (slot #2) is a per-session frozen snapshot injected per run by the AG-UI
    # endpoint, not baked here.
    if settings.user_profile_enabled:
        from app.agent.user_memory import USER_MEMORY_INSTRUCTION, manage_user_memory

        tools.append(manage_user_memory)
        instructions += USER_MEMORY_INSTRUCTION

    # Tool approval gating (PRP-0067, CTR-0099, UDR-0043 D1).
    # The agent factory is the single chokepoint where bare Python
    # callables are turned into MAF tool surfaces; this is the right
    # place to decorate destructive tools with approval_mode. Skip mode
    # / DevUI bypass produce an empty require-set so wrap_with_approval
    # is a no-op for every entry.
    if apply_approval:
        require_set = resolve_require_set()
        tools = [wrap_with_approval(t, require_set) for t in tools]

    # Context providers (CTR-0043, PRP-0024)
    context_providers: list[Any] = [history_provider]
    skills_provider = create_skills_provider()
    if skills_provider:
        context_providers.append(skills_provider)

    # Return the RAW capability instructions (slot #3..). The Identity (slot #1)
    # and -- when enabled -- the per-session Memory Block (slot #2) are assembled
    # by the consumer: AgentRegistry bakes Identity-only and supplies the
    # capability/memory remainder per run when USER_PROFILE_ENABLED, otherwise it
    # bakes the full Identity+capability prompt (CTR-0104 v2, CTR-0105, UDR-0051
    # D4). build_devui_agent assembles the full prompt directly.
    return tools, context_providers, instructions


def create_agent_registry() -> AgentRegistry:
    """Create the AgentRegistry with one Agent per configured model (CTR-0070)."""
    tools, context_providers, instructions = _build_tools_and_instructions(
        include_mcp=True,
        include_rag=True,
        apply_approval=True,
    )
    compaction_strategy = resolve_compaction_strategy()
    return AgentRegistry(
        tools=tools,
        context_providers=context_providers,
        instructions=instructions,
        compaction_strategy=compaction_strategy,
    )


async def rebuild_agent_registry(registry: AgentRegistry) -> None:
    """Re-assemble the shared tool set (override-aware) and rebuild all agents.

    PRP-0086 / UDR-0064: called by the MCP management API (CTR-0121) after the
    in-memory MCP override store changes. Reuses the SAME assembly as
    ``create_agent_registry()`` so the rebuilt agents differ only by the gated MCP
    tool set; ``AgentRegistry.rebuild()`` swaps the per-model map atomically. Safe
    to call repeatedly -- ``_build_tools_and_instructions`` re-initialises only
    cheap, idempotent pieces (RAG init is guarded, CTR-0077).
    """
    tools, context_providers, instructions = _build_tools_and_instructions(
        include_mcp=True,
        include_rag=True,
        apply_approval=True,
    )
    await registry.rebuild(
        tools=tools,
        context_providers=context_providers,
        instructions=instructions,
    )


def build_devui_agent() -> Agent | None:
    """Build a single Agent for DevUI (PRP-0046, PRP-0066).

    DevUI runs in a daemon thread with its own asyncio event loop. To
    avoid cross-loop invocation of MCP tools (whose async context is
    entered by the main FastAPI lifespan) and the ChromaDB client
    (SQLite is thread-bound), this function constructs a fresh Agent
    that excludes MCP tools and ``rag_search`` when the respective
    ``DEVUI_DISABLE_*`` flags are set (default ``true``).

    Returns ``None`` when there are no configured models; the caller
    should fall back to the default-model registry agent in that case.

    DEMO_MODE (PRP-0066): DevUI is recommended-disabled on the demo
    deploy target (UDR-0041 D5), but the factory still works -- it
    builds against the demo model list and DemoChatClient.
    """
    if is_demo_mode():
        models = resolve_demo_models()
        model = models[0]
    else:
        if not settings.all_model_list:
            return None
        model = settings.default_model

    include_mcp = not settings.devui_disable_mcp
    include_rag = not settings.devui_disable_rag

    # UDR-0043 D5 (DevUI clause): DevUI runs in a daemon thread with no
    # human-in-the-loop approval UI, so we register the unwrapped tools
    # regardless of TOOL_APPROVAL_MODE. The factory still applies
    # compaction (UDR-0042 D1) -- compaction has no UI dependency.
    tools, context_providers, instructions = _build_tools_and_instructions(
        include_mcp=include_mcp,
        include_rag=include_rag,
        apply_approval=False,
    )

    # DEMO_MODE: DemoChatClient; LIVE: provider dispatch (CTR-0102).
    client = _build_chat_client(model)

    # Web search + per-model options are provider-supplied (PRP-0069, UDR-0045).
    # Demo preserves pre-PRP-0069 behavior: OpenAI web search tool + instruction.
    if is_demo_mode():
        web_search = providers.openai_web_search_tool()
        devui_tools = [web_search, *tools]
        # DevUI bakes the full Identity + capability prompt (no per-run Memory
        # Block; the User Profile snapshot is the AG-UI session path, UDR-0051 D10).
        devui_instructions = build_system_prompt(instructions + WEB_SEARCH_INSTRUCTION)
        model_options: dict[str, Any] = {}
    else:
        web_search = providers.web_search_tool(model)
        devui_tools = [web_search, *tools] if web_search is not None else list(tools)
        devui_instructions = build_system_prompt(
            instructions + (WEB_SEARCH_INSTRUCTION if web_search is not None else "")
        )
        model_options = providers.build_model_options(model)

    agent = Agent(
        name=f"ChatWalaau-DevUI-{model}",
        instructions=devui_instructions,
        client=client,
        tools=devui_tools,
        context_providers=context_providers,
        default_options=model_options or None,
        compaction_strategy=resolve_compaction_strategy(),
    )
    logger.info(
        "DevUI agent built (model=%s, include_mcp=%s, include_rag=%s, demo=%s)",
        model,
        include_mcp,
        include_rag,
        is_demo_mode(),
    )
    return agent
