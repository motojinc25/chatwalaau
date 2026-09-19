"""ChatWalaau Desktop backend launcher (CTR-0208, PRP-0167, UDR-0151 D7/D8/D9).

Run by the Desktop with the active venv's interpreter and cwd = the profile directory.
It is a thin adapter, not a CLI replacement: it owns the loopback socket, the handshake
with the Electron Main process and the shutdown path, and serves the UNCHANGED
``app.main:app`` wrapped in the local access guard (desktop_guard.py).

Protocol (JSON Lines):

- stdin, first line:  {"type":"start","launchId","token","port","sandboxPort","profileDir","envId"}
  The token travels on this pipe and NEVER through the environment, so no process the
  backend spawns can inherit it (UDR-0151 D6).
- stdin, later:       {"type":"shutdown"}  -- or EOF (the Desktop died): graceful stop.
  The control pipe is PRIVATE (UDR-0157 D1/D2): it is moved to a non-inheritable handle
  before anything is read, and the process's standard input becomes the null device, so
  no child the backend starts can inherit it. On Windows a child touching a pipe on which
  this process has a pending synchronous read blocks until that read completes -- i.e.
  until the window closes (PRP-0175).
- stdout events:      "@@CWDESKTOP@@ " + {"launchId", "type": bound|ready|startup-error|stopping|stopped, ...}
  Any other stdout/stderr output is ordinary backend logging.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import traceback
from typing import Any, TextIO

EVENT_PREFIX = "@@CWDESKTOP@@ "
LOOPBACK = "127.0.0.1"

_emit_lock = threading.Lock()
_launch_id = ""


def emit(event_type: str, **fields: Any) -> None:
    line = EVENT_PREFIX + json.dumps({"launchId": _launch_id, "type": event_type, **fields}, ensure_ascii=True)
    with _emit_lock:
        out = sys.__stdout__
        if out is not None:
            out.write(line + "\n")
            out.flush()


def fail(code: str, message: str, exit_code: int = 2) -> None:
    emit("startup-error", code=code, message=message)
    sys.exit(exit_code)


# ---- Windows Job Object (UDR-0151 D9) -------------------------------------------------

_job_handle: Any = None


def join_kill_on_close_job() -> None:
    """Put this process in a Job Object with KILL_ON_JOB_CLOSE.

    Called BEFORE the app is imported, so every descendant (MCP stdio servers, shell
    commands, Cron children, skill scripts) is created inside the job. The handle is held
    for the life of the process; when the process exits -- normally or killed -- the last
    handle closes and Windows terminates every remaining descendant. Nothing is ever
    killed by name or by port.
    """
    global _job_handle
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Structure names mirror the Win32 SDK.
    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x2000

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    if not kernel32.SetInformationJobObject(
        handle, job_object_extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    _job_handle = handle


# ---- socket ----------------------------------------------------------------------------


def bind_loopback(preferred: int) -> socket.socket:
    """Bind 127.0.0.1 and keep the socket; fall back to an OS-chosen port if taken."""
    for port in [preferred, 0] if preferred else [0]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            # Never share a port with another process (SO_EXCLUSIVEADDRUSE).
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        try:
            sock.bind((LOOPBACK, port))
            sock.listen(2048)
            return sock
        except OSError:
            sock.close()
    raise OSError("no loopback port could be bound")


# ---- control pipe isolation (UDR-0157 D1/D2) --------------------------------------------


def _set_windows_std_input(fd: int) -> None:
    """Point the Win32 STD_INPUT_HANDLE at ``fd``'s handle.

    ``subprocess`` on Windows takes an unspecified stdin from GetStdHandle, not from
    fd 0, so rebinding the C descriptor alone is not enough.
    """
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetStdHandle.restype = wintypes.BOOL
    kernel32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    std_input_handle = wintypes.DWORD(-10 & 0xFFFFFFFF)
    if not kernel32.SetStdHandle(std_input_handle, msvcrt.get_osfhandle(fd)):
        raise OSError(ctypes.get_last_error(), "SetStdHandle failed")


def isolate_control_stdin() -> TextIO:
    """Move the control pipe to a private stream; make standard input the null device.

    Returns the ONLY reader of the control pipe. ``os.dup`` creates a non-inheritable
    descriptor (PEP 446), so no child process receives the pipe.
    """
    control = open(os.dup(0), encoding="utf-8")  # noqa: PTH123, SIM115 -- fd or device, lives for the process
    nul_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(nul_fd, 0)
    os.close(nul_fd)
    if sys.platform == "win32":
        _set_windows_std_input(0)
    sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: PTH123, SIM115 -- fd or device, lives for the process
    return control


def load_profile_env(profile: Path) -> int:
    """Load ``<profile>/.env`` into os.environ without overriding (UDR-0157 D4).

    ``load_dotenv()`` in app.main searches from the installed package's folder, which
    under the Desktop is site-packages, so it finds nothing. Returns the number of keys
    added; names and values are never logged.
    """
    env_file = profile / ".env"
    if not env_file.is_file():
        return 0
    from dotenv import load_dotenv

    before = set(os.environ)
    load_dotenv(env_file, override=False, encoding="utf-8")
    return len(set(os.environ) - before)


def read_start_command(control: TextIO) -> dict[str, Any]:
    line = control.readline()
    if not line:
        fail("BACKEND_START_FAILED", "no start command on stdin")
    try:
        cmd = json.loads(line)
    except json.JSONDecodeError:
        fail("BACKEND_START_FAILED", "start command is not JSON")
    required = {"type", "launchId", "token", "port", "sandboxPort", "profileDir"}
    if not isinstance(cmd, dict) or cmd.get("type") != "start" or not required <= cmd.keys():
        fail("BACKEND_START_FAILED", "malformed start command")
    return cmd


def watch_stdin(control: TextIO, loop: asyncio.AbstractEventLoop, server: Any) -> None:
    """Graceful stop on {"type":"shutdown"} or on EOF (the Desktop is gone)."""
    while True:
        line = control.readline()
        if not line:
            break
        with contextlib.suppress(json.JSONDecodeError):
            if json.loads(line).get("type") == "shutdown":
                break
    emit("stopping")
    loop.call_soon_threadsafe(setattr, server, "should_exit", True)


async def serve(cmd: dict[str, Any], control: TextIO) -> int:
    import uvicorn

    sock = bind_loopback(int(cmd["port"]))
    port = sock.getsockname()[1]
    emit("bound", port=port)

    # Desktop-owned keys win over the profile .env: python-dotenv and pydantic-settings
    # both rank the process environment first (PRP-0167 section 1.2, gap G13).
    os.environ["APP_HOST"] = LOOPBACK
    os.environ["APP_PORT"] = str(port)
    os.environ["MCP_APPS_SANDBOX_PORT"] = str(int(cmd["sandboxPort"]))

    # After the Desktop-owned keys, before app.main: os.environ consumers (catalog ${VAR}
    # references, environment-reading libraries) see the profile .env (UDR-0157 D4).
    try:
        added = load_profile_env(Path.cwd())
    except Exception:
        traceback.print_exc()
        fail("BACKEND_START_FAILED", "loading the profile .env failed; see the backend log")
        return 2
    print(f"profile .env loaded: {added} keys added", file=sys.stderr, flush=True)

    try:
        from app.core.config import settings
        from app.main import app
    except Exception:
        traceback.print_exc()
        fail("BACKEND_START_FAILED", "importing app.main failed; see the backend log")
        return 2

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from desktop_guard import DesktopGuard

    guarded = DesktopGuard(
        app,
        token=str(cmd["token"]),
        port=port,
        api_key_configured=lambda: bool((getattr(settings, "api_key", "") or "").strip()),
    )
    config = uvicorn.Config(guarded, host=LOOPBACK, port=port, lifespan="on", log_level="info")
    server = uvicorn.Server(config)
    loop = asyncio.get_running_loop()
    threading.Thread(target=watch_stdin, args=(control, loop, server), daemon=True, name="desktop-control").start()

    try:
        from app.core.version import get_app_version

        backend_version = get_app_version()
    except Exception:
        backend_version = ""

    async def announce_ready() -> None:
        while not server.started:
            if server.should_exit:
                return
            await asyncio.sleep(0.05)
        emit("ready", port=port, pid=os.getpid(), backendVersion=backend_version, envId=cmd.get("envId"))

    announcer = asyncio.create_task(announce_ready())
    try:
        await server.serve(sockets=[sock])
    finally:
        announcer.cancel()
    if not server.started:
        fail("BACKEND_START_FAILED", "the server stopped before it started; see the backend log")
    emit("stopped")
    return 0


def main() -> None:
    global _launch_id
    try:
        control = isolate_control_stdin()
    except OSError as exc:
        fail("BACKEND_START_FAILED", f"could not isolate the control pipe: {exc}")
        return
    cmd = read_start_command(control)
    _launch_id = str(cmd["launchId"])
    try:
        join_kill_on_close_job()
    except OSError as exc:
        fail("BACKEND_START_FAILED", f"could not create the process job: {exc}")
    profile = Path(str(cmd["profileDir"]))
    if not profile.is_dir():
        fail("PERMISSION_DENIED", f"profile directory missing: {profile}")
    os.chdir(profile)
    try:
        code = asyncio.run(serve(cmd, control))
    except OSError as exc:
        fail("PORT_CONFLICT", str(exc))
        return
    sys.exit(code)


if __name__ == "__main__":
    main()
