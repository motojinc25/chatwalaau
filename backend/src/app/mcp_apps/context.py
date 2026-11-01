"""MCP App model context for one turn (CTR-0067, PRP-0240, UDR-0019 D13).

A View's ``ui/update-model-context`` is kept by the SPA (last one per View) and sent with
the next user turn in the AG-UI ``RunAgentInput.context`` list as
``{"description": "mcp_app:<server>/<tool>", "value": "<text>"}``. This module adds those
entries to that turn's user message as one context block. The block reaches the model
for this run only: the SPA saves the user's own text, so it is never shown in the chat
and never persisted as a message.
"""

import logging
from typing import Any

from agent_framework import Content

logger = logging.getLogger(__name__)

CONTEXT_PREFIX = "mcp_app:"
MAX_ENTRIES = 8
MAX_VALUE_CHARS = 16_000


def view_context_entries(context: list[dict[str, Any]] | None) -> list[tuple[str, str]]:
    """The (source, text) pairs of MCP App context entries, bounded."""
    out: list[tuple[str, str]] = []
    for entry in context or []:
        if not isinstance(entry, dict):
            continue
        description = entry.get("description")
        value = entry.get("value")
        if not isinstance(description, str) or not description.startswith(CONTEXT_PREFIX):
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        if len(value) > MAX_VALUE_CHARS:
            logger.warning("MCP App context from %s truncated to %d characters", description, MAX_VALUE_CHARS)
            value = value[:MAX_VALUE_CHARS]
        out.append((description[len(CONTEXT_PREFIX) :], value))
        if len(out) >= MAX_ENTRIES:
            break
    return out


def inject_view_context(maf_messages: list[Any], context: list[dict[str, Any]] | None) -> int:
    """Append the MCP App context block to the turn's last user message.

    Returns the number of entries added (0 when there is no entry or no user message).
    """
    entries = view_context_entries(context)
    if not entries:
        return 0
    target = next((m for m in reversed(maf_messages) if getattr(m, "role", None) == "user"), None)
    if target is None:
        return 0
    blocks = "\n".join(f'<mcp-app-context source="{source}">\n{text}\n</mcp-app-context>' for source, text in entries)
    target.contents.append(
        Content.from_text(
            text=(
                "Context the user's open MCP App views provided (not written by the user; use it only "
                f"when it is relevant):\n{blocks}"
            )
        )
    )
    logger.info("MCP App context added to the turn: %d view(s)", len(entries))
    return len(entries)
