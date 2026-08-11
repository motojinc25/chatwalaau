"""Workspace shell executor for harness agents (CTR-0193, PRP-0135, UDR-0119 D7).

WHY THIS EXISTS -- measured, not inferred:

MAF's ``LocalShellTool`` executes commands through ``asyncio`` subprocess
transports. Under uvicorn the running loop is a ``SelectorEventLoop``
(``uvicorn/loops/asyncio.py`` returns ``asyncio.SelectorEventLoop``), and on
Windows that loop raises ``NotImplementedError`` from
``base_events._make_subprocess_transport`` -- verified directly:

    SelectorEventLoop  -> NotImplementedError
    ProactorEventLoop  -> subprocess OK

The failure is not conditional on what the model does: ``ShellEnvironmentProvider``
probes the shell in ``before_run``, so EVERY harness turn died before the first
token whenever a shell was attached.

ChatWalaʻau already owns a cross-platform answer to exactly this constraint --
``app.cron.executor`` runs scripts with ``subprocess.run`` in a worker thread
rather than ``asyncio.create_subprocess_exec``, and the coding tool
``bash_execute`` (CTR-0031) does the same. This module applies that same
technique to the harness shell, so the shell works uniformly on every platform
instead of only where the event loop happens to cooperate.

The class SUBCLASSES ``LocalShellTool`` so the model-facing surface stays
upstream's: ``as_function()`` supplies the ``kind="shell"`` marker that
``client.get_shell_tool()`` recognizes, and the ``always_require`` approval mode
that routes every command through the FEAT-0028 approval card (UDR-0119 D6).
Only the three coroutines that touch asyncio subprocesses are overridden, and
they read this class's OWN copies of the configuration -- with ONE enumerated
private-attribute traversal (``_stateless_argv``, see below), registered under
the UDR-0110 D2 residue discipline.

Semantics equal upstream's ``mode="stateless"``: each command runs in a fresh
subprocess anchored at the workspace, so ``cd`` / ``export`` do not carry across
calls. That is the honest trade for a uniform implementation, and it matches how
``bash_execute`` has always behaved here.

The command is handed to the SHELL UPSTREAM RESOLVED (``_stateless_argv``), not
to ``shell=True``. This matters: ``ShellEnvironmentProvider`` tells the model
which shell family it is in from the PLATFORM alone (Windows -> PowerShell), so
running the model's PowerShell through ``shell=True`` -- which is ``cmd.exe`` on
Windows -- would execute ``Get-ChildItem`` / ``$env:X`` in the wrong interpreter.
Reusing upstream's own resolution also preserves the ``AGENT_FRAMEWORK_SHELL``
override and the trailing ``-Command`` / ``-c`` flag.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from agent_framework_tools.shell import (
    LocalShellTool,
    ShellCommandError,
    ShellPolicy,
    ShellRequest,
    ShellResult,
)

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# Per-command wall clock. Long builds/tests are the point of a harness agent, so
# this is generous compared with the coding tool's default; the loop cap
# (UDR-0119 D4) bounds how many of these a run can accumulate.
DEFAULT_TIMEOUT_SECONDS = 300.0

# Captured output ceiling per stream, mirroring the upstream default.
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Return ``(text, truncated)`` clipped to ``limit`` characters."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class WorkspaceShellTool(LocalShellTool):
    """A ``LocalShellTool`` whose execution path is thread-based, not asyncio.

    Constructed with the same keywords as the upstream tool; only ``workdir``,
    ``timeout``, ``max_output_bytes``, ``policy`` and ``env`` participate in the
    overridden execution path.
    """

    def __init__(
        self,
        *,
        workdir: str | Path,
        timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        policy: ShellPolicy | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        # Stateless upstream mode: there is no persistent session to own, so the
        # single-session ownership caveat in LocalShellTool's docstring does not
        # apply and eviction has nothing to tear down.
        super().__init__(
            mode="stateless",
            workdir=workdir,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            policy=policy,
            env=env,
            **kwargs,
        )
        # OWN copies: the overridden run() never reads an upstream private attr.
        self._cw_workdir = str(workdir)
        self._cw_timeout = timeout
        self._cw_max_output_bytes = max_output_bytes
        self._cw_policy = policy or ShellPolicy()
        self._cw_env = env

    async def start(self) -> None:
        """No-op: there is no long-lived shell process to start."""
        return None

    async def close(self) -> None:
        """No-op: nothing is held open between commands."""
        return None

    async def __aenter__(self) -> WorkspaceShellTool:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def run(self, command: str, *, timeout: float | None = None) -> ShellResult:
        """Execute ``command`` in the workspace and return its ``ShellResult``.

        Policy is evaluated exactly as upstream does (deny -> ``ShellCommandError``);
        approval is NOT handled here -- the framework applies it around the tool
        produced by ``as_function()`` (UDR-0119 D6).
        """
        decision = self._cw_policy.evaluate(ShellRequest(command=command, workdir=self._cw_workdir))
        if decision.decision == "deny":
            raise ShellCommandError(f"Command rejected by policy: {decision.reason}")

        effective_timeout = self._cw_timeout if timeout is None else timeout
        return await asyncio.to_thread(self._run_sync, command, effective_timeout)

    def _resolved_argv(self, command: str) -> list[str]:
        """Return the argv to execute, using UPSTREAM's resolved stateless shell.

        ``_stateless_argv`` is computed by ``LocalShellTool.__init__`` (which this
        class calls) via ``resolve_shell(..., interactive=False)``; it honors the
        ``AGENT_FRAMEWORK_SHELL`` override and is guaranteed to end with the
        ``-Command`` / ``-c`` flag, so the command string appends verbatim. Reusing
        it keeps the interpreter identical to the shell family
        ``ShellEnvironmentProvider`` advertises to the model. A defensive fallback
        keeps the tool usable if upstream ever renames the attribute.
        """
        argv = getattr(self, "_stateless_argv", None)
        if isinstance(argv, list) and argv:
            return [*argv, command]
        logger.warning("Upstream stateless shell argv unavailable; falling back to the platform shell.")
        return (
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
            if _IS_WINDOWS
            else ["/bin/sh", "-c", command]
        )

    def _run_sync(self, command: str, timeout: float | None) -> ShellResult:
        """Blocking execution off the event loop (the cron / bash_execute technique)."""
        started = time.monotonic()
        workdir = self._cw_workdir if Path(self._cw_workdir).is_dir() else None
        try:
            completed = subprocess.run(
                self._resolved_argv(command),
                capture_output=True,
                # Decode as UTF-8 with replacement rather than the process locale.
                # On Windows that default is cp1252 and raises UnicodeDecodeError on
                # the reader thread for any unmappable byte, which would surface as a
                # hard tool failure (the CTR-0031 lesson).
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=workdir,
                env=self._cw_env,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return ShellResult(
                stdout=stdout,
                stderr=(stderr + f"\nCommand timed out after {timeout}s.").strip(),
                exit_code=124,
                duration_ms=elapsed,
                timed_out=True,
            )
        except (OSError, ValueError) as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return ShellResult(
                stdout="",
                stderr=f"Failed to start command: {exc}",
                exit_code=127,
                duration_ms=elapsed,
            )

        elapsed = int((time.monotonic() - started) * 1000)
        stdout, out_cut = _truncate(completed.stdout or "", self._cw_max_output_bytes)
        stderr, err_cut = _truncate(completed.stderr or "", self._cw_max_output_bytes)
        return ShellResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=completed.returncode,
            duration_ms=elapsed,
            truncated=out_cut or err_cut,
        )


__all__ = ["DEFAULT_MAX_OUTPUT_BYTES", "DEFAULT_TIMEOUT_SECONDS", "WorkspaceShellTool"]
