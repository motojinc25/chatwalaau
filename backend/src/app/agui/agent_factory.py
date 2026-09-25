"""Agent factory for Multi-Model Agent Registry (CTR-0026, CTR-0070, PRP-0035, PRP-0046).

Creates an AgentRegistry maintaining one Agent instance per configured
deployment name. All agents share the same Tools, Skills, MCP tools,
and context_providers. Only the underlying client differs.

Weather tools (CTR-0027, PRP-0017) are registered as AI functions.
Coding tools (CTR-0031, CTR-0032, PRP-0019) are conditionally registered.
Agent Skills (CTR-0043, PRP-0024) are conditionally loaded via SkillsProvider.
MCP tools (CTR-0060, PRP-0031) are dynamically loaded from config file.

PRP-0046 adds ``include_mcp`` / ``include_rag`` parameters so a caller can
construct an agent that does not share MCP tool async contexts or the
ChromaDB client. Since PRP-0183 (UDR-0165 D3) the consumer is the Declarative
Workflow prompt node (``app/workflow/handlers.py``, both flags ``False``); the
flags MUST NOT be removed as DevUI residue.
"""

import logging
from pathlib import Path
import platform
from typing import Any

from app import models_catalog
from app.agent.capability_guidance import ToolGuidance
from app.agent.compaction import resolve_compaction_strategy
from app.agui.agent_registry import AgentRegistry
from app.core.config import settings
from app.demo import is_demo_mode
from app.mcp.lifecycle import get_mcp_tools, get_server_tool_names
from app.mcp.overrides import get_override_store
from app.session.provider import FileHistoryProvider
from app.skills.provider import create_skills_provider
from app.weather.tools import weather_geocode_city, weather_get_current, weather_get_forecast

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

    base = (
        "You have access to coding tools for working with files in the workspace directory. "
        "Use file_glob to find files by pattern before reading them. "
        "Use file_grep to search for specific content across files. "
        "Use file_read to read file content. Use offset/limit for large files. "
        "Use file_write to create or modify files. "
        "Use bash_execute to run shell commands (build, test, git, etc.). "
        "All file paths are relative to the workspace directory. " + platform_note
    )
    return f"{base.rstrip()}\n\n{_workspace_file_reference_note()}"


def _workspace_file_reference_note() -> str:
    """How a delivered file is referenced (CTR-0207, PRP-0166, UDR-0150 D5/D6).

    Part of the `coding` tool-guide, so it is emitted only when the coding tools are
    registered. The absolute workspace path is disclosed because a skill script runs
    in its OWN directory (UDR-0145 D4) and needs an absolute output path to deliver
    into the workspace; the same model already runs bash_execute inside it.

    PRP-0177 / UDR-0159 D1 scopes the rule to what the agent PRODUCES with these tools.
    A generated image is NOT that: image_generate saves it under UPLOAD_DIR and returns
    /api/uploads/..., which the chat renders itself (CTR-0049 / CTR-0051), so this text
    used to make the model shell-copy an already delivered image into the workspace and
    hand back a workspace: link. Everything PRP-0166 promised is kept verbatim in
    effect: the form, the banned targets, and the skill-script absolute output path.
    """
    raw = (settings.coding_workspace_dir or "").strip()
    workspace = str(Path(raw).resolve()) if raw else "the workspace directory"
    return (
        "When you create a file for the user WITH THE CODING OR SKILL TOOLS, save it inside the workspace "
        "and reference it in your answer as a Markdown link whose target is workspace:<path relative to the "
        "workspace>, for example [hello_world.pdf](workspace:output/pdf/hello_world.pdf). The same applies to "
        "an image YOU produce that way, for example a chart your code writes: "
        "![chart](workspace:output/chart.png). It does NOT apply to an image returned by the "
        "image-generation tools: that image is already saved and shown to the user, so never copy it into "
        "the workspace and never link it. Never use sandbox:, file:, or an absolute path as a link "
        "target; the user cannot open those. run_skill_script runs a skill's script inside the skill's own "
        "directory, so a relative output path there does NOT land in the workspace. When a skill script "
        f"writes a file for the user, pass it an absolute output path under the workspace: {workspace}"
    )


def _validate_coding_config() -> None:
    """Validate coding configuration at startup (CTR-0032).

    The workspace directory is auto-created when missing (v0.90.1): operators no
    longer have to pre-create CODING_WORKSPACE_DIR. A creation failure (e.g. a
    read-only parent, a permission error, or a non-directory path component) is
    surfaced as a clear ValueError rather than letting later file operations fail.
    """
    workspace = settings.coding_workspace_dir
    if not workspace:
        msg = "CODING_WORKSPACE_DIR must be set when CODING_ENABLED=true"
        raise ValueError(msg)
    path = Path(workspace)
    if not path.is_absolute():
        msg = f"CODING_WORKSPACE_DIR must be an absolute path: {workspace}"
        raise ValueError(msg)
    if not path.is_dir():
        try:
            path.mkdir(parents=True, exist_ok=True)
            logger.info("Created CODING_WORKSPACE_DIR: %s", workspace)
        except OSError as exc:
            msg = f"CODING_WORKSPACE_DIR does not exist and could not be created: {workspace} ({exc})"
            raise ValueError(msg) from exc


def _build_tools_and_instructions(
    *,
    include_mcp: bool,
    include_rag: bool,
    spec: Any = None,
) -> tuple[list[Any], list[Any], list[ToolGuidance], list[Any]]:
    """Assemble (tools, context_providers, guidance, middleware) from current settings.

    PRP-0046 introduces the ``include_mcp`` / ``include_rag`` flags so a
    caller can build an agent without the loop-bound MCP tools and the
    ChromaDB-backed rag_search tool. The workflow prompt node is the consumer
    (UDR-0165 D3).

    PRP-0185 / UDR-0167 D7: the THIRD element is the slot-#3 guidance as a LIST of
    ``ToolGuidance`` blocks, not the rendered string it used to be. A capability
    withheld for one model must drop that tool AND its ``<tool-guide>`` block in the
    same breath (D6), and a rendered string cannot be subset without parsing it back.
    The consumer that knows the MODEL renders it -- ``AgentRegistry`` through
    ``subset_for_model()``, the workflow node through
    ``render_capability_guidance()``. This assembly stays model-agnostic and runs
    ONCE, because building it again would repeat the SkillsProvider side effect
    UDR-0130 D1 requires to happen once.

    The fourth return element ``middleware`` is the agent-level middleware list
    shared by every per-model Agent. It is EMPTY since PRP-0179 (UDR-0161 D1/D2):
    no tool is approval-gated, so no approval middleware exists. The seam stays so a
    future non-approval middleware has a place to go.
    """
    history_provider = FileHistoryProvider(
        sessions_dir=Path(settings.sessions_dir),
    )

    # Per-agent tool surface (PRP-0117, UDR-0100 D2). The active declarative agent's
    # tool_allowlist (None => inherit the full shared surface) SUBSETS the tools
    # assembled below. Resolved here so EVERY consumer of this chokepoint -- the
    # AgentRegistry rebuild AND the workflow node build -- honors it identically. Local
    # imports avoid the agent_factory <-> declarative import cycle (the router
    # precedent, declarative/router.py). ``_fn_ok`` gates a built-in function tool.
    from app.agent.declarative.store import active_spec as _active_spec
    from app.agent.declarative.tool_ids import parse_allowlist as _parse_allowlist

    # ``spec`` lets a caller pin a SPECIFIC declarative spec instead of the globally
    # active one -- used to build a workflow node agent from its referenced Prompt
    # agent (PRP-0118, CTR-0180). Defaulting to active_spec() keeps every existing
    # caller (AgentRegistry rebuild, workflow node build) byte-for-byte.
    _effective_spec = spec if spec is not None else _active_spec()
    _allow = _parse_allowlist(_effective_spec.tool_allowlist)

    def _fn_ok(name: str) -> bool:
        return _allow is None or _allow.allows_function(name)

    # Web search is provider-supplied and added per-model in the AgentRegistry
    # (PRP-0069, UDR-0045 D5), so it is NOT part of this shared
    # base tool list and the web search guidance lives in WEB_SEARCH_INSTRUCTION
    # (appended only for models whose provider supplies a web search tool).
    _weather_tools = [
        t for t in (weather_geocode_city, weather_get_current, weather_get_forecast) if _fn_ok(t.__name__)
    ]
    tools: list[Any] = list(_weather_tools)
    # Capability / tool guidance (Prompt Assembly slot #3). Each tool category
    # contributes a named ToolGuidance block; render_capability_guidance wraps each in
    # a <tool-guide name="..."> tag at the end (PRP-0120, CTR-0104 v4, UDR-0103 D1).
    # The Global Agent Identity (slot #1) is prepended by build_system_prompt() and the
    # Memory Blocks (slot #2) by the AgentRegistry -- unchanged. A category's guidance
    # is appended IFF that category contributes at least one tool, generalizing the
    # former weather-only "emit iff built" behavior.
    guidance: list[ToolGuidance] = []
    if _weather_tools:
        guidance.append(
            ToolGuidance(
                "weather",
                "You can look up weather information for any city worldwide. "
                "For weather queries: first use weather_geocode_city to get coordinates, "
                "then use weather_get_current or weather_get_forecast. "
                "After calling weather tools, provide a clear summary of the weather information.",
            )
        )

    # Conditionally register coding tools (CTR-0032, PRP-0019)
    if settings.coding_enabled:
        _validate_coding_config()
        from app.coding.tools import bash_execute, file_glob, file_grep, file_read, file_write

        coding_tools = [t for t in (file_read, file_write, bash_execute, file_glob, file_grep) if _fn_ok(t.__name__)]
        if coding_tools:
            tools.extend(coding_tools)
            guidance.append(ToolGuidance("coding", _build_coding_instructions()))
            logger.info(
                "Coding tools enabled (workspace=%s, max_turns=%d)",
                settings.coding_workspace_dir,
                settings.coding_max_turns,
            )

    # RAG Search tool (CTR-0077, PRP-0037) -- excluded when include_rag=False.
    # PRP-0114 / UDR-0095 D1/D2/D4: the query embedder is configured SOLELY by a
    # catalog `embeddings` offering (non-demo). rag_search is registered only when
    # CHROMA_DIR is set AND (an embeddings offering exists OR DEMO_MODE). CHROMA_DIR
    # set but no offering (non-demo) -> not registered (graceful); a startup advisory
    # (app.main) names the fix.
    if (
        include_rag
        and settings.chroma_dir
        and (models_catalog.embedding_config() is not None or is_demo_mode())
        and _fn_ok("rag_search")
    ):
        try:
            from app.rag.tools import init_rag_search, rag_search

            init_rag_search(
                chroma_dir=settings.chroma_dir,
                collection_name=settings.rag_collection_name,
                top_k=settings.rag_top_k,
            )
            tools.append(rag_search)
            guidance.append(
                ToolGuidance(
                    "rag",
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
                    "If rag_search returns no results, inform the user and optionally fall back to web search.",
                )
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
    # PRP-0114 / UDR-0095 D1/D2/D4: image generation is configured SOLELY by a
    # catalog `image` offering (non-demo). The tools are registered when such an
    # offering exists OR in DEMO_MODE (which routes to the demo image lane and needs
    # no offering). No offering (non-demo) -> not registered (graceful); a startup
    # advisory (app.main) surfaces a leftover IMAGE_DEPLOYMENT_NAME.
    _image_offering = models_catalog.image_config()
    if _image_offering is not None or is_demo_mode():
        from app.image_gen.tools import image_edit, image_generate

        image_tools = [t for t in (image_generate, image_edit) if _fn_ok(t.__name__)]
        if image_tools:
            tools.extend(image_tools)
            guidance.append(
                ToolGuidance(
                    "image",
                    "You can generate images from text descriptions using image_generate. "
                    "You can also edit existing images using image_edit: pass the filename of the "
                    "uploaded or previously generated image FIRST in image_filenames, then any "
                    "reference images. Write the edit prompt as two parts -- 'Change:' what must be "
                    "different, and 'Preserve:' what must stay exactly as it is (for example: "
                    "'Change: the background only. Preserve: the product shape, logo and colors.'). "
                    "Leave size, quality and background out unless the user asked for a specific "
                    "value; output is always PNG. "
                    "After generating or editing an image, describe what was created. "
                    # UDR-0159 D2: a tool whose result the chat renders says so itself,
                    # or another tool-guide's file rule takes over (PRP-0177).
                    "The image is already saved and shown to the user in the chat: do not save it again, "
                    "do not copy it into the workspace, do not offer a download link, and do not run shell "
                    "or file commands for it.",
                )
            )
            logger.info(
                "Image generation tools enabled (deployment=%s, demo=%s)",
                (_image_offering.deployment if _image_offering is not None else "<demo>"),
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
                # Per-agent allow-list (PRP-0117, UDR-0100 D2): drop a server the
                # active declarative agent did not select at all. A whole-server
                # selection (mcp:<server>) passes through to the override logic below;
                # a per-tool selection (mcp:<server>/<tool>) further narrows it.
                if _allow is not None and not _allow.mcp_server_selected(server):
                    tool.allowed_tools = None
                    continue
                full_names = get_server_tool_names(server)
                disabled = store.disabled_tools_for(server)
                if full_names:
                    # Enabled subset after the MCP override store...
                    override_enabled = [n for n in full_names if n not in disabled]
                    # ...then intersected with the per-agent allow-list when active
                    # (whole-server returns override_enabled verbatim).
                    if _allow is not None:
                        final_allowed = _allow.mcp_allowed_tools(server, override_enabled) or []
                    else:
                        final_allowed = override_enabled
                    if not final_allowed:
                        # Nothing of this server survives -> drop it.
                        tool.allowed_tools = None
                        continue
                    # None when nothing is filtered, so the unmodified case stays
                    # byte-for-byte identical to pre-PRP-0086 / no allow-list.
                    tool.allowed_tools = final_allowed if len(final_allowed) != len(full_names) else None
                else:
                    # Server tools not known yet (not connected) -> expose all.
                    tool.allowed_tools = None
                enabled_mcp_tools.append(tool)
                enabled_servers.append(server)
            if enabled_mcp_tools:
                tools.extend(enabled_mcp_tools)
                servers_list = ", ".join(enabled_servers)
                guidance.append(
                    ToolGuidance(
                        "mcp",
                        f"You have MCP (Model Context Protocol) tools available from the following "
                        f"connected servers: {servers_list}. "
                        "When the user's request can be fulfilled by an MCP tool, ALWAYS prefer "
                        "using the MCP tool over web search or other built-in tools. "
                        "MCP tools provide direct, structured access to external services and "
                        "are more reliable than general web search for their specific domains. "
                        "After using an MCP tool, summarize the result clearly for the user.",
                    )
                )
                logger.info(
                    "MCP tools added to agent: %d active server(s): %s",
                    len(enabled_mcp_tools),
                    servers_list,
                )

    # User Preference Memory tool (PRP-0075, CTR-0105, UDR-0051 D5/D10).
    # Registered on the shared agent at this single chokepoint when enabled, so
    # it is available to every consumer. The Memory Block itself
    # (slot #2) is a per-session frozen snapshot injected per run by the AG-UI
    # endpoint, not baked here.
    if settings.user_profile_enabled and _fn_ok("manage_user_memory"):
        from app.agent.user_memory import USER_MEMORY_INSTRUCTION, manage_user_memory

        tools.append(manage_user_memory)
        guidance.append(ToolGuidance("user-memory", USER_MEMORY_INSTRUCTION))

    # Agent Curated Memory tool (PRP-0100, CTR-0162, UDR-0079 D7). Registered on
    # the shared agent at this single chokepoint when AGENT_MEMORY_ENABLED, so the
    # LLM can save durable environment/project facts mid-conversation. The
    # <agent-memory> Block itself (slot
    # #2b) is a per-session frozen snapshot injected per run by the AG-UI endpoint,
    # not baked here.
    if settings.agent_memory_enabled and _fn_ok("manage_memory"):
        from app.agent.agent_memory import AGENT_MEMORY_INSTRUCTION, manage_memory

        tools.append(manage_memory)
        guidance.append(ToolGuidance("agent-memory", AGENT_MEMORY_INSTRUCTION))

    # Cron management tool (PRP-0089, CTR-0134, UDR-0067 D7). Registered on the
    # shared agent only when CRON_ENABLED so the LLM can schedule script jobs. The
    # workspace jail + CODING_ENABLED gate are enforced at run time by the executor
    # (CTR-0132).
    if settings.cron_enabled and _fn_ok("manage_cron"):
        from app.cron.tool import CRON_TOOL_INSTRUCTION, manage_cron

        tools.append(manage_cron)
        guidance.append(ToolGuidance("cron", CRON_TOOL_INSTRUCTION))

    # Pipeline management tool (PRP-0096, CTR-0147, UDR-0074 D9). Registered on the
    # shared agent only when PIPELINE_ENABLED so the LLM can submit data-processing jobs
    # (rag-ingest). Replaces the former batch MCP tools; writes through the same engine +
    # store as the REST API (CTR-0146). Curated job types, no shell.
    # NOT registered under DEMO_MODE (PRP-0138 / UDR-0122): the tool is the SECOND write
    # path into the pipeline, and closing only the REST endpoint would leave "ingest this
    # PDF for me" working in chat. A capability is closed at the API AND at tool
    # registration, never at the UI alone (the same rule UDR-0161 D4 makes general).
    if settings.pipeline_enabled and not is_demo_mode() and _fn_ok("manage_pipeline"):
        from app.pipeline.tool import PIPELINE_TOOL_INSTRUCTION, manage_pipeline

        tools.append(manage_pipeline)
        guidance.append(ToolGuidance("pipeline", PIPELINE_TOOL_INSTRUCTION))

    # Webhook management tool (PRP-0097, CTR-0155, UDR-0075). Registered on the shared
    # agent only when WEBHOOK_ENABLED so the LLM can manage Graph subscriptions and run
    # the Teams meeting pipeline on demand. Writes through the same store + Graph client +
    # pipeline engine as the REST API (CTR-0154).
    if settings.webhook_enabled and _fn_ok("manage_webhook"):
        from app.webhook.tool import WEBHOOK_TOOL_INSTRUCTION, manage_webhook

        tools.append(manage_webhook)
        guidance.append(ToolGuidance("webhook", WEBHOOK_TOOL_INSTRUCTION))

    # Ontology query tool (PRP-0105, CTR-0172, UDR-0084 D9). Registered on the
    # shared agent only when ONTOLOGY_ENABLED so the LLM can answer questions from
    # the operator's RDF concept models (catalog + CONSTRUCT-only NL query answering
    # fenced Turtle). Read-only by construction.
    if settings.ontology_enabled and _fn_ok("query_ontology"):
        from app.ontology.tool import ONTOLOGY_TOOL_INSTRUCTION, query_ontology

        tools.append(query_ontology)
        guidance.append(ToolGuidance("ontology", ONTOLOGY_TOOL_INSTRUCTION))

    # The collected slot-#3 blocks are returned UNRENDERED (PRP-0185, UDR-0167 D7).
    # Rendering happens at the consumer, which knows the model and can therefore drop
    # a withheld capability's block alongside its tools (D6). The renderer and its
    # <tool-guide name="..."> format are unchanged (PRP-0120, CTR-0104 v4,
    # UDR-0103 D1): an empty list still renders "" so the no-tool / headless path is
    # byte-for-byte "no capability guidance".

    # No approval wrapping (PRP-0179, UDR-0161 D1): every tool is registered as the
    # plain callable, which MAF builds as never_require. Whether an agent HAS a
    # destructive tool is decided above -- CODING_ENABLED, the per-agent allow-list
    # (UDR-0161 D4) -- not by a runtime prompt.

    # Context providers (CTR-0043, PRP-0024)
    context_providers: list[Any] = [history_provider]
    middleware: list[Any] = []
    # Per-agent Skills subset (PRP-0117, UDR-0100 D2/D3): when the active agent has a
    # tool_allowlist, only the selected skill names survive (an allow-list with no
    # skill entries yields the empty set -> no skills). None => inherit all.
    skills_provider = create_skills_provider(allowlist_names=(_allow.skills if _allow is not None else None))
    if skills_provider:
        # The provider's three tools are built approval-free at construction
        # (disable_*_approval, PRP-0179 / UDR-0161 D1), so no auto-approval
        # middleware is attached on any lane (D2).
        context_providers.append(skills_provider)

    # Return the slot-#3 guidance BLOCKS. The Identity (slot #1) and -- when enabled
    # -- the per-session Memory Block (slot #2) are assembled by the consumer:
    # AgentRegistry bakes Identity-only and supplies the capability/memory remainder
    # per run when USER_PROFILE_ENABLED, otherwise it bakes the full
    # Identity+capability prompt (CTR-0104 v2, CTR-0105, UDR-0051 D4).
    return tools, context_providers, guidance, middleware


def create_agent_registry() -> AgentRegistry:
    """Create the AgentRegistry with one Agent per configured model (CTR-0070)."""
    tools, context_providers, guidance, middleware = _build_tools_and_instructions(
        include_mcp=True,
        include_rag=True,
    )
    compaction_strategy = resolve_compaction_strategy()
    return AgentRegistry(
        tools=tools,
        context_providers=context_providers,
        guidance=guidance,
        compaction_strategy=compaction_strategy,
        middleware=middleware,
    )


async def rebuild_agent_registry(registry: AgentRegistry) -> None:
    """Re-assemble the shared tool set (override-aware) and rebuild all agents.

    PRP-0086 / UDR-0064: called by the MCP management API (CTR-0121) after the
    in-memory MCP override store changes. Reuses the SAME assembly as
    ``create_agent_registry()`` so the rebuilt agents differ only by the gated MCP
    tool set; ``AgentRegistry.rebuild()`` swaps the per-model map atomically. Safe
    to call repeatedly -- ``_build_tools_and_instructions`` re-initialises only
    cheap, idempotent pieces (RAG init is guarded, CTR-0077).

    PRP-0162 / UDR-0140 D1: the compaction strategy is re-resolved here, exactly as
    ``create_agent_registry()`` does. It used to be carried over from process
    start, so the three ``rebuild``-scope compaction settings never reached the
    rebuilt agents. The resolver's own INFO line doubles as the operator's record
    of which window the next turn will use.
    """
    tools, context_providers, guidance, middleware = _build_tools_and_instructions(
        include_mcp=True,
        include_rag=True,
    )
    await registry.rebuild(
        tools=tools,
        context_providers=context_providers,
        guidance=guidance,
        compaction_strategy=resolve_compaction_strategy(),
        middleware=middleware,
    )
