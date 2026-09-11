"""Conversation Compaction resolver (CTR-0098 v4, PRP-0163, UDR-0141).

The Prompt lane runs ONE fixed, ordered two-stage pipeline. There is no
strategy name to select and no unknown-name fallback branch::

    (1) SelectiveToolCallCompactionStrategy(keep_last_tool_call_groups=K)
            exclude older tool-call groups, keep the newest K
    (2) SlidingWindowStrategy(keep_last_groups=N, preserve_system=B)
            keep the newest N of whatever (1) left included

The order is normative, not stylistic (UDR-0141 D1). Both stages mutate the
same annotated message list through MAF's ``set_excluded`` and both re-read the
included-group set at call time, so stage (2) counts only what stage (1) left
included. Reversed, stage (1)'s K under-counts against a set the window has
already thinned and the active request is excluded at N tool rounds -- measured
at the pinned ``agent-framework 1.17.0``.

Each stage covers what the other cannot. Stage (1) alone is a no-op on a
session without tool calls; stage (2) alone bounds that session but ranks the
active request equal to a tool round, so it is the one that can lose the
request. Composed, a 1000-round tool turn and a 1000-turn chat session both
project to 26 messages, with the request present.

The request the run is answering survives STRUCTURALLY rather than by a wrapper
(UDR-0141 D3, superseding UDR-0138 D1/D2/D3). Stage (1) caps the INCLUDED
tool-call groups at K across the whole history, so the request sits at most
K+1 groups from the end and stage (2) retains it whenever ``N > K``. The
configuration is held to the stronger ``2K < N`` so that the measured tolerance
for interim ``assistant_text`` groups -- exactly ``N - K - 1``, and stage (1)
does not bound those -- is at least as large as the tool budget itself. That
rule is enforced on the write path (``app.app_settings.store``) and degraded on
the load path (``Settings._validate_compaction``), never argued in a comment
(UDR-0141 D4/D5).

The resolved object is consumed by ``app.agui.agent_factory`` on every registry
build -- at ``AgentRegistry.__init__`` AND at every ``AgentRegistry.rebuild()``
(UDR-0140 D1/D2) -- and by ``app.workflow.builder`` for every Prompt workflow
node, which is the same population and needs no special case. Compaction
operates purely on the in-memory message list MAF assembles for the next model
call; the on-disk session JSON owned by ``FileHistoryProvider`` (CTR-0014) is
not mutated (UDR-0042 D4).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

_logger = logging.getLogger(__name__)


class CompactionPipeline:
    """Run an ordered sequence of strategies over the shared message list.

    Implements MAF's ``CompactionStrategy`` Protocol, which is the extension
    route UDR-0042 D3 names (it forbids a wrapper Protocol of our own, not an
    implementation OF theirs). This is the ONLY in-house strategy
    implementation; it replaced ``AnchorLastUserTurnStrategy`` rather than
    joining it, so the count stayed at one and the private-MAF-symbol
    dependency that wrapper carried (``_compaction.set_excluded``) is gone.

    Stages share one annotated list and each re-reads the included-group set, so
    running them in sequence is what composes them -- there is nothing to merge.
    ``changed`` is the OR of the stages, because MAF only needs to know whether
    the projection moved.

    Takes no client and calls no model, so the token-ledger coverage statement
    (UDR-0136 D11 as corrected by UDR-0137) is unaffected.
    """

    def __init__(self, *stages: Any) -> None:
        if not stages:
            msg = "CompactionPipeline requires at least one stage"
            raise ValueError(msg)
        self._stages = tuple(stages)

    @property
    def stages(self) -> tuple[Any, ...]:
        """The ordered stages. Exposed for tests and logging, not for dispatch."""
        return self._stages

    async def __call__(self, messages: list[Any]) -> bool:
        changed = False
        for stage in self._stages:
            changed = (await stage(messages)) or changed
        return changed

    def __repr__(self) -> str:
        inner = ", ".join(repr(stage) for stage in self._stages)
        return f"CompactionPipeline({inner})"


def resolve_compaction_strategy() -> Any | None:
    """Return the fixed compaction pipeline, or ``None`` when it is disabled.

    Read on every registry build -- construction and rebuild alike (UDR-0140
    D2) -- and the resolved instance is reused across every per-model Agent of
    that build.

    The two stages are enabled and disabled as a UNIT (UDR-0141 D2): the D3
    guarantee is a property of the composition, and a configuration able to run
    the window alone is a configuration able to lose the active request.
    """
    # Local import keeps the agent_framework import cost confined to the
    # registry constructor call site -- modules that never instantiate
    # Agents (e.g., test invariants that import config) do not pay it.
    from agent_framework import (
        SelectiveToolCallCompactionStrategy,
        SlidingWindowStrategy,
    )

    if not settings.compaction_enabled:
        _logger.info("Compaction disabled (COMPACTION_ENABLED=false)")
        return None

    keep_tool_call_groups = settings.compaction_keep_last_tool_call_groups
    keep_groups = settings.compaction_keep_last_groups
    preserve_system = settings.compaction_preserve_system

    pipeline = CompactionPipeline(
        SelectiveToolCallCompactionStrategy(keep_last_tool_call_groups=keep_tool_call_groups),
        SlidingWindowStrategy(keep_last_groups=keep_groups, preserve_system=preserve_system),
    )
    _logger.info(
        "Compaction pipeline: selective-tool-call(keep_last_tool_call_groups=%d) "
        "-> sliding-window(keep_last_groups=%d, preserve_system=%s)",
        keep_tool_call_groups,
        keep_groups,
        preserve_system,
    )
    return pipeline


__all__ = ["CompactionPipeline", "resolve_compaction_strategy"]
