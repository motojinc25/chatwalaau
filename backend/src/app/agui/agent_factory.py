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
from agent_framework_openai import OpenAIChatClient
from azure.identity import AzureCliCredential

from app.agui.agent_registry import AgentRegistry
from app.core.config import settings
from app.mcp.lifecycle import get_mcp_server_names, get_mcp_tools
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
) -> tuple[list[Any], list[Any], str]:
    """Assemble (tools, context_providers, instructions) from current settings.

    PRP-0046 introduces the ``include_mcp`` / ``include_rag`` flags so
    DevUI can build an agent without the loop-bound MCP tools and
    ChromaDB-backed rag_search tool.
    """
    from agent_framework_openai import OpenAIChatClient as _OpenAIChatClient  # local alias for static method

    web_search_tool = _OpenAIChatClient.get_web_search_tool(
        user_location={"type": "approximate", "country": settings.web_search_country},
    )

    history_provider = FileHistoryProvider(
        sessions_dir=Path(settings.sessions_dir),
    )

    tools: list[Any] = [web_search_tool, get_coords_by_city, get_current_weather_by_coords, get_weather_next_week]
    instructions = (
        "You are a helpful AI assistant. "
        "You can search the web for up-to-date information. "
        "When you use web search results, include source links in your response "
        "as Markdown links: [source title](URL). "
        "Place citation links inline near the relevant text. "
        "You can also look up weather information for any city worldwide. "
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

    # Conditionally register image generation tools (CTR-0050, PRP-0027)
    if settings.image_deployment_name:
        from app.image_gen.tools import edit_image, generate_image

        tools.extend([generate_image, edit_image])
        instructions += (
            " You can generate images from text descriptions using generate_image. "
            "You can also edit existing images using edit_image by providing the filename "
            "of an uploaded or previously generated image. "
            "After generating or editing an image, describe what was created."
        )
        logger.info("Image generation tools enabled (deployment=%s)", settings.image_deployment_name)

    # MCP tools (CTR-0060, PRP-0031) -- excluded when include_mcp=False
    if include_mcp:
        mcp_tools = get_mcp_tools()
        if mcp_tools:
            tools.extend(mcp_tools)
            mcp_server_names = get_mcp_server_names()
            servers_list = ", ".join(mcp_server_names)
            instructions += (
                f" You have MCP (Model Context Protocol) tools available from the following "
                f"connected servers: {servers_list}. "
                "When the user's request can be fulfilled by an MCP tool, ALWAYS prefer "
                "using the MCP tool over web search or other built-in tools. "
                "MCP tools provide direct, structured access to external services and "
                "are more reliable than general web search for their specific domains. "
                "After using an MCP tool, summarize the result clearly for the user."
            )
            logger.info("MCP tools added to agent: %d tool(s) from servers: %s", len(mcp_tools), servers_list)

    # Context providers (CTR-0043, PRP-0024)
    context_providers: list[Any] = [history_provider]
    skills_provider = create_skills_provider()
    if skills_provider:
        context_providers.append(skills_provider)

    return tools, context_providers, instructions


def create_agent_registry() -> AgentRegistry:
    """Create the AgentRegistry with one Agent per configured model (CTR-0070)."""
    tools, context_providers, instructions = _build_tools_and_instructions(
        include_mcp=True,
        include_rag=True,
    )
    return AgentRegistry(
        tools=tools,
        context_providers=context_providers,
        instructions=instructions,
    )


def build_devui_agent() -> Agent | None:
    """Build a single Agent for DevUI (PRP-0046).

    DevUI runs in a daemon thread with its own asyncio event loop. To
    avoid cross-loop invocation of MCP tools (whose async context is
    entered by the main FastAPI lifespan) and the ChromaDB client
    (SQLite is thread-bound), this function constructs a fresh Agent
    that excludes MCP tools and ``rag_search`` when the respective
    ``DEVUI_DISABLE_*`` flags are set (default ``true``).

    Returns ``None`` when there are no configured models; the caller
    should fall back to the default-model registry agent in that case.
    """
    if not settings.model_list:
        return None

    include_mcp = not settings.devui_disable_mcp
    include_rag = not settings.devui_disable_rag

    tools, context_providers, instructions = _build_tools_and_instructions(
        include_mcp=include_mcp,
        include_rag=include_rag,
    )

    model = settings.default_model
    credential = AzureCliCredential()
    client = OpenAIChatClient(
        model=model,
        credential=credential,
        azure_endpoint=settings.azure_openai_endpoint or None,
    )

    model_options: dict[str, Any] = {}
    effort = settings.get_reasoning_effort(model)
    if effort:
        model_options["reasoning"] = {"effort": effort, "summary": "detailed"}

    agent = Agent(
        name=f"ChatWalaau-DevUI-{model}",
        instructions=instructions,
        client=client,
        tools=tools,
        context_providers=context_providers,
        default_options=model_options or None,
    )
    logger.info(
        "DevUI agent built (model=%s, include_mcp=%s, include_rag=%s)",
        model,
        include_mcp,
        include_rag,
    )
    return agent
