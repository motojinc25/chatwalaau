"""Agent Skills provider factory (CTR-0043, PRP-0024).

Creates a MAF SkillsProvider for file-based Agent Skills discovery.
SkillsProvider implements progressive disclosure:
  1. Advertise -- skill names + descriptions injected into system prompt (~100 tokens/skill)
  2. Load -- full SKILL.md body returned via load_skill tool
  3. Read resources -- supplementary files returned via read_skill_resource tool
  4. Run scripts -- executable skill scripts (e.g. pptx's build script) run via the
     run_skill_script tool, executed by the script runner below.

Skills are discovered from SKILLS_DIR (default: ".skills"), searching up to 2 levels deep.

Script execution (v0.75.x defect fix):
  Current agent-framework builds advertise a ``run_skill_script`` tool and instruct
  the agent to use it whenever a discovered skill ships scripts (e.g. the document
  ``pptx`` skill). MAF does NOT ship a script runner -- without one, every script
  call raises ``ValueError: ... requires a runner``, which the agent retries until
  the tool-approval loop aborts ("exceeded 16 rounds"). We supply a subprocess
  runner that reuses the existing local-code-execution posture:
    - Execution is gated by ``CODING_ENABLED`` (the operator's existing opt-in for
      running local code, CTR-0031). When off, the runner declines with a clear,
      non-retryable message instead of raising, so the agent stops flailing.
    - The subprocess reuses CODING_BASH_TIMEOUT and CODING_MAX_OUTPUT_CHARS for the
      timeout and output cap, runs in the skill's own directory, and decodes output
      as UTF-8 (errors replaced) so non-ASCII script output is Windows-safe.

Approval model (PRP-0108, UDR-0086 D2 -- MAF 1.10):
  MAF 1.10 registers all three skill tools (``load_skill``, ``read_skill_resource``,
  ``run_skill_script``) with ``approval_mode="always_require"`` unconditionally; the
  former ``require_script_approval`` constructor parameter no longer exists. The
  EXISTING ``TOOL_APPROVAL_MODE`` semantics are preserved by attaching a
  ``ToolApprovalMiddleware`` (built by ``create_skills_approval_middleware()``) at
  the agent factory chokepoint: read-only tools are auto-approved before they ever
  surface, while ``run_skill_script`` keeps flowing through the existing Tool
  Approval flow (FEAT-0028) unless ``TOOL_APPROVAL_MODE=skip`` auto-approves it too.

Discovery (PRP-0108, UDR-0086 D3 -- MAF 1.10):
  ``FileSkillsSource`` no longer whitelists ``references/`` / ``assets/`` /
  ``scripts/`` directories; it depth-scans (default depth 2) with extension
  filters. The default script filter is ``.py`` ONLY, so we pass an explicit
  ``script_extensions`` covering every interpreter ``_command_for`` dispatches --
  otherwise a shipped ``.sh`` / ``.js`` skill script would silently vanish.
"""

import asyncio
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from agent_framework import (
    DeduplicatingSkillsSource,
    FileSkillsSource,
    FilteringSkillsSource,
    SkillsProvider,
    ToolApprovalMiddleware,
)

from app.core.config import settings
from app.skills.loaded import set_loaded_skills
from app.skills.overrides import get_skills_override_store

logger = logging.getLogger(__name__)

_SKILL_FILE_NAME = "SKILL.md"
# Mirror MAF's FileSkillsSource discovery depth (agent_framework default search_depth).
_MAX_SEARCH_DEPTH = 2

# Script extensions handed to FileSkillsSource (UDR-0086 D3). MAF 1.10's default
# is (".py",) only; this list MUST cover every interpreter _command_for()
# dispatches so a shipped .sh / .js skill script keeps being discovered.
_SCRIPT_EXTENSIONS = (".py", ".sh", ".bash", ".js", ".mjs", ".cjs")

_DISABLED_MESSAGE = (
    "Skill script execution is disabled in this deployment (CODING_ENABLED is off). "
    "Do NOT call run_skill_script again; either complete the task using your other "
    "tools, or tell the user to set CODING_ENABLED=true to run skill scripts."
)


def _script_args(args: dict[str, Any] | list[str] | None) -> list[str]:
    """Normalize the agent-supplied args into a subprocess argv tail."""
    if args is None:
        return []
    if isinstance(args, list):
        return [str(a) for a in args]
    if isinstance(args, dict):
        out: list[str] = []
        for key, value in args.items():
            out += [f"--{key}", str(value)]
        return out
    return [str(args)]


def _command_for(script_path: Path) -> list[str]:
    """Choose the interpreter for a script by extension (Python is the default)."""
    ext = script_path.suffix.lower()
    if ext == ".py":
        return [sys.executable, str(script_path)]
    if ext in (".sh", ".bash"):
        return ["bash", str(script_path)]
    if ext in (".js", ".mjs", ".cjs"):
        return ["node", str(script_path)]
    # Fall back to direct execution (shebang / executable bit).
    return [str(script_path)]


def _run_skill_script_sync(skill: Any, script: Any, args: dict[str, Any] | list[str] | None) -> str:
    """Execute a file-based skill script in its skill directory (gated by CODING_ENABLED)."""
    if not settings.coding_enabled:
        return _DISABLED_MESSAGE

    script_path = Path(getattr(script, "full_path", "") or "")
    if not script_path.is_file():
        return f"Error: skill script not found: {script_path}"
    skill_dir = Path(getattr(skill, "path", "") or script_path.parent)

    cmd = _command_for(script_path) + _script_args(args)
    timeout = settings.coding_bash_timeout
    max_output = settings.coding_max_output_chars
    # Force UTF-8 in the child so scripts that print non-ASCII (very common in
    # document skills) do not crash with a cp1252 UnicodeEncodeError on Windows.
    child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(skill_dir),
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        return f"Error: skill script timed out after {timeout} seconds: {getattr(script, 'name', script_path.name)}"
    except OSError as exc:
        return f"Error executing skill script: {exc}"

    output = (result.stdout or "") + (result.stderr or "")
    if len(output) > max_output:
        output = output[:max_output] + f"\n... (output truncated at {max_output} characters)"
    return f"Exit code: {result.returncode}\n{output}"


async def _skill_script_runner(skill: Any, script: Any, args: dict[str, Any] | list[str] | None = None) -> str:
    """SkillScriptRunner: run the script off the event loop, error-isolated."""
    return await asyncio.to_thread(_run_skill_script_sync, skill, script, args)


def discover_skill_names(root: Path | None = None) -> set[str]:
    """Return the discovered skill names (SKILL.md dir basenames), depth <= 2.

    Mirrors MAF ``FileSkillsSource`` discovery (basename == frontmatter name) so the
    set matches what ``get_skills()`` would load. Used to (a) snapshot the live build
    set for the ``loaded`` flag (CTR-0123, UDR-0068 D4) and (b) prune stale override
    entries on Reload. A synchronous disk walk so it can run inside the sync agent
    build chokepoint.
    """
    base = Path(settings.skills_dir) if root is None else root
    names: set[str] = set()

    def _search(directory: Path, depth: int) -> None:
        if (directory / _SKILL_FILE_NAME).is_file():
            names.add(directory.name)
        if depth >= _MAX_SEARCH_DEPTH:
            return
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                _search(entry, depth + 1)

    if base.is_dir():
        _search(base, 0)
    return names


def build_skill_source() -> DeduplicatingSkillsSource:
    """Build the UNFILTERED, deduplicated file-based skill source.

    Mirrors what ``SkillsProvider.from_paths()`` constructs internally
    (``DeduplicatingSkillsSource(FileSkillsSource(...))``) so that, with an empty
    override store, ``create_skills_provider()`` produces a byte-for-byte
    equivalent provider. Reused by ``app.skills.inventory`` so the inventory's
    skill NAMES match exactly what the runtime FilteringSkillsSource predicate
    gates against (PRP-0087, UDR-0065 D2/D3).
    """
    skills_path = Path(settings.skills_dir)
    return DeduplicatingSkillsSource(
        FileSkillsSource(
            skills_path,
            script_runner=_skill_script_runner,
            script_extensions=_SCRIPT_EXTENSIONS,
        )
    )


def create_skills_provider(allowlist_names: set[str] | None = None) -> SkillsProvider | None:
    """Create SkillsProvider if SKILLS_DIR exists and is a directory.

    PRP-0087 / UDR-0065: the active Skills set is gated at runtime by the in-memory
    override store. The file source is wrapped in MAF ``FilteringSkillsSource`` with
    the predicate "skill name not in the disabled set", so a disabled skill is
    dropped from ``get_skills()`` and therefore leaves BOTH the advertise block AND
    the load_skill / read_skill_resource / run_skill_script closures. The disabled
    set is SNAPSHOTTED here, so a rebuild is the deterministic apply boundary
    (UDR-0065 D2); the agent rebuild (CTR-0070) re-runs this factory. With an empty
    override store this is byte-for-byte the pre-PRP-0087 behaviour.

    When every discovered skill is disabled the filtered source yields no skills,
    so MAF injects no advertise text and no skill tools at all -- observationally
    identical to omitting the provider (UDR-0065 D3).

    PRP-0117 / UDR-0100 D2/D3: ``allowlist_names`` layers the active declarative
    agent's per-agent Skills subset on top of the override store. ``None`` means
    "inherit all" (byte-for-byte). A set (possibly empty) means "keep ONLY these
    skill names"; combined with the disabled set, the predicate becomes
    "enabled AND selected", so an allow-list with no skill entries yields no skills.

    Returns:
        SkillsProvider instance if skills directory exists, None otherwise.
    """
    skills_path = Path(settings.skills_dir)

    # Refresh the live-build snapshot every build so the inventory's ``loaded`` flag
    # (CTR-0123, UDR-0068 D4) reflects exactly what this build discovered on disk.
    # A skill added after this point is shown but disabled until the next Reload.
    set_loaded_skills(discover_skill_names(skills_path))

    if not skills_path.is_dir():
        logger.info("Skills directory not found: %s (skipping SkillsProvider)", skills_path)
        return None

    # We construct the provider manually (rather than SkillsProvider.from_paths)
    # so the override filter can be injected over the same deduplicated file
    # source. A script runner is supplied so skills that ship scripts (e.g. pptx)
    # actually run instead of raising "requires a runner".
    #
    # PRP-0108 / UDR-0086 D2: MAF 1.10 removed ``require_script_approval`` -- the
    # three skill tools are approval-required unconditionally. The TOOL_APPROVAL_MODE
    # mapping now lives in the ToolApprovalMiddleware attached by the agent factory
    # (create_skills_approval_middleware below), not in this constructor.
    disabled = frozenset(get_skills_override_store().disabled_names())
    source = build_skill_source()
    if disabled or allowlist_names is not None:
        allow = None if allowlist_names is None else frozenset(allowlist_names)

        def _predicate(s: Any) -> bool:
            name = s.frontmatter.name
            if name in disabled:
                return False
            return allow is None or name in allow

        source = FilteringSkillsSource(source, predicate=_predicate)

    provider = SkillsProvider(source)
    logger.info(
        "SkillsProvider created (skills_dir=%s, disabled_skills=%d [%s], allowlist=%s, "
        "script_execution=%s, script_approval=%s)",
        skills_path,
        len(disabled),
        ", ".join(sorted(disabled)) if disabled else "none",
        "all" if allowlist_names is None else f"{len(allowlist_names)} selected",
        "on" if settings.coding_enabled else "off (CODING_ENABLED=false)",
        settings.tool_approval_mode != "skip",
    )
    return provider


class SessionTolerantToolApprovalMiddleware(ToolApprovalMiddleware):
    """ToolApprovalMiddleware constrained to AUTO-APPROVAL only (UDR-0086 D2).

    Three deviations from the MAF base class, each protecting an existing
    ChatWalaʻau approval behavior:

    1. Session-tolerant: the base raises ``RuntimeError`` on a session-less run,
       and DevUI can run its entities session-less (a raw API call without a
       conversation id). Such runs pass through untouched -- the approval
       request surfaces to the consumer instead of crashing, the same fail-open
       shape as an unmatched rule.
    2. Inbound passthrough: the base EXTRACTS ``function_approval_response``
       contents from inbound messages and re-injects them PREPENDED before the
       message list. That inverts ChatWalaʻau's approval-resume replay
       (``[assistant(function_call), user(approval_response)]`` becomes
       ``[function_result, assistant(function_call)]`` after function
       execution), and the OpenAI Responses API rejects a function_call whose
       output precedes it: ``400 No tool output found for function call ...``
       (v0.102.0 field defect, first coding-tool approval after the upgrade).
       Inbound messages are therefore passed through untouched -- the
       FEAT-0028 replay owns approval responses, not this middleware.
    3. All-or-nothing, no queueing: the base auto-approves the matching subset
       of a round and hides all but the first unresolved request in SESSION
       state. ChatWalaʻau renders PARALLEL approval cards (ToolApprovalList)
       and builds a fresh ``AgentSession`` per HTTP request, so a queued
       request would be lost forever -- and hiding an auto-approved request
       whose function_call already streamed desynchronizes the FEAT-0028
       accumulator. A round is auto-approved only when EVERY pending request
       matches a rule; otherwise the round is left completely untouched.

    What remains of the base behavior is exactly the intended scope: matching
    ``function_approval_request`` contents are auto-approved in-stream (the
    collected responses re-enter via the base loop) and everything else flows
    to the existing FEAT-0028 surfaces unchanged.
    """

    async def process(self, context: Any, call_next: Any) -> None:
        if getattr(context, "session", None) is None:
            await call_next()
            return
        await super().process(context, call_next)

    def _prepare_inbound_messages(self, messages: Any, state: Any) -> list[Any]:
        # Deviation 2: never hijack inbound approval responses (see class doc).
        return list(messages)

    async def _process_outbound_messages(self, messages: Any, state: Any) -> bool:
        # Deviation 3: ALL-OR-NOTHING auto-approval, and NEVER queue (see class
        # doc). The round is auto-approved only when EVERY pending request
        # matches a rule; a MIXED round (e.g. load_skill + file_write in one
        # model turn) is left completely untouched so every request surfaces to
        # the operator. Auto-approving just the matching subset would hide a
        # request whose function_call content ALREADY streamed to the consumer:
        # the FEAT-0028 accumulator would then replay a bare function_call
        # while this middleware holds the response in session state -- either a
        # Responses API 400 (unpaired call) or a double execution on resume.
        # Standing session rules (``state.rules``) cannot accumulate in
        # per-request sessions, so only the constructor rules are consulted.
        approval_requests = [
            content
            for message in messages
            for content in message.contents
            if content.type == "function_approval_request"
        ]
        if not approval_requests:
            return False

        for request in approval_requests:
            if not await self._matches_auto_rule(request):
                return False  # mixed or fully-human round: touch nothing

        for request in approval_requests:
            state.collected_approval_responses.append(request.to_function_approval_response(approved=True))
        self._remove_approval_requests(messages, {id(r) for r in approval_requests})
        return True


def create_skills_approval_middleware(*, auto_approve_all: bool) -> ToolApprovalMiddleware:
    """Build the skills auto-approval middleware (PRP-0108, UDR-0086 D2).

    Maps the EXISTING ``TOOL_APPROVAL_MODE`` semantics onto MAF 1.10's
    approval-by-default skill tools, preserving v0.101.0 operator-visible
    behavior byte-for-byte:

    - ``auto_approve_all=False`` (TOOL_APPROVAL_MODE != "skip"): auto-approve
      only the read-only tools (``load_skill`` / ``read_skill_resource``);
      ``run_skill_script`` keeps raising the FEAT-0028 approval card.
    - ``auto_approve_all=True`` (skip mode, or the DevUI agent whose loop has
      no human-in-the-loop UI per UDR-0043 D5): auto-approve every skill tool.

    The rules are the SkillsProvider-shipped static callbacks, so they stay
    scoped to this provider's local tools (hosted ``server_label`` calls are
    never auto-approved) and track upstream tool renames automatically.
    """
    rule = (
        SkillsProvider.all_tools_auto_approval_rule
        if auto_approve_all
        else SkillsProvider.read_only_tools_auto_approval_rule
    )
    return SessionTolerantToolApprovalMiddleware(auto_approval_rules=[rule])
