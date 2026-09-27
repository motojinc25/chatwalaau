"""Where Computer Use exists: generic host facts, never Desktop detection (UDR-0171 D1).

The tools are OFFERED only when every host fact H1-H6 holds. The Desktop app and a
local ``dev:full`` on Windows satisfy them by construction; a server deployment fails
H3, H5 or H6. Nothing here names the Desktop, so UDR-0151 D1 / invariant I1 hold.

========  =============================================  ==========================
fact      condition                                      reason when false
========  =============================================  ==========================
H1        ``COMPUTER_USE_ENABLED=true`` (.env gate)      ``disabled``
H2        not ``DEMO_MODE``                              ``demo_mode``
H3        ``sys.platform == "win32"``                    ``unsupported_platform``
H4        the desktop provider executable resolves       ``missing_dependency``
H5        an interactive input desktop, not session 0    ``no_interactive_desktop``
H6        the backend binds loopback                     ``not_loopback``
========  =============================================  ==========================

H7 (the run came from a loopback peer) and H8 (the desktop is not locked) are checked
per CALL by the tools.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
import logging
import sys

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Availability:
    offered: bool
    reason: str | None = None


@functools.cache
def _dependencies_present() -> bool:
    """H4: the desktop provider can be started (PRP-0191, UDR-0173 D9).

    The ``chatwalaau-computer-use`` executable is installed (the wheel is a Windows x64
    dependency of chatwalaau), or ``COMPUTER_USE_PROVIDER_COMMAND`` names another one.
    """
    from app.computer_use.mcp_backend import provider_executable

    return bool(settings.computer_use_provider_command.strip()) or provider_executable() is not None


@functools.cache
def _interactive_session() -> bool:
    """H5 at startup: a service (session 0) or a headless session cannot be driven."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    session = wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(
        ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(session)
    ):
        return False
    if session.value == 0:
        return False
    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    desk = user32.OpenInputDesktop(0, False, 0x0100)
    if not desk:
        # Locked right now is not "no desktop": the session is interactive, H8 covers it.
        return bool(user32.GetDesktopWindow())
    user32.CloseDesktop(wintypes.HANDLE(desk))
    return True


def availability() -> Availability:
    """The H1-H6 verdict. Cheap: the expensive probes are cached for the process."""
    from app.demo import is_demo_mode

    if not settings.computer_use_enabled:
        return Availability(False, "disabled")
    if is_demo_mode():
        return Availability(False, "demo_mode")
    if sys.platform != "win32":
        return Availability(False, "unsupported_platform")
    if not _dependencies_present():
        return Availability(False, "missing_dependency")
    if not _interactive_session():
        return Availability(False, "no_interactive_desktop")
    if not settings.is_loopback_bind:
        return Availability(False, "not_loopback")
    return Availability(True, None)


def offered() -> bool:
    return availability().offered


def reset_cache() -> None:
    """Tests: forget the cached probes."""
    _dependencies_present.cache_clear()
    _interactive_session.cache_clear()


__all__ = ["Availability", "availability", "offered", "reset_cache"]
