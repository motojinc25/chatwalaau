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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application lifespan: startup and shutdown hooks."""
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
    yield
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

# Session management API (CTR-0015)
app.include_router(session_router)

# Prompt Templates API (CTR-0047)
app.include_router(templates_router)

# Image upload API (CTR-0022)
app.include_router(upload_router)

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

# Server -> client notification WebSocket (CTR-0110, PRP-0077). Real-time push
# channel; first event type is session_title (CTR-0109).
from app.notifications import register_notifications_endpoint

register_notifications_endpoint(app)

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
        "max_context_tokens": settings.get_max_context_tokens(),
        "max_context_tokens_map": settings.max_context_tokens_map,
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

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """SPA fallback: serve index.html for all non-API routes."""
        resolved = (dist_path / full_path).resolve()
        if resolved.is_file() and resolved.is_relative_to(dist_path):
            return FileResponse(resolved)
        return FileResponse(dist_path / "index.html")
