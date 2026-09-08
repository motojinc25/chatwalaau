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

PRP-0160 / UDR-0138: every resolved strategy is wrapped in
``AnchorLastUserTurnStrategy`` so that no strategy can elide the request
the current run is answering. MAF applies compaction before EVERY model
call -- once per tool-loop iteration, not once per turn -- and
``SlidingWindowStrategy`` ranks the user's request equal to a tool round,
so a turn that called four tools used to lose its own question and answer
something else. The anchor is not operator-configurable (UDR-0138 D3).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

_logger = logging.getLogger(__name__)

# Names that mean "compaction disabled". Trimmed lowercased value
# is compared against this set.
_DISABLED_VALUES = frozenset({"", "none", "off", "disabled"})


class AnchorLastUserTurnStrategy:
    """Run the selected strategy, then re-include the trailing user group.

    Implements MAF's ``CompactionStrategy`` Protocol (UDR-0042 D3 permits an
    implementation OF that Protocol; it forbids a wrapper Protocol of our own).
    The rule lives here, above the selected strategy, rather than inside any one
    of them, because "compaction may not elide the active request" applies to
    every strategy including ones not yet adopted (UDR-0138 D2).

    The trailing ``user`` message IS the active request at every point of a run:
    MAF appends the new user message before the first model call and appends no
    further user message during the tool loop. A ``user`` group is exactly one
    message, so re-including it can neither split a group nor orphan a
    ``function_call_output``.

    Takes no client and calls no model, so the token-ledger coverage statement
    (UDR-0136 D11 as corrected by UDR-0137) is unaffected.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def inner(self) -> Any:
        """The wrapped strategy. Exposed for tests and logging, not for dispatch."""
        return self._inner

    async def __call__(self, messages: list[Any]) -> bool:
        # Private MAF surface, pinned by
        # tests/invariants/test_prp0153_maf_116_upgrade.py (UDR-0086 D5).
        from agent_framework._compaction import set_excluded

        changed = await self._inner(messages)
        for message in reversed(messages):
            if message.role == "user":
                return set_excluded(message, excluded=False) or changed
        return changed

    def __repr__(self) -> str:
        return f"AnchorLastUserTurnStrategy({self._inner!r})"


def resolve_compaction_strategy() -> Any | None:
    """Return a MAF ``CompactionStrategy`` instance, or ``None`` if disabled.

    Read once per process at registry construction; the resolved instance
    is reused across every per-model Agent. Unknown strategy names log
    a WARNING and fall back to ``SlidingWindowStrategy(keep_last_groups=N)``
    (UDR-0042 D2).

    Every non-``None`` branch is wrapped in ``AnchorLastUserTurnStrategy``
    (UDR-0138 D1). The disabled branch still returns ``None``: there is
    nothing to anchor where nothing compacts.
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
        return AnchorLastUserTurnStrategy(strategy)

    if name == "selective-tool-call":
        if not preserve_system:
            _logger.info(
                "COMPACTION_PRESERVE_SYSTEM=false is ignored by selective-tool-call strategy (sliding-window only)"
            )
        strategy = SelectiveToolCallCompactionStrategy(keep_last_tool_call_groups=keep)
        _logger.info("Compaction strategy: selective-tool-call (keep_last_tool_call_groups=%d)", keep)
        return AnchorLastUserTurnStrategy(strategy)

    if name == "tool-result":
        if not preserve_system:
            _logger.info("COMPACTION_PRESERVE_SYSTEM=false is ignored by tool-result strategy (sliding-window only)")
        strategy = ToolResultCompactionStrategy(keep_last_tool_call_groups=keep)
        _logger.info("Compaction strategy: tool-result (keep_last_tool_call_groups=%d)", keep)
        return AnchorLastUserTurnStrategy(strategy)

    # Unknown name -> fall back to the safe default per UDR-0042 D2.
    # The Settings validator already logged a WARNING for unknown names
    # at startup; emit one more here so the operator can correlate the
    # warning with the actual fallback that was chosen.
    _logger.warning(
        "Unknown COMPACTION_STRATEGY=%r; falling back to sliding-window (keep_last_groups=%d)",
        settings.compaction_strategy,
        keep,
    )
    return AnchorLastUserTurnStrategy(SlidingWindowStrategy(keep_last_groups=keep, preserve_system=preserve_system))


__all__ = ["AnchorLastUserTurnStrategy", "resolve_compaction_strategy"]
