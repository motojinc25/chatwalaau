"""Harness Run Progress (CTR-0219, PRP-0181, UDR-0163 D3/D4/D5).

The ONE module that turns a harness run's loop into something a user can follow.

Why it exists
-------------
The harness runs ``todos_remaining()`` through MAF's ``AgentLoopMiddleware``. Between
iterations MAF injects ``user`` messages -- ``"Progress so far: ..."`` and
``"Continue working on the task. If it is complete, say so."`` -- and, in streaming
mode, yields them as updates with ``role="user"`` so a consumer can see the turns that
drive the next iteration. The AG-UI endpoint used to print them inside the answer.
They now END the text message and become a ``harness_progress`` event instead
(UDR-0163 D3); the model's own history keeps them unchanged.

What it does
------------
- ``is_loop_nudge(update)`` recognises one injected update (``role == "user"``).
- ``is_progress_tool(name)`` names the tools whose result changes the picture
  (``todos_*``, ``mode_set``).
- ``HarnessRunTracker`` counts iterations (one boundary may carry TWO user updates:
  the progress log and the nudge), reads the Todo list and the mode from the harness
  session through MAF's PUBLIC API (``TodoProvider``, ``AgentModeProvider``,
  ``get_agent_mode``) -- never by parsing tool-result prose -- and builds the event
  value. Every value is a FULL snapshot, bounded (D4), and the same dict is what the
  SPA persists as ``usage.harness_run`` (D5).

It never raises into the stream: a failed read logs and reports no todos.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# CUSTOM event name (CTR-0009, additive).
HARNESS_PROGRESS_EVENT = "harness_progress"

# Bounds (UDR-0163 D4).
MAX_TODOS = 100
MAX_TITLE_CHARS = 200

# Tools whose result changes the snapshot.
_TODO_TOOL_PREFIX = "todos_"
_MODE_SET_TOOL = "mode_set"

# End states (CTR-0219). "running" is the only non-final state.
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_WAITING = "waiting"
STATE_CAP_REACHED = "cap_reached"
STATE_STOPPED = "stopped"
STATES = (STATE_RUNNING, STATE_COMPLETED, STATE_WAITING, STATE_CAP_REACHED, STATE_STOPPED)


def is_loop_nudge(update: Any) -> bool:
    """True for an update MAF's loop injected between iterations (``role == "user"``).

    Only the harness lane runs the loop, so callers check this on that lane only.
    """
    return str(getattr(update, "role", "") or "").lower() == "user"


def is_progress_tool(name: str | None) -> bool:
    """True for a tool whose result changes the Todo list or the mode."""
    return bool(name) and (str(name).startswith(_TODO_TOOL_PREFIX) or name == _MODE_SET_TOOL)


def _find_provider(agent: Any, provider_type: type) -> Any:
    for provider in getattr(agent, "context_providers", None) or []:
        if isinstance(provider, provider_type):
            return provider
    return None


class HarnessRunTracker:
    """Follows ONE harness turn and builds its ``harness_progress`` values."""

    def __init__(
        self,
        *,
        agent: Any,
        session: Any,
        harness_id: str,
        run_id: str,
        max_iterations: int,
    ) -> None:
        from agent_framework import AgentModeProvider, TodoProvider

        self._agent = agent
        self._session = session
        self.harness_id = harness_id
        self.run_id = run_id
        self.max_iterations = max_iterations
        self._todo_provider = _find_provider(agent, TodoProvider)
        self._mode_provider = _find_provider(agent, AgentModeProvider)
        # The first iteration is running as soon as the turn starts.
        self.iteration = 1
        self._in_nudge = False
        # True once a value was emitted: the end-of-turn value is sent (and the SPA
        # persists it) only for a turn that showed something.
        self.shown = False

    @property
    def enabled(self) -> bool:
        """A harness with ``todo.disabled`` has nothing to show (no todo is invented)."""
        return self._todo_provider is not None

    def observe(self, update: Any) -> bool:
        """Record one stream update; True when it is a loop nudge (not answer text).

        A boundary may carry two user updates (the progress log, then the nudge), so
        the iteration advances on the FIRST user update after non-user output.
        """
        nudge = is_loop_nudge(update)
        if nudge and not self._in_nudge:
            self.iteration += 1
        self._in_nudge = nudge
        return nudge

    async def _read(self) -> tuple[list[dict[str, Any]], bool, str | None]:
        todos: list[dict[str, Any]] = []
        truncated = False
        mode: str | None = None
        if self._todo_provider is not None:
            try:
                items = await self._todo_provider.store.load_items(
                    self._session, source_id=self._todo_provider.source_id
                )
                truncated = len(items) > MAX_TODOS
                todos = [
                    {
                        "id": item.id,
                        "title": str(item.title or "")[:MAX_TITLE_CHARS],
                        "done": bool(item.is_complete),
                    }
                    for item in items[:MAX_TODOS]
                ]
            except Exception:
                logger.warning("Harness progress: reading the Todo list failed", exc_info=True)
        if self._mode_provider is not None:
            try:
                from agent_framework import get_agent_mode

                mode = get_agent_mode(
                    self._session,
                    source_id=self._mode_provider.source_id,
                    default_mode=self._mode_provider.default_mode,
                    available_modes=self._mode_provider.available_modes,
                )
            except Exception:
                logger.warning("Harness progress: reading the mode failed", exc_info=True)
        return todos, truncated, mode

    def _value(self, todos: list[dict[str, Any]], truncated: bool, mode: str | None, state: str) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "harness_id": self.harness_id,
            "mode": mode,
            "iteration": min(self.iteration, self.max_iterations),
            "max_iterations": self.max_iterations,
            "state": state,
            "todos": todos,
            "todos_truncated": truncated,
        }

    async def snapshot(self, *, at_start: bool = False) -> dict[str, Any] | None:
        """A ``running`` value, or None when there is nothing to show yet.

        The Todo list lives in the harness session and outlives a turn, so a finished
        list from an earlier turn must not decorate a plain follow-up question:

        - at the START of a turn, something is shown only when an OPEN todo carries
          over (the agent is resuming work);
        - after a todo / mode tool result or at a loop boundary, it is shown when the
          list is not empty or the loop has advanced.
        """
        if not self.enabled:
            return None
        todos, truncated, mode = await self._read()
        if at_start:
            if not any(not t["done"] for t in todos):
                return None
        elif not self.shown and not todos and self.iteration <= 1:
            return None
        self.shown = True
        return self._value(todos, truncated, mode, STATE_RUNNING)

    async def final(self, *, error: bool) -> dict[str, Any] | None:
        """The end-of-turn value (the one the SPA persists), or None if nothing was shown."""
        if not self.enabled or not self.shown:
            return None
        todos, truncated, mode = await self._read()
        return self._value(todos, truncated, mode, self.end_state(todos, mode, error=error))

    def end_state(self, todos: list[dict[str, Any]], mode: str | None, *, error: bool) -> str:
        """completed / waiting / cap_reached / stopped (CTR-0219)."""
        if error:
            return STATE_STOPPED
        if not any(not t["done"] for t in todos):
            return STATE_COMPLETED
        if (mode or "").lower() == "plan":
            return STATE_WAITING
        if self.iteration >= self.max_iterations:
            return STATE_CAP_REACHED
        return STATE_WAITING


__all__ = [
    "HARNESS_PROGRESS_EVENT",
    "MAX_TITLE_CHARS",
    "MAX_TODOS",
    "STATES",
    "STATE_CAP_REACHED",
    "STATE_COMPLETED",
    "STATE_RUNNING",
    "STATE_STOPPED",
    "STATE_WAITING",
    "HarnessRunTracker",
    "is_loop_nudge",
    "is_progress_tool",
]
