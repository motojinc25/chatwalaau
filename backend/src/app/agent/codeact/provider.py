"""CodeAct compute sandbox provider (CTR-0239, PRP-0193, UDR-0175).

``ChatWalaauCodeActProvider`` is MAF's ``MontyCodeActProvider`` with three things
ChatWalaʻau owns:

* the GATES (D5) -- evaluated in ``before_run`` on every run, so the App Setting
  applies at once on every lane, including cached harness runtimes that an App
  Settings apply does not rebuild. Closed => the context is left untouched and the
  model request is byte-identical to a build without CodeAct;
* the WORDS (D8) -- ChatWalaʻau's ``<tool-guide name="code_act">`` block and tool
  description instead of MAF's "one primary tool" text;
* the EXECUTION (D6) -- ``OffLoopExecuteCodeTool`` runs the snippet through
  :class:`~app.agent.codeact.bridge.OffLoopCodeBridge`, never on the event loop.

Phase 1 registers NO sandbox tools, no ``workspace_root`` and no ``file_mounts``
(D2): ``execute_code`` is a pure compute sandbox. Registering a tool later is its own
PRP under the D3 rule.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_framework import Content
from agent_framework_monty import MontyCodeActProvider, MontyExecuteCodeTool

from app.agent.codeact.bridge import OffLoopCodeBridge
from app.agent.codeact.guidance import EXECUTE_CODE_DESCRIPTION, codeact_instructions
from app.core.config import settings

logger = logging.getLogger(__name__)

CODEACT_SOURCE_ID = "codeact"
EXECUTE_CODE_TOOL_NAME = "execute_code"

_BYTES_PER_MB = 1024 * 1024


def codeact_resource_limits() -> dict[str, Any]:
    """Monty ``ResourceLimits`` from the App Settings bounds, read now (D7)."""
    return {
        "max_duration_secs": float(settings.codeact_max_duration_secs),
        "max_memory": int(settings.codeact_max_memory_mb) * _BYTES_PER_MB,
    }


def codeact_runtime_open() -> bool:
    """The per-run gates (D5): the App Setting is on and DEMO_MODE is off."""
    from app.demo import is_demo_mode

    return bool(settings.codeact_enabled) and not is_demo_mode()


class OffLoopExecuteCodeTool(MontyExecuteCodeTool):
    """``execute_code`` with ChatWalaʻau's description, run off the event loop.

    Overrides the private ``_run_code`` -- the ONE private MAF name ChatWalaʻau relies
    on (D10; pinned by an invariant canary). ``FunctionTool.__init__`` binds
    ``func=self._run_code``, so the override is what the tool invokes.
    """

    @property
    def description(self) -> str:
        return EXECUTE_CODE_DESCRIPTION

    @description.setter
    def description(self, value: str) -> None:
        # FunctionTool.__init__ assigns MAF's default; the property above wins.
        self.__dict__["description"] = value

    async def _run_code(self, *, code: str) -> list[Content]:
        try:
            result = await OffLoopCodeBridge(resource_limits=self.resource_limits).run(code)
        except Exception as exc:
            return [Content.from_error(message="Execution error", error_details=f"{type(exc).__name__}: {exc}")]
        return _execution_contents(result)


def _execution_contents(result: dict[str, Any]) -> list[Content]:
    """The MAF result shape: stdout text, then the final expression as JSON text."""
    import json

    stdout = str(result.get("stdout") or "").replace("\r\n", "\n")
    truncated = bool(result.get("truncated"))
    output = result.get("output")
    contents: list[Content] = []
    if stdout or truncated:
        contents.append(Content.from_text(f"{stdout}\n\n[stdout truncated]" if truncated else stdout))
    if output is not None:
        contents.append(Content.from_text(json.dumps(output, ensure_ascii=False)))
    if not contents:
        contents.append(Content.from_text("Code executed successfully without output."))
    return contents


class ChatWalaauCodeActProvider(MontyCodeActProvider):
    """The shared CodeAct context provider (one instance per build; stateless per run)."""

    def __init__(self) -> None:
        # D2: no tools, no workspace_root, no file_mounts.
        super().__init__(CODEACT_SOURCE_ID)

    def create_run_tool(self) -> OffLoopExecuteCodeTool:
        """A run-scoped ``execute_code`` carrying the limits in force right now."""
        return OffLoopExecuteCodeTool(resource_limits=codeact_resource_limits())

    async def before_run(
        self,
        *,
        agent: Any,
        session: Any,
        context: Any,
        state: dict[str, Any],
    ) -> None:
        """Inject the tool AND its guidance together, or neither (D5).

        The parent is deliberately not called: it would inject MAF's instructions (D8).
        """
        if not codeact_runtime_open():
            return
        context.extend_instructions(self.source_id, codeact_instructions())
        context.extend_tools(self.source_id, [self.create_run_tool()])


def is_codeact_provider(obj: Any) -> bool:
    """True when ``obj`` is the CodeAct provider (matched by type, never by name)."""
    return isinstance(obj, MontyCodeActProvider)


def create_codeact_provider() -> ChatWalaauCodeActProvider | None:
    """Build the provider, or ``None`` when the package cannot load (never raises).

    Attached unconditionally at the two chokepoints (D4); whether it DOES anything is
    decided per run (D5), so turning the App Setting on needs no rebuild.
    """
    try:
        return ChatWalaauCodeActProvider()
    except Exception:
        logger.warning("CodeAct provider unavailable; agents run without execute_code.", exc_info=True)
        return None


__all__ = [
    "CODEACT_SOURCE_ID",
    "EXECUTE_CODE_TOOL_NAME",
    "ChatWalaauCodeActProvider",
    "OffLoopExecuteCodeTool",
    "codeact_resource_limits",
    "codeact_runtime_open",
    "create_codeact_provider",
    "is_codeact_provider",
]
