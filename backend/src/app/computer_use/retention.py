"""Observation Retention Middleware (CTR-0234, PRP-0189 Section 2.9, UDR-0171 D8).

Tool results are never replayed across turns (``FileHistoryProvider`` strips
``tool_calls``), but INSIDE one run the MAF tool loop re-sends the whole message list
on every model call -- so N cycles would carry N screenshots. This ``ChatMiddleware``
runs before EVERY model call of the loop and keeps only the newest
``computer_use_keep_images`` observation images; each older one becomes a short text
placeholder. The text part of every result (what happened, the element list) stays.

It touches ONLY image items tagged by the computer tools
(``additional_properties["computer_use_observation"]``): a user upload or any other
content passes through untouched. It never mutates a shared message -- the context's
list is replaced item by item with shallow copies, so the loop's own history is not
rewritten.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from agent_framework import ChatMiddleware, Content

from app.core.config import settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agent_framework import ChatContext

TAG = "computer_use_observation"


def _tag(item: Any) -> str | None:
    props = getattr(item, "additional_properties", None) or {}
    value = props.get(TAG)
    return str(value) if value else None


def prune(messages: list[Any], keep: int) -> list[Any]:
    """Return ``messages`` with all but the newest ``keep`` tagged images replaced."""
    tagged: list[tuple[int, int, int]] = []  # (message index, content index, item index)
    for mi, message in enumerate(messages):
        for ci, content in enumerate(getattr(message, "contents", None) or []):
            for ii, item in enumerate(getattr(content, "items", None) or []):
                if _tag(item):
                    tagged.append((mi, ci, ii))
    if len(tagged) <= keep:
        return messages
    drop = tagged[: len(tagged) - keep]
    out = list(messages)
    by_message: dict[int, list[tuple[int, int]]] = {}
    for mi, ci, ii in drop:
        by_message.setdefault(mi, []).append((ci, ii))
    for mi, positions in by_message.items():
        message = copy.copy(out[mi])
        contents = list(message.contents)
        for ci in {c for c, _ in positions}:
            content = copy.copy(contents[ci])
            items = list(content.items or [])
            for c, ii in positions:
                if c == ci:
                    items[ii] = Content.from_text(f"[screenshot {_tag(items[ii])} superseded]")
            content.items = items
            contents[ci] = content
        message.contents = contents
        out[mi] = message
    return out


class ObservationRetentionMiddleware(ChatMiddleware):
    """Keeps only the newest observation image(s) in every model call (D8)."""

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        keep = max(1, int(settings.computer_use_keep_images))
        messages = list(context.messages)
        pruned = prune(messages, keep)
        if pruned is not messages:
            context.messages[:] = pruned
        await call_next()


__all__ = ["TAG", "ObservationRetentionMiddleware", "prune"]
