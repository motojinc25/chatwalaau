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

from agent_framework import (
    DeduplicatingSkillsSource,
    FileSkillsSource,
    FilteringSkillsSource,
    SkillsProvider,
)

from app.core.config import settings
from app.skills.loaded import set_loaded_skills
from app.skills.overrides import get_skills_override_store

logger = logging.getLogger(__name__)

_SKILL_FILE_NAME = "SKILL.md"
# Mirror MAF's FileSkillsSource discovery depth (agent_framework MAX_SEARCH_DEPTH).
_MAX_SEARCH_DEPTH = 2

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
        FileSkillsSource(skills_path, script_runner=_skill_script_runner)
    )


def create_skills_provider() -> SkillsProvider | None:
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
    disabled = frozenset(get_skills_override_store().disabled_names())
    source = build_skill_source()
    if disabled:
        source = FilteringSkillsSource(source, predicate=lambda s: s.frontmatter.name not in disabled)

    provider = SkillsProvider(
        source,
        require_script_approval=settings.tool_approval_mode != "skip",
    )
    logger.info(
        "SkillsProvider created (skills_dir=%s, disabled_skills=%d [%s], script_execution=%s, script_approval=%s)",
        skills_path,
        len(disabled),
        ", ".join(sorted(disabled)) if disabled else "none",
        "on" if settings.coding_enabled else "off (CODING_ENABLED=false)",
        settings.tool_approval_mode != "skip",
    )
    return provider
