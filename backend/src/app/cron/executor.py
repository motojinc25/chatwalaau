"""Cron Script Execution Harness (CTR-0132 / UDR-0067 D8).

Script-only execution rooted in the coding workspace. A job's script path is
resolved THROUGH the existing CTR-0031 realpath jail (``resolve_safe_path``) and
must stay inside CODING_WORKSPACE_DIR; cwd is the workspace. The interpreter is
resolved from ``job.script.interpreter`` or a per-extension default map (never a
POSIX shebang alone, for Windows portability). Each run honors
CRON_RUN_TIMEOUT_SECONDS and caps captured stdout/stderr at CRON_OUTPUT_MAX_BYTES.

Cron execution REQUIRES CODING_ENABLED (the same opt-in as the coding tools,
FEAT-0004); when false the run is refused (UDR-0067 D7).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

from app.coding.security import resolve_safe_path
from app.core.config import settings
from app.cron.models import RUN_FAILED, RUN_REFUSED, RUN_SUCCESS, RUN_TIMEOUT

# Default interpreter by file extension (UDR-0067 D8). ``.py`` reuses the running
# interpreter so a cron script shares the server's Python.
_DEFAULT_INTERPRETERS: dict[str, list[str]] = {
    ".py": [sys.executable or "python"],
    ".sh": ["sh"],
    ".bash": ["bash"],
    ".ps1": ["pwsh"],
    ".js": ["node"],
    ".mjs": ["node"],
    ".rb": ["ruby"],
}


@dataclass
class RunResult:
    status: str
    exit_code: int | None
    interpreter: str
    stdout: str
    stderr: str


def _resolve_interpreter(script_path: str, interpreter: str) -> list[str] | None:
    """Resolve the interpreter command tokens, or None if unresolved."""
    interpreter = (interpreter or "").strip()
    if interpreter:
        return [interpreter]
    ext = Path(script_path).suffix.lower()
    return _DEFAULT_INTERPRETERS.get(ext)


async def run_script(script: dict) -> RunResult:
    """Execute a job's script under the workspace jail. Never raises.

    Returns a terminal RunResult: success / failed / timeout / refused.
    """
    if not settings.coding_enabled:
        return RunResult(RUN_REFUSED, None, "", "", "Cron execution requires CODING_ENABLED=true.")

    workspace = settings.coding_workspace_dir
    if not workspace:
        return RunResult(RUN_REFUSED, None, "", "", "CODING_WORKSPACE_DIR is not configured.")

    rel_path = str(script.get("path", "")).strip()
    try:
        safe_path = resolve_safe_path(workspace, rel_path)
    except ValueError as exc:
        return RunResult(RUN_REFUSED, None, "", "", str(exc))

    if not Path(safe_path).is_file():
        return RunResult(RUN_REFUSED, None, "", "", f"Script not found in workspace: {rel_path}")

    cmd_prefix = _resolve_interpreter(rel_path, str(script.get("interpreter", "")))
    if cmd_prefix is None:
        return RunResult(
            RUN_REFUSED,
            None,
            "",
            "",
            f"No interpreter for '{rel_path}'. Set script.interpreter explicitly.",
        )

    args = [str(a) for a in (script.get("args") or [])]
    cmd = [*cmd_prefix, safe_path, *args]
    interpreter_label = " ".join(cmd_prefix)
    timeout = max(1, int(settings.cron_run_timeout_seconds))
    workspace_real = resolve_safe_path(workspace, "")

    # Run via subprocess.run in a worker thread (NOT asyncio.create_subprocess_exec):
    # under uvicorn the running loop is a SelectorEventLoop, which raises
    # NotImplementedError for subprocess transports on Windows. A blocking
    # subprocess.run off the event loop is uniformly cross-platform and mirrors the
    # coding tools (FEAT-0004, bash_execute).
    return await asyncio.to_thread(_run_sync, cmd, workspace_real, interpreter_label, timeout)


def _run_sync(cmd: list[str], cwd: str, interpreter_label: str, timeout: int) -> RunResult:
    """Synchronous subprocess execution (script-only, shell=False). Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return RunResult(
            RUN_TIMEOUT,
            None,
            interpreter_label,
            (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            f"{partial}\nScript timed out after {timeout}s.".strip(),
        )
    except (OSError, ValueError) as exc:
        return RunResult(RUN_FAILED, None, interpreter_label, "", f"Failed to start script: {exc}")

    status = RUN_SUCCESS if proc.returncode == 0 else RUN_FAILED
    return RunResult(status, proc.returncode, interpreter_label, proc.stdout or "", proc.stderr or "")


__all__ = ["RunResult", "run_script"]
