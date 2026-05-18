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
import importlib.metadata
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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import AzureOpenAI

from app.agui.agent_factory import build_devui_agent, create_agent_registry
from app.agui.endpoint import register_agui_endpoints
from app.auth.web_auth import router as web_auth_router
from app.azure_credential import get_azure_openai_kwargs
from app.core.config import settings
from app.devui.launcher import launch_devui_if_enabled
from app.image_gen.router import router as image_edit_router
from app.mcp.lifecycle import activate_mcp, prepare_mcp, shutdown_mcp
from app.mcp_apps.router import router as mcp_apps_router
from app.openai_api.router import register_openai_api
from app.prompt_templates.router import router as templates_router
from app.session.router import router as session_router
from app.stt.router import router as stt_router
from app.stt.router import set_stt_provider
from app.stt.whisper import AzureOpenAIWhisperProvider
from app.tts.elevenlabs import ElevenLabsTTSProvider
from app.tts.router import router as tts_router
from app.tts.router import set_tts_provider
from app.upload.router import router as upload_router

# Suppress pydantic warnings from agent-framework-ag-ui's Field(validation_alias=...) usage
warnings.filterwarnings("ignore", category=UserWarning, module=r"pydantic\._internal\._generate_schema")

load_dotenv()

# Logging is configured via log_conf.yaml (passed to uvicorn --log-config)

# Read version: importlib.metadata (pip install) -> pyproject.toml fallback (dev)
try:
    _app_version = importlib.metadata.version("chatwalaau")
except importlib.metadata.PackageNotFoundError:
    import tomllib

    _pyproject_path = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    if _pyproject_path.exists():
        with _pyproject_path.open("rb") as _f:
            _app_version = tomllib.load(_f).get("project", {}).get("version", "0.0.0")
    else:
        _app_version = "0.0.0"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application lifespan: startup and shutdown hooks."""
    # Startup: activate MCP servers (CTR-0061, PRP-0031)
    # prepare_mcp() was already called at module level before create_agent()
    await activate_mcp()
    yield
    # Shutdown: stop MCP servers
    await shutdown_mcp()


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
# Credential resolution centralised in app.azure_credential (PRP-0058, UDR-0034).
if settings.azure_openai_endpoint:
    stt_client = AzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_version="2024-10-21",
        **get_azure_openai_kwargs(),
    )
    set_stt_provider(AzureOpenAIWhisperProvider(stt_client, model=settings.whisper_deployment_name))
app.include_router(stt_router)

# Text-to-Speech API (CTR-0039)
if settings.elevenlabs_api_key and settings.tts_voice_id:
    set_tts_provider(
        ElevenLabsTTSProvider(
            api_key=settings.elevenlabs_api_key,
            voice_id=settings.tts_voice_id,
            model_id=settings.tts_model_id,
        )
    )
app.include_router(tts_router)

# Prepare MCP tools synchronously before agent creation (CTR-0060, PRP-0031)
# activate_mcp() is called later in lifespan to start servers asynchronously
prepare_mcp()

# Multi-Model Agent Registry (CTR-0070, PRP-0035)
agent_registry = create_agent_registry()

# AG-UI endpoint (CTR-0009) -- receives registry for per-request model selection
register_agui_endpoints(app, agent_registry=agent_registry)

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
