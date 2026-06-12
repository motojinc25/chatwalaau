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
    - Each run is approval-gated through the existing Tool Approval flow
      (FEAT-0028) by setting ``require_script_approval`` unless
      ``TOOL_APPROVAL_MODE=skip`` -- the SPA shows the same approval card as
      bash_execute.
    - The subprocess reuses CODING_BASH_TIMEOUT and CODING_MAX_OUTPUT_CHARS for the
      timeout and output cap, runs in the skill's own directory, and decodes output
      as UTF-8 (errors replaced) so non-ASCII script output is Windows-safe.
"""

import asyncio
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from agent_framework import SkillsProvider

from app.core.config import settings

logger = logging.getLogger(__name__)

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


def create_skills_provider() -> SkillsProvider | None:
    """Create SkillsProvider if SKILLS_DIR exists and is a directory.

    Returns:
        SkillsProvider instance if skills directory exists, None otherwise.
    """
    skills_path = Path(settings.skills_dir)

    if not skills_path.is_dir():
        logger.info("Skills directory not found: %s (skipping SkillsProvider)", skills_path)
        return None

    # MAF moved file-based skill discovery to the SkillsProvider.from_paths()
    # factory; the constructor now takes Skill instances / sources, so the old
    # SkillsProvider(skill_paths=...) call raises TypeError on current
    # agent-framework builds. We also supply a script runner so skills that ship
    # scripts (e.g. pptx) actually run instead of raising "requires a runner"
    # (which previously made the agent flail until the approval loop aborted).
    provider = SkillsProvider.from_paths(
        skills_path,
        script_runner=_skill_script_runner,
        require_script_approval=settings.tool_approval_mode != "skip",
    )
    logger.info(
        "SkillsProvider created (skills_dir=%s, script_execution=%s, script_approval=%s)",
        skills_path,
        "on" if settings.coding_enabled else "off (CODING_ENABLED=false)",
        settings.tool_approval_mode != "skip",
    )
    return provider
