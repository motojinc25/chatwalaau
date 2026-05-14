"""DevUI server launcher (CTR-0025, PRP-0016).

Launches Microsoft Agent Framework DevUI as a parallel development server
in a daemon thread when DEVUI_ENABLED=true. Provides web-based agent
testing UI and OpenAI-compatible Responses API with Bearer token auth.
"""

import logging
import threading

from agent_framework import Agent

from app.core.config import settings

logger = logging.getLogger(__name__)


def launch_devui_if_enabled(agent: Agent) -> None:
    """Launch DevUI server in background thread if enabled (CTR-0025).

    Args:
        agent: Shared Agent instance from agent_factory (CTR-0026).
    """
    if not settings.devui_enabled:
        return

    try:
        from agent_framework.devui import serve
    except (ImportError, ModuleNotFoundError):
        logger.warning(
            "DEVUI_ENABLED=true but agent-framework-devui is not installed. "
            "Install with: uv add --group dev agent-framework-devui"
        )
        return

    def _run() -> None:
        serve(
            entities=[agent],
            port=settings.devui_port,
            host=settings.app_host,
            auto_open=False,
            ui_enabled=True,
            instrumentation_enabled=settings.devui_tracing,
            mode=settings.devui_mode,
            auth_enabled=settings.devui_auth_enabled,
            auth_token=settings.devui_auth_token or None,
        )

    thread = threading.Thread(target=_run, daemon=True, name="devui-server")
    thread.start()

    logger.info(
        "DevUI server starting on http://%s:%d/ (auth=%s, mode=%s)",
        settings.app_host,
        settings.devui_port,
        settings.devui_auth_enabled,
        settings.devui_mode,
    )
