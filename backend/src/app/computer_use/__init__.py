"""Computer Use: a provider-neutral see-plan-act loop on the user's own desktop (FEAT-0071).

PRP-0189 / UDR-0171. The agent sees a native Windows application (screenshot + UI
Automation elements), plans ONE batch of actions in a bounded DSL (CTR-0230), and the
backend executes it, waits until the screen is stable, and returns the result -- one
model call per decision cycle. It works with any model offering that accepts images
in function results (the OpenAI Responses and Anthropic lanes).

It exists only where the backend owns the user's screen: Windows, an interactive
session, loopback bind, a loopback caller (UDR-0171 D1) -- the Desktop app and local
development satisfy that without the core ever detecting the Desktop.

Layout: ``dsl`` (CTR-0230), ``perception`` (CTR-0231), ``executor`` (CTR-0232),
``engine`` (the per-tool operations), ``tools`` (CTR-0229), ``retention`` (CTR-0234),
``router`` (CTR-0233), ``mcp_backend`` (CTR-0237, the desktop behind MCP), ``worker`` (the
one desktop thread and the kill-switch hotkey). The Windows primitives themselves live in
the native desktop provider ``chatwalaau-computer-use`` (CAP-012, PRP-0191, Rust source in
``computer-use/``), a separate wheel that always runs as a stdio MCP provider process.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.computer_use.availability import offered

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def register_computer_use(app: FastAPI) -> None:
    """Mount CTR-0233 and, when offered, arm the kill-switch hotkey."""
    from app.computer_use.availability import availability as host_availability
    from app.computer_use.router import router

    app.include_router(router)
    verdict = host_availability()
    if verdict.offered:
        from app.computer_use.worker import start_hotkey

        start_hotkey()
        logger.info("Computer Use offered on this host (PRP-0189)")
    else:
        logger.info("Computer Use not offered: %s", verdict.reason)


def shutdown() -> None:
    from app.computer_use.worker import shutdown as stop_worker

    stop_worker()


__all__ = ["offered", "register_computer_use", "shutdown"]
