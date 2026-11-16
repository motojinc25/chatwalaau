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

import json
import logging
from typing import Any

from app.core.config import settings

_logger = logging.getLogger(__name__)

# PRP-0255 (UDR-0236 D1): the MAF skill tools whose results ARE a skill's
# instructions or one of its resources. Matched by the public tool name.
SKILL_CONTEXT_TOOLS = frozenset({"load_skill", "read_skill_resource"})

# MAF answers an unknown or empty skill name with a plain string starting with
# this prefix instead of raising (``SkillsProvider._load_skill``).
_SKILL_ERROR_PREFIX = "Error:"

# The exclude reasons MAF's two stages write (public behaviour, read back only
# to count them in the PRP-0255 log line).
_REASON_TOOL_CALL = "tool_call_compaction"
_REASON_WINDOW = "sliding_window"
_REASON_PIN = "skill_context_pin"


def _annotation(message: Any) -> dict[str, Any]:
    from agent_framework import GROUP_ANNOTATION_KEY

    value = message.additional_properties.get(GROUP_ANNOTATION_KEY)
    return value if isinstance(value, dict) else {}


def _ordered_groups(messages: list[Any]) -> list[tuple[str, str | None, list[Any]]]:
    """(group id, kind, messages) in history order, from MAF's public annotation keys."""
    from agent_framework import GROUP_ID_KEY, GROUP_KIND_KEY

    order: list[str] = []
    kinds: dict[str, str | None] = {}
    members: dict[str, list[Any]] = {}
    for message in messages:
        annotation = _annotation(message)
        group_id = annotation.get(GROUP_ID_KEY)
        if not isinstance(group_id, str):
            continue
        if group_id not in members:
            order.append(group_id)
            members[group_id] = []
            kind = annotation.get(GROUP_KIND_KEY)
            kinds[group_id] = kind if isinstance(kind, str) else None
        members[group_id].append(message)
    return [(group_id, kinds[group_id], members[group_id]) for group_id in order]


def _is_excluded(message: Any) -> bool:
    from agent_framework import EXCLUDED_KEY

    return bool(message.additional_properties.get(EXCLUDED_KEY, False))


def _arguments(call: Any) -> dict[str, Any]:
    raw = getattr(call, "arguments", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _result_text(result: Any) -> str:
    value = getattr(result, "result", None)
    if isinstance(value, str):
        return value
    parts = [getattr(item, "text", None) for item in (getattr(result, "items", None) or [])]
    return "".join(part for part in parts if isinstance(part, str))


def _skill_context_keys(group: list[Any]) -> list[tuple[str, str]] | None:
    """The (key, label) pairs a group loads, or None when the group is not a pure skill load.

    Pure means: every FUNCTION call in the group is a skill tool and succeeded.
    Hosted calls (web search, code interpreter) and reasoning do not count --
    they are never function calls -- so a skill load the model made after a web
    search still qualifies. Any other function call disqualifies the group,
    because re-including it would also keep an unrelated tool output.
    """
    contents = [content for message in group for content in (message.contents or [])]
    calls = [content for content in contents if content.type == "function_call"]
    if not calls or any(getattr(call, "name", None) not in SKILL_CONTEXT_TOOLS for call in calls):
        return None
    results = {getattr(c, "call_id", None): c for c in contents if c.type == "function_result"}
    keys: list[tuple[str, str]] = []
    for call in calls:
        result = results.get(getattr(call, "call_id", None))
        if result is None or getattr(result, "exception", None):
            return None
        if _result_text(result).lstrip().startswith(_SKILL_ERROR_PREFIX):
            return None
        args = _arguments(call)
        skill = str(args.get("skill_name") or "").strip()
        if not skill:
            return None
        label = skill
        if call.name == "read_skill_resource":
            resource = str(args.get("resource_name") or "").strip()
            if not resource:
                return None
            label = f"{skill}/{resource}"
        keys.append((f"{call.name}:{label.casefold()}", label))
    return keys


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

    def __init__(self, *stages: Any, skill_context_max: int = 0) -> None:
        if not stages:
            msg = "CompactionPipeline requires at least one stage"
            raise ValueError(msg)
        self._stages = tuple(stages)
        # PRP-0255 (UDR-0236 D1): 0 turns the pin pass off.
        self._skill_context_max = max(0, int(skill_context_max))

    @property
    def stages(self) -> tuple[Any, ...]:
        """The ordered stages. Exposed for tests and logging, not for dispatch."""
        return self._stages

    @property
    def skill_context_max(self) -> int:
        """How many skill contexts the pin pass keeps; 0 when the pass is off."""
        return self._skill_context_max

    async def __call__(self, messages: list[Any]) -> bool:
        changed = False
        for stage in self._stages:
            changed = (await stage(messages)) or changed
        pinned: list[str] = []
        if self._skill_context_max:
            pinned = self._pin_skill_contexts(messages)
            changed = changed or bool(pinned)
        if changed:
            self._log_projection(messages, pinned)
        return changed

    def _pin_skill_contexts(self, messages: list[Any]) -> list[str]:
        """Re-include the newest skill-context groups the stages excluded (UDR-0236 D1-D3).

        Walks the pure skill-load groups newest first. Each skill (or skill
        resource) key counts once, whether its newest group is already in view
        or has to be re-included; the walk stops after ``skill_context_max``
        keys. Only ``EXCLUDED_KEY`` is flipped back, so every message keeps its
        position and each call stays beside its result. Nothing is excluded
        here (D2): the request guarantee of UDR-0141 D3 cannot be affected.
        Returns the labels of the re-included groups, for the log line.
        """
        from agent_framework import EXCLUDE_REASON_KEY, EXCLUDED_KEY

        candidates = [
            (group, keys)
            for _group_id, kind, group in _ordered_groups(messages)
            if kind == "tool_call" and (keys := _skill_context_keys(group)) is not None
        ]
        seen: set[str] = set()
        restored: list[str] = []
        for group, keys in reversed(candidates):
            if len(seen) >= self._skill_context_max:
                break
            new = [(key, label) for key, label in keys if key not in seen]
            if not new:
                continue
            seen.update(key for key, _label in keys)
            if not any(_is_excluded(message) for message in group):
                continue
            for message in group:
                message.additional_properties[EXCLUDED_KEY] = False
                message.additional_properties[EXCLUDE_REASON_KEY] = _REASON_PIN
            restored.extend(label for _key, label in new)
        return restored

    def _log_projection(self, messages: list[Any], pinned: list[str]) -> None:
        """One INFO line per changed projection: counts and skill names, never text (UDR-0236 D5)."""
        from agent_framework import EXCLUDE_REASON_KEY

        excluded = {_REASON_TOOL_CALL: 0, _REASON_WINDOW: 0}
        included = 0
        for _group_id, _kind, group in _ordered_groups(messages):
            if not all(_is_excluded(message) for message in group):
                included += 1
                continue
            reason = group[0].additional_properties.get(EXCLUDE_REASON_KEY)
            if reason in excluded:
                excluded[reason] += 1
        _logger.info(
            "Compaction: tool_call_excluded=%d window_excluded=%d skill_context_kept=%d%s included_groups=%d",
            excluded[_REASON_TOOL_CALL],
            excluded[_REASON_WINDOW],
            len(pinned),
            f" ({', '.join(pinned)})" if pinned else "",
            included,
        )

    def __repr__(self) -> str:
        inner = ", ".join(repr(stage) for stage in self._stages)
        return f"CompactionPipeline({inner}, skill_context_max={self._skill_context_max})"


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
    skill_context_max = settings.compaction_skill_context_max if settings.compaction_keep_skill_context else 0

    pipeline = CompactionPipeline(
        SelectiveToolCallCompactionStrategy(keep_last_tool_call_groups=keep_tool_call_groups),
        SlidingWindowStrategy(keep_last_groups=keep_groups, preserve_system=preserve_system),
        skill_context_max=skill_context_max,
    )
    _logger.info(
        "Compaction pipeline: selective-tool-call(keep_last_tool_call_groups=%d) "
        "-> sliding-window(keep_last_groups=%d, preserve_system=%s) -> skill-context pin(max=%d)",
        keep_tool_call_groups,
        keep_groups,
        preserve_system,
        skill_context_max,
    )
    return pipeline


__all__ = ["CompactionPipeline", "resolve_compaction_strategy"]
