"""Conversation Compaction resolver (CTR-0098, PRP-0067, UDR-0042).

Maps the operator Settings ``COMPACTION_STRATEGY`` /
``COMPACTION_KEEP_LAST_GROUPS`` / ``COMPACTION_PRESERVE_SYSTEM`` triplet
to a Microsoft Agent Framework ``CompactionStrategy`` instance (or
``None`` when compaction is disabled).

The resolved object is consumed once at ``AgentRegistry.__init__`` time
(``app.agui.agent_factory``) and passed as the ``compaction_strategy=``
keyword on every ``Agent(...)`` construction call (CTR-0007 v7,
UDR-0042 D1). Compaction operates purely on the in-memory message list
MAF assembles for the next model call; the on-disk session JSON owned
by ``FileHistoryProvider`` (CTR-0014) is not mutated (UDR-0042 D4).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

_logger = logging.getLogger(__name__)

# Names that mean "compaction disabled". Trimmed lowercased value
# is compared against this set.
_DISABLED_VALUES = frozenset({"", "none", "off", "disabled"})


def resolve_compaction_strategy() -> Any | None:
    """Return a MAF ``CompactionStrategy`` instance, or ``None`` if disabled.

    Read once per process at registry construction; the resolved instance
    is reused across every per-model Agent. Unknown strategy names log
    a WARNING and fall back to ``SlidingWindowStrategy(keep_last_groups=N)``
    (UDR-0042 D2).
    """
    # Local import keeps the agent_framework import cost confined to the
    # registry constructor call site -- modules that never instantiate
    # Agents (e.g., test invariants that import config) do not pay it.
    from agent_framework import (
        SelectiveToolCallCompactionStrategy,
        SlidingWindowStrategy,
        ToolResultCompactionStrategy,
    )

    name = (settings.compaction_strategy or "").strip().lower()
    keep = settings.compaction_keep_last_groups
    preserve_system = settings.compaction_preserve_system

    if name in _DISABLED_VALUES:
        _logger.info("Compaction disabled (COMPACTION_STRATEGY=%r)", settings.compaction_strategy)
        return None

    if name == "sliding-window":
        strategy = SlidingWindowStrategy(keep_last_groups=keep, preserve_system=preserve_system)
        _logger.info(
            "Compaction strategy: sliding-window (keep_last_groups=%d, preserve_system=%s)",
            keep,
            preserve_system,
        )
        return strategy

    if name == "selective-tool-call":
        if not preserve_system:
            _logger.info(
                "COMPACTION_PRESERVE_SYSTEM=false is ignored by selective-tool-call strategy (sliding-window only)"
            )
        strategy = SelectiveToolCallCompactionStrategy(keep_last_tool_call_groups=keep)
        _logger.info("Compaction strategy: selective-tool-call (keep_last_tool_call_groups=%d)", keep)
        return strategy

    if name == "tool-result":
        if not preserve_system:
            _logger.info("COMPACTION_PRESERVE_SYSTEM=false is ignored by tool-result strategy (sliding-window only)")
        strategy = ToolResultCompactionStrategy(keep_last_tool_call_groups=keep)
        _logger.info("Compaction strategy: tool-result (keep_last_tool_call_groups=%d)", keep)
        return strategy

    # Unknown name -> fall back to the safe default per UDR-0042 D2.
    # The Settings validator already logged a WARNING for unknown names
    # at startup; emit one more here so the operator can correlate the
    # warning with the actual fallback that was chosen.
    _logger.warning(
        "Unknown COMPACTION_STRATEGY=%r; falling back to sliding-window (keep_last_groups=%d)",
        settings.compaction_strategy,
        keep,
    )
    return SlidingWindowStrategy(keep_last_groups=keep, preserve_system=preserve_system)


__all__ = ["resolve_compaction_strategy"]
