"""FastAPI application entry point.

- Mounts AG-UI endpoint (CTR-0009)
- Mounts OpenAI-compatible Responses API (CTR-0057, PRP-0030)
- Launches DevUI server (CTR-0025, PRP-0016)
- Mounts session management API (CTR-0015)
- Mounts image upload API (CTR-0022)
- Mounts speech-to-text API (CTR-0021)
- Mounts text-to-speech API (CTR-0039)
- Mounts prompt templates API (CTR-0047)
- Mounts Web SPA authentication API (CTR-0094, PRP-0057)
- Manages MCP server lifecycle (CTR-0061, PRP-0031)
- Serves frontend build artifacts (CTR-0005)
- Loads configuration (CTR-0006)
"""

from contextlib import asynccontextmanager
import mimetypes
from pathlib import Path
import sys
import warnings

# Ensure UTF-8 output on Windows to prevent garbled non-ASCII characters (e.g. °C) in logs
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# CTR-0005 v3: force-register frontend asset MIME types before Starlette
# StaticFiles / FileResponse calls mimetypes.guess_type(). On Windows the
# stdlib mimetypes module seeds itself from HKEY_CLASSES_ROOT, and operator
# machines with a corrupted ".js" registry entry (text/plain, text/jscript,
# application/x-javascript, ...) cause the browser to reject ES module
# scripts under strict MIME checking. The explicit add_type() calls below
# pin the correct values process-wide regardless of the host registry state.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/wasm", ".wasm")

from dotenv import load_dotenv

# Load .env early -- before the agent_framework-importing imports below -- so the
# import-time SHOW_EXPERIMENTAL_WARNINGS toggle honors a value set in .env.
load_dotenv()


def _suppress_experimental_warnings_unless_opted_in() -> None:
    """Hide MAF staged-feature (ExperimentalWarning) startup noise by default.

    Microsoft Agent Framework's @experimental decorator warns (once each, via
    warnings.warn with an ExperimentalWarning -> FutureWarning category) when
    experimental classes (MemoryStore, SkillResource) are instantiated /
    subclassed during the agent / skills setup triggered by the imports below.
    This filter MUST be installed before those imports (PRP-0065 / UDR-0040).
    It is narrow by design: it matches only the staged-feature message, never a
    blanket ignore, and never suppresses ChatWalaʻau's own warnings. Operators
    restore the warnings with SHOW_EXPERIMENTAL_WARNINGS=true.
    """
    import os

    if (os.environ.get("SHOW_EXPERIMENTAL_WARNINGS") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    warnings.filterwarnings(
        "ignore",
        message=r".*is experimental and may change or be removed.*",
        category=FutureWarning,
    )


_suppress_experimental_warnings_unless_opted_in()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import providers
from app.agent.router import router as tool_approval_router
from app.agui.agent_factory import build_devui_agent, create_agent_registry
from app.agui.endpoint import register_agui_endpoints
from app.auth.web_auth import router as web_auth_router
from app.core.config import settings
from app.core.version import get_app_version
from app.demo import is_demo_mode
from app.devui.launcher import launch_devui_if_enabled
from app.image_gen.router import router as image_edit_router
from app.mcp.lifecycle import activate_mcp, prepare_mcp, shutdown_mcp
from app.mcp_apps.router import router as mcp_apps_router
from app.openai_api.router import register_openai_api
from app.paint.router import router as paint_router
from app.prompt_templates.router import router as templates_router
from app.session.router import router as session_router
from app.stt.factory import create_stt_provider
from app.stt.router import router as stt_router
from app.stt.router import set_stt_provider
from app.tts.factory import create_tts_provider
from app.tts.router import router as tts_router
from app.tts.router import set_tts_provider
from app.upload.router import router as upload_router

# Suppress pydantic warnings from agent-framework-ag-ui's Field(validation_alias=...) usage
warnings.filterwarnings("ignore", category=UserWarning, module=r"pydantic\._internal\._generate_schema")

# (.env was already loaded near the top of the module, before the imports above.)

# Logging is configured via log_conf.yaml (passed to uvicorn --log-config)

# Running app version (CTR-0094 v5, UDR-0044 D2): single shared helper.
_app_version = get_app_version()


def _warn_unconfigured_model_offerings() -> None:
    """Startup advisory for image / embedding features left unconfigured (PRP-0114).

    UDR-0095 D2: image generation and RAG embeddings are now configured SOLELY by a
    catalog `image` / `embeddings` offering (non-demo). A missing offering degrades
    gracefully (the feature is simply off), but when a dependent feature is otherwise
    enabled -- CHROMA_DIR is set, or a now-ignored IMAGE_DEPLOYMENT_NAME /
    EMBEDDING_DEPLOYMENT_NAME is still in the environment -- we emit a WARNING so the
    unconfigured state is operator-visible with the fix. DEMO_MODE is orthogonal and
    never warned (UDR-0095 D4).
    """
    import logging as _logging
    import os as _os

    _logger = _logging.getLogger(__name__)

    if is_demo_mode():
        return
    from app import models_catalog

    if models_catalog.active_catalog() is None:
        return  # no catalog at all -> chat routing already warned (PRP-0113); stay quiet here
    if models_catalog.image_config() is None and (_os.environ.get("IMAGE_DEPLOYMENT_NAME") or "").strip():
        _logger.warning(
            "IMAGE_DEPLOYMENT_NAME is set but no longer read (PRP-0114). Image generation is "
            'disabled until you add an offering with operations: ["image"] to '
            "model_offerings.jsonc (chatwalaau models add / Model Settings)."
        )
    if models_catalog.embedding_config() is None:
        leftover_embed = (_os.environ.get("EMBEDDING_DEPLOYMENT_NAME") or "").strip()
        if settings.chroma_dir or leftover_embed:
            _logger.warning(
                "RAG is enabled (CHROMA_DIR set) but no embeddings offering is configured "
                "(PRP-0114). rag_search and rag-ingest are unavailable until you add an offering "
                'with operations: ["embeddings"] to model_offerings.jsonc '
                "(chatwalaau models add / Model Settings)."
                if settings.chroma_dir
                else "EMBEDDING_DEPLOYMENT_NAME is set but no longer read (PRP-0114). Add an "
                'offering with operations: ["embeddings"] to model_offerings.jsonc.'
            )


def _warn_removed_role_model_env() -> None:
    """Startup advisory for removed per-task model env vars (PRP-0115, UDR-0096 D1).

    The five ``*_MODEL`` selectors (SESSION_TITLE_MODEL, USER_MEMORY_EXTRACTION_MODEL,
    AGENT_MEMORY_CURATION_MODEL, TEAMS_MEETING_SUMMARY_MODEL, ONTOLOGY_NL_MODEL) are
    no longer read; the model is assigned in the catalog ``roles`` block. When one is
    still present in the environment, name it and point at the successor role so the
    operator migrates it. Non-failing (an advisory must never block startup); DEMO is
    unaffected (the catalog / roles are ignored under demo).
    """
    import logging as _logging
    import os as _os

    from app.models_catalog import LEGACY_ROLE_ENV_VARS

    _logger = _logging.getLogger(__name__)
    for env_name, role in LEGACY_ROLE_ENV_VARS.items():
        if (_os.environ.get(env_name) or "").strip():
            _logger.warning(
                "%s is set but no longer read (PRP-0115). Assign the model under 'Task model "
                "assignments' in Model Settings, with `chatwalaau models role set %s <offering-id>`, "
                "or as a `roles.%s` entry in model_offerings.jsonc.",
                env_name,
                role,
                role,
            )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application lifespan: startup and shutdown hooks."""
    # Model Offering Catalog advisory for image / embedding (PRP-0114, UDR-0095 D2).
    _warn_unconfigured_model_offerings()
    # Advisory for removed per-task model env vars (PRP-0115, UDR-0096 D1).
    _warn_removed_role_model_env()
    # Session token store rehydrate (PRP-0110, CTR-0095 v2, UDR-0089 D4). Building
    # the singleton loads the digest projection from disk when the web auth lane is
    # enabled and AUTH_SESSION_PERSIST is true, so a restart no longer signs users
    # out. No-op (and nothing read) otherwise.
    if settings.web_auth_enabled:
        from app.auth.session_store import get_session_store

        get_session_store()
    # Startup: activate MCP servers (CTR-0061, PRP-0031)
    # prepare_mcp() was already called at module level before create_agent()
    await activate_mcp()
    # Temporary Chat retention sweep (PRP-0076, CTR-0106, UDR-0052 D4): delete
    # expired .temporary/ quarantine entries. Best-effort and non-failing; run
    # off the event loop so a large quarantine never delays startup.
    import asyncio as _asyncio

    from app.agent.temporary import sweep_temporary

    await _asyncio.to_thread(sweep_temporary)
    # Demo Mode bootstrap (PRP-0066, UDR-0041 D6): auto-seed the bundled
    # demo RAG corpus into ChromaDB when the collection is empty. The
    # helper is non-failing and idempotent.
    if is_demo_mode():
        from app.demo.bootstrap import seed_rag_corpus_if_needed

        await seed_rag_corpus_if_needed()
    # Cron Scheduler tick loop (PRP-0089, CTR-0130, UDR-0067). Started only when
    # CRON_ENABLED; the loop is otherwise never created (byte-for-byte unchanged).
    if settings.cron_enabled:
        from app.cron.engine import start_scheduler

        start_scheduler()
    # Pipeline Job engine (PRP-0096, CTR-0073, UDR-0074). In-process, ON by default
    # (PIPELINE_ENABLED). Ensures the jobs dir and logs the registered job types; the
    # queue runs jobs as asyncio tasks on submit, so there is no tick loop to start.
    if settings.pipeline_enabled:
        from app.pipeline.engine import start_pipeline

        start_pipeline()
    # Microsoft Teams integration (PRP-0092, CTR-0138, UDR-0070 D2). When
    # TEAMS_ENABLED, register the SDK messaging route onto this app + set up Bot
    # Framework JWT validation WITHOUT starting the SDK's own server (app.initialize,
    # not app.start). No-op when disabled.
    if settings.teams_enabled:
        from app.teams import initialize_teams

        await initialize_teams()
    # Inbound Webhook Gateway (PRP-0097, CTR-0152, UDR-0075 D8). Starts the internal
    # maintenance scheduler that auto-renews Graph subscriptions ONLY while CRON_ENABLED.
    # No-op unless WEBHOOK_ENABLED.
    if settings.webhook_enabled:
        from app.webhook import initialize_webhook

        await initialize_webhook()
    yield
    # Shutdown: flush pending session-token TTL slides (PRP-0110, CTR-0095 v2,
    # UDR-0089 D3). create/revoke already flushed synchronously; this persists the
    # slides accumulated by lookup() so a clean restart keeps the sliding expiry.
    if settings.web_auth_enabled:
        from app.auth.session_store import get_session_store

        await get_session_store().flush_if_dirty()
    # Shutdown: stop the cron scheduler (cancel loop + drain in-flight runs).
    if settings.cron_enabled:
        from app.cron.engine import stop_scheduler

        await stop_scheduler()
    # Shutdown: drain in-flight pipeline jobs (PRP-0096, CTR-0073).
    if settings.pipeline_enabled:
        from app.pipeline.engine import stop_pipeline

        await stop_pipeline()
    # Shutdown: stop the webhook maintenance scheduler (PRP-0097, CTR-0152).
    if settings.webhook_enabled:
        from app.webhook import shutdown_webhook

        await shutdown_webhook()
    # Shutdown: stop MCP servers
    await shutdown_mcp()
    # Drain in-flight background tasks (PRP-0077, CTR-0108). Best-effort: gives
    # running tasks a brief grace period, then cancels stragglers. A cancelled
    # title task simply leaves the truncation title.
    from app.background import shutdown as shutdown_background

    await shutdown_background()


app = FastAPI(
    title="ChatWalaʻau",
    version=_app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.app_debug else None,
    redoc_url="/redoc" if settings.app_debug else None,
    openapi_url="/openapi.json" if settings.app_debug else None,
)


# TLS startup sanity check (PRP-0058 UX-1, CTR-0054 v2).
# APP_SSL_CERTFILE / APP_SSL_KEYFILE are uvicorn-time arguments; if the operator
# launches uvicorn directly (e.g. `uv run uvicorn app.main:app --reload`) the
# kwargs are never injected and the server silently runs HTTP. Warn once at
# import so the misconfiguration surfaces in startup logs.
def _warn_if_tls_settings_unused() -> None:
    import logging as _logging
    import sys as _sys

    if not (settings.app_ssl_certfile and settings.app_ssl_keyfile):
        return
    argv = _sys.argv or []
    entry = argv[0].lower() if argv else ""
    # `chatwalaau` CLI passes ssl_certfile/ssl_keyfile to uvicorn.run() as
    # kwargs (no CLI flags appear in argv), so trust the entry-point name.
    if "chatwalaau" in entry:
        return
    # Direct `uvicorn` / `pnpm run dev:full` paths carry the flags in argv.
    if any("ssl-certfile" in arg for arg in argv):
        return
    _logging.getLogger(__name__).warning(
        "APP_SSL_CERTFILE / APP_SSL_KEYFILE are set but the running uvicorn process "
        "was not invoked with --ssl-certfile / --ssl-keyfile (argv=%r). "
        "HTTPS will NOT be active. Use the `chatwalaau` CLI or `pnpm run dev:full` "
        "(both auto-forward the SSL kwargs), or invoke uvicorn directly with "
        "`--ssl-certfile %s --ssl-keyfile %s`.",
        argv,
        settings.app_ssl_certfile,
        settings.app_ssl_keyfile,
    )


_warn_if_tls_settings_unused()


# .env drift advisory (PRP-0064, CTR-0097, UDR-0039 D8).
# Most releases add value through new opt-in env vars that default OFF, so the
# runtime keeps working after `pip install -U` without .env edits -- but the
# operator cannot discover the new knobs from their own .env. Emit ONE INFO
# line at startup when the bundled template declares keys the operator's .env
# is missing. Log-only and non-failing: an advisory must never block startup.
def _advise_env_drift() -> None:
    import logging as _logging

    try:
        from app.core.env_template import compute_drift, read_template_text

        env_file = Path(".env")
        if not env_file.exists():
            return
        drift = compute_drift(read_template_text(), env_file.read_text(encoding="utf-8"))
        if not drift.added:
            return
        preview = ", ".join(drift.added[:8]) + (", ..." if len(drift.added) > 8 else "")
        _logging.getLogger(__name__).info(
            "%d new setting(s) are available since your .env was generated (%s). "
            "Run `chatwalaau env diff` to review, then `chatwalaau env sync` to apply.",
            len(drift.added),
            preview,
        )
    except Exception:  # advisory must never block startup
        _logging.getLogger(__name__).debug("env drift advisory skipped", exc_info=True)


_advise_env_drift()

cors_origins = [origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Web SPA Authentication API (CTR-0094, PRP-0057) -- mounted before session
# routes so /api/auth/* takes priority over the catch-all SPA fallback.
app.include_router(web_auth_router)

# Tool Approval REST endpoint (CTR-0099, PRP-0067)
app.include_router(tool_approval_router)

# Memory Curation API (CTR-0164, PRP-0100) -- per-turn "like" trigger for the
# Agent Curated Memory (CTR-0162).
from app.memory.router import router as memory_router

app.include_router(memory_router)

# Memory Files Editing API (CTR-0166, PRP-0101) -- human-facing editor for the
# three built-in file memories (.agent/IDENTITY.md / USER.md / MEMORY.md).
from app.memory.editor import router as memory_editor_router

app.include_router(memory_editor_router)

# Session management API (CTR-0015)
app.include_router(session_router)

# Prompt Templates API (CTR-0047)
app.include_router(templates_router)

# Image upload API (CTR-0022)
app.include_router(upload_router)

# Paint scene persistence API (CTR-0161, PRP-0099)
app.include_router(paint_router)

# Mask-based image editing API (CTR-0053, PRP-0028)
app.include_router(image_edit_router)

# MCP Apps RPC bridge and HTML serving (CTR-0067, PRP-0034)
app.include_router(mcp_apps_router)

# Speech-to-Text API (CTR-0021)
# Provider selection delegated to app.stt.factory per UDR-0036
# (PRP-0061): REST audio.transcriptions for whisper-1 / gpt-4o-transcribe
# / gpt-4o-mini-transcribe, Realtime API WebSocket for
# gpt-realtime-whisper. Credential resolution still centralised in
# app.azure_credential (PRP-0058, UDR-0034).
_stt_provider = create_stt_provider(
    azure_openai_endpoint=settings.azure_openai_endpoint,
    deployment=settings.whisper_deployment_name,
    kind_override=settings.whisper_model_kind,
    realtime_connection_deployment=settings.whisper_realtime_connection_deployment,
    api_version_realtime=settings.azure_openai_realtime_api_version,
    realtime_audio_rate=settings.whisper_realtime_audio_rate,
)
if _stt_provider is not None:
    set_stt_provider(_stt_provider)
app.include_router(stt_router)

# Text-to-Speech API (CTR-0039)
# Provider selection delegated to app.tts.factory per UDR-0038
# (PRP-0063): ElevenLabs (default) or Azure OpenAI Realtime
# (gpt-realtime-2). The realtime lane reuses the centralised credential
# resolution in app.azure_credential (PRP-0058, UDR-0034) and the
# shared Realtime URL shape introduced for STT (PRP-0061).
_tts_provider = create_tts_provider(
    provider=settings.tts_provider,
    elevenlabs_api_key=settings.elevenlabs_api_key,
    tts_voice_id=settings.tts_voice_id,
    tts_model_id=settings.tts_model_id,
    azure_openai_endpoint=settings.azure_openai_endpoint,
    realtime_deployment=settings.tts_realtime_deployment,
    realtime_voice=settings.tts_realtime_voice,
    realtime_api_version=settings.azure_openai_realtime_api_version,
    realtime_audio_rate=settings.tts_realtime_audio_rate,
)
if _tts_provider is not None:
    set_tts_provider(_tts_provider)
app.include_router(tts_router)

# Prepare MCP tools synchronously before agent creation (CTR-0060, PRP-0031)
# activate_mcp() is called later in lifespan to start servers asynchronously
prepare_mcp()

# Multi-Model Agent Registry (CTR-0070, PRP-0035)
agent_registry = create_agent_registry()

# AG-UI endpoint (CTR-0009) -- receives registry for per-request model selection
register_agui_endpoints(app, agent_registry=agent_registry)

# MCP Tool Management API (CTR-0121, PRP-0086) -- runtime gating of the active MCP
# tool set. Receives the same registry so PUT can rebuild it atomically (CTR-0070).
from app.mcp.router import register_mcp_management

register_mcp_management(app, agent_registry=agent_registry)

# Skills Management API (CTR-0123, PRP-0087) -- runtime gating of the active Agent
# Skills set. Receives the same registry so PUT can rebuild it atomically (CTR-0070).
from app.skills.router import register_skills_management

register_skills_management(app, agent_registry=agent_registry)

# Declarative Agent Management API (CTR-0143, PRP-0094, UDR-0072) -- inventory +
# activate + reload of declarative agents. The YAML is a SPECIFICATION; ChatWalaʻau
# owns construction (the registry was already built from the active spec above, which
# defaults to the CORE agent = current behavior). Switching rebuilds the registry
# atomically (CTR-0070). Switching is SPA-only; API/Teams follow the active agent.
from app.agent.declarative import active_spec, log_active_agent
from app.agent.declarative.router import register_declarative_agents

register_declarative_agents(app, agent_registry=agent_registry)
log_active_agent()

# Declarative Workflows (CTR-0180..0185, PRP-0118, UDR-0101) -- a `kind: Workflow`
# declarative entity discovered from the SAME DECLARATIVE_AGENTS_DIR tree (dispatched
# by `kind`, UDR-0101 D2). Management + authoring API (CTR-0182/0183); the `workflow`
# Pipeline job type registers the asynchronous lane (CTR-0181 / UDR-0074 D7). A
# workflow is a per-conversation run-target (AG-UI state.workflow_id), not a persona,
# so no agent_registry rebuild is needed here (UDR-0101 D3).
from app.workflow.router import register_workflows
from app.workflow.runtime import register_workflow_job_type

register_workflows(app)
register_workflow_job_type()

# Model Offering Catalog Management API (CTR-0175, PRP-0111, UDR-0090) -- read/write
# the model_offerings.jsonc catalog (FEAT-0059 / CTR-0174) at runtime. PUT / reload
# rewrite the file (structured JSON, comments dropped) and rebuild the registry
# atomically (CTR-0070), so a change takes effect without a restart (UDR-0090 D2).
from app.model_offerings import register_model_offerings

register_model_offerings(app, agent_registry=agent_registry)

# Slash Commands API (CTR-0126, PRP-0088) -- read-only merged command inventory
# of built-ins plus prompt-template-derived and skill-derived commands. Dispatch
# is client-side per UDR-0066 D1, so no agent_registry is needed.
from app.commands.router import router as commands_router

app.include_router(commands_router)

# Workspace File Completion API (CTR-0127, PRP-0088) -- workspace-jailed file
# listing for @file completion; gated by CODING_ENABLED (UDR-0066 D6).
from app.workspace.router import router as workspace_router

app.include_router(workspace_router)

# File Explorer API (CTR-0136, PRP-0091) -- human-facing browse/edit over the coding
# workspace, sibling of CTR-0127. The surface returns 404 unless FILE_EXPLORER_ENABLED
# and CODING_ENABLED so the SPA can gate its launcher icon by probing it (UDR-0069 D3).
# Every path resolves through the CTR-0031 jail; mutating endpoints consume CTR-0083.
from app.workspace.explorer import router as file_explorer_router

app.include_router(file_explorer_router)

# Cron Management API (CTR-0133, PRP-0089) -- job CRUD + run history/detail. The
# surface returns 404 when CRON_ENABLED is false so the SPA can gate its launcher
# icon by probing it (UDR-0067 D10). Mutating endpoints consume CTR-0083.
from app.cron.router import router as cron_router

app.include_router(cron_router)

# Pipeline Management API (CTR-0146, PRP-0096) -- pipeline job CRUD + cancel + run
# history/detail + job-type schema. The surface returns 404 when PIPELINE_ENABLED is
# false so the SPA can gate its launcher icon by probing it (UDR-0074 D5). Mutating
# endpoints consume CTR-0083.
from app.pipeline.router import router as pipeline_router

app.include_router(pipeline_router)

# Ontology Management API (CTR-0171, PRP-0105, UDR-0084) -- catalog CRUD, JSON graph
# projection GET/PUT, RDF import/export, read-only SPARQL, and NL -> SPARQL. The
# surface returns 404 when ONTOLOGY_ENABLED is false so the SPA can gate its launcher
# icon by probing the catalog (UDR-0084 D12). Mutations + query POSTs consume CTR-0083.
from app.ontology.router import router as ontology_router

app.include_router(ontology_router)

# Server -> client notification WebSocket (CTR-0110, PRP-0077). Real-time push
# channel; first event type is session_title (CTR-0109).
from app.notifications import register_notifications_endpoint

register_notifications_endpoint(app)

# Microsoft Teams integration (CTR-0138, PRP-0092, UDR-0070). NO-OP unless
# TEAMS_ENABLED: when disabled the router is not mounted and the microsoft-teams-apps
# SDK is never imported/constructed -- byte-for-byte unchanged (UDR-0070 D10). When
# enabled, the inbound POST /api/teams/messages is mounted into THIS FastAPI app
# (ChatWalaʻau owns the HTTP lifecycle; the SDK owns Teams protocol handling -- D2).
# The endpoint is Bot Framework JWT-authenticated and exempt from CTR-0083 (CAP-009,
# D3). The Teams adapter reuses this app's agent registry unchanged.
from app.teams import register_teams

register_teams(app, agent_registry=agent_registry)

# Inbound Webhook Gateway (CTR-0149/0154, PRP-0097, UDR-0075). A separate
# external-boundary capability (CAP-010), mounted into THIS FastAPI app (ChatWalaʻau
# owns the single HTTP lifecycle, as it does for CAP-009 Teams). The public ingress
# (POST/GET /api/webhook/{source}) is EXEMPT from CTR-0083 and protected by the source's
# own validation (validation handshake + clientState HMAC); the management API
# (/api/webhooks/*) consumes CTR-0083. Both routers 404 unless WEBHOOK_ENABLED, and the
# msgraph source + teams-meeting job type are registered only when enabled (UDR-0075 D11).
from app.webhook import register_webhook

register_webhook(app)

# OpenAI-compatible Responses API (CTR-0057, PRP-0030)
register_openai_api(app, agent_registry=agent_registry)

# DevUI server (CTR-0025) -- PRP-0046: uses an isolated agent that
# excludes MCP tools and rag_search by default so the daemon-thread
# event loop does not share loop-bound handles with the main FastAPI
# loop. Falls back to the registry's default agent only when both
# DEVUI_DISABLE_MCP and DEVUI_DISABLE_RAG are set to false.
if settings.devui_enabled:
    if settings.devui_disable_mcp or settings.devui_disable_rag:
        _devui_agent = build_devui_agent() or agent_registry.get()
    else:
        _devui_agent = agent_registry.get()
    launch_devui_if_enabled(_devui_agent)


# Model info endpoint (CTR-0041, CTR-0069, PRP-0035)
@app.get("/api/model", tags=["Model"])
async def get_model_info():
    """Return model configuration for frontend model selector and context window display."""
    return {
        "models": agent_registry.available_models,
        "default_model": agent_registry.default_model,
        # Context-window limits are catalog-aware (CTR-0069, PRP-0109): an
        # offering's declared context_window wins; otherwise MODEL_MAX_CONTEXT_TOKENS
        # applies (byte-for-byte on the legacy lane).
        "max_context_tokens": providers.get_max_context_tokens(),
        "max_context_tokens_map": providers.max_context_tokens_map(agent_registry.available_models),
        # Per-model generation option catalog for the model-options panel
        # (CTR-0069 v4 / CTR-0102 v4, PRP-0081): model -> {options: [descriptor]}.
        # Covers reasoning effort plus, for gpt-5.x, text verbosity.
        "model_options": providers.model_options_map(agent_registry.available_models),
        # Per-model reasoning effort catalog (CTR-0069 / CTR-0102, PRP-0071):
        # model -> {allowed, default}. Retained as a derived back-compat subset of
        # `model_options` (the effort axis) for clients that still read it.
        "reasoning_options": providers.reasoning_options_map(agent_registry.available_models),
        # Per-model background-response capability (CTR-0045, PRP-0073):
        # model -> bool. The UI disables the Background toggle for models
        # whose provider does not support background runs (e.g. Anthropic).
        "background_supported_map": providers.background_supported_map(agent_registry.available_models),
        # Per-model structured-output capability (CTR-0069 v5 / CTR-0102 v5,
        # PRP-0082): model -> {supported, native, fallback}. The UI enables /
        # annotates the structured-output control per model (UDR-0058 D6).
        "structured_output": providers.structured_output_map(agent_registry.available_models),
        # Active declarative agent (CTR-0142 / CTR-0144, PRP-0094, UDR-0072): its
        # mapped option defaults + structured-output default, so the SPA reflects the
        # active agent's effort / verbosity / structured output and refreshes them when
        # the agent is switched. Behavior is enforced server-side regardless (the AG-UI
        # endpoint applies these as defaults); this is for the UI to display.
        "active_agent": _active_agent_info(),
    }


def _active_agent_info() -> dict:
    """Compact view of the active declarative agent for the model UI (PRP-0094)."""
    spec = active_spec()
    so = spec.structured_output or {}
    return {
        "id": spec.id,
        "name": spec.name,
        "model_options": spec.model_options_override or {},
        "output_format": so.get("mode", "none"),
        "has_schema": bool(so.get("schema")),
        # The actual mapped JSON Schema so the SPA structured-output editor can show it
        # when the agent declares one (CTR-0144, PRP-0094).
        "output_schema": so.get("schema"),
    }


# MCP Apps config endpoint (CTR-0066, PRP-0034)
@app.get("/api/mcp-apps/config", tags=["MCP Apps"])
async def get_mcp_apps_config():
    """Return MCP Apps configuration for frontend sandbox proxy discovery."""
    return {"sandbox_port": settings.mcp_apps_sandbox_port}


# Static file serving (CTR-0005)
# Dual-mode path resolution: explicit override -> dev layout -> bundled assets
_explicit = Path(settings.frontend_dist).resolve()
if _explicit.is_dir():
    dist_path = _explicit
else:
    _dev_path = Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist"
    _bundled_path = Path(__file__).resolve().parent / "_frontend_dist"
    if _dev_path.is_dir():
        dist_path = _dev_path
    elif _bundled_path.is_dir():
        dist_path = _bundled_path
    else:
        dist_path = None

if dist_path is not None:
    app.mount("/assets", StaticFiles(directory=dist_path / "assets"), name="static-assets")

    # index.html must never be cached: it is the manifest that points at the
    # content-hashed /assets/*.js chunks. If a browser (or a tunnel / CDN) serves a stale
    # index after the frontend is rebuilt, its lazy imports request old chunk hashes that
    # no longer exist on disk -> 404 "Failed to fetch dynamically imported module". The
    # hashed assets themselves are immutable and safe to cache for a long time.
    _INDEX_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """SPA fallback: serve index.html for all non-API routes."""
        resolved = (dist_path / full_path).resolve()
        # A real file other than the SPA shell (e.g. favicon) -- serve as-is.
        if resolved.is_file() and resolved.is_relative_to(dist_path) and resolved.name != "index.html":
            return FileResponse(resolved)
        return FileResponse(dist_path / "index.html", headers=_INDEX_NO_CACHE)
