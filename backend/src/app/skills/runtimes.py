"""Which script interpreters this server has (PRP-0266 C7, UDR-0247 D7).

``run_skill_script`` (CTR-0043) picks the interpreter by extension: ``.py`` runs on
the backend's own Python, ``.sh`` / ``.bash`` on ``bash`` and ``.js`` / ``.mjs`` /
``.cjs`` on ``node`` from ``PATH``. The container image ships Python and Node.js; a
Desktop or PyPI install has whatever the machine has. This module answers "what is
here" for the Skills screen and for the runner's clear error, so a missing
interpreter is said in words instead of a raw ``[Errno 2]``.

Version probes run at most once per ``_TTL_SECONDS`` and never block for long.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

from app.core.config import settings

_TTL_SECONDS = 60.0
_PROBE_TIMEOUT = 5.0

_lock = threading.Lock()
_cache: tuple[float, dict[str, dict[str, Any]]] | None = None

# Interpreter command -> the human name the messages use.
INTERPRETER_NAMES = {"node": "Node.js", "bash": "bash"}


def _probe(command: str) -> dict[str, Any]:
    path = shutil.which(command)
    if not path:
        return {"found": False, "version": "", "path": ""}
    version = ""
    try:
        result = subprocess.run(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT,
        )
        lines = (result.stdout or result.stderr or "").strip().splitlines()
        version = lines[0].strip() if lines else ""
    except (OSError, subprocess.SubprocessError):
        version = ""
    return {"found": True, "version": version, "path": path}


def detect_runtimes(*, refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Return ``{"python": {...}, "node": {...}, "bash": {...}}`` (found, version, path)."""
    global _cache
    now = time.monotonic()
    with _lock:
        if not refresh and _cache is not None and now - _cache[0] < _TTL_SECONDS:
            return _cache[1]
    report = {
        "python": {"found": True, "version": platform.python_version(), "path": sys.executable},
        "node": _probe("node"),
        "bash": _probe("bash"),
    }
    with _lock:
        _cache = (now, report)
    return report


def runtime_report() -> dict[str, Any]:
    """The ``runtimes`` block of the CTR-0205 catalog view."""
    return {**detect_runtimes(), "coding_enabled": bool(settings.coding_enabled)}


def missing_interpreter_message(command: str, script_name: str) -> str | None:
    """The runner's message when ``command`` is not on PATH, else None."""
    if command not in INTERPRETER_NAMES or shutil.which(command):
        return None
    name = INTERPRETER_NAMES[command]
    if command == "node":
        return (
            f"Error: {name} is not installed on this server, so the script {script_name} cannot run. "
            "Install Node.js or use a skill without .js scripts."
        )
    return (
        f"Error: {name} is not installed on this server, so the script {script_name} cannot run. "
        "Install bash or use a skill without .sh scripts."
    )


__all__ = ["INTERPRETER_NAMES", "detect_runtimes", "missing_interpreter_message", "runtime_report"]
