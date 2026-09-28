"""Computer Use MCP Backend Adapter (CTR-0237, PRP-0190 Section 2.4, UDR-0172).

``McpDesktopBackend`` is a ``DesktopBackend`` whose methods forward to a desktop
provider speaking ``chatwalaau.computer-use/1`` over MCP stdio (CTR-0236). The engine,
the DSL, perception, the executor guards and the seven ``computer_*`` tools do not
change. Since PRP-0190 amendment A1 this is the ONLY runtime backend: the host never
touches the desktop itself.

* INTERNAL: the session is the ``mcp`` SDK's ``ClientSession`` owned here. The provider is
  never registered with MCP Tool Management and its tools never reach a model (D3).
* THREADS: the engine calls these methods synchronously on the desktop worker; one
  provider client thread with its own (Proactor) event loop owns the session, and every
  method submits a coroutine there and waits with a per-operation timeout.
* LAZY: the provider starts on the first call, never at app start (D6), and is checked
  with ``describe`` (protocol major, every REQUIRED feature).
* CHEAP POLLING: every capture asks for the grayscale thumbnail and a frame handle; the
  observation image is rendered by the provider with ``screen_encode`` at the size the
  host chose (D5). A provider MUST declare ``screen.thumbnail`` and ``screen.frames``: the
  host does no image processing of its own (PRP-0191 Q3, UDR-0173 D7).
* THE PROVIDER is the native ``chatwalaau-computer-use`` executable of the
  ``chatwalaau-computer-use`` wheel (PRP-0191, UDR-0173 D9), found in this environment's
  scripts directory, then on ``PATH``; ``COMPUTER_USE_PROVIDER_COMMAND`` replaces it.
* REPAINTS: a provider declaring ``screen.changes`` answers ``changes()`` (PRP-0191 A1); a
  refusal of that call is reported as ``Changes(available=False)`` so the wait polls instead.
* PATHS: ``path()`` waits for ``input_path`` in slices and sends ``input_cancel`` as soon as
  ``should_cancel()`` holds (the hotkey, Stop, Abort); a provider lost DURING a path gets
  ``input_release`` on its next start, so no button or modifier stays down (PRP-0192, UDR-0174 D6).
* FAILURE: a timeout, a closed pipe or an incompatible provider raises
  :class:`ProviderUnavailable` (``provider_unavailable``); the next call restarts the
  provider, at most :data:`MAX_RESTARTS` times in :data:`RESTART_WINDOW_S` (Q3).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import collections
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
import contextlib
from datetime import timedelta
import json
import logging
from pathlib import Path
import shlex
import shutil
import sys
import sysconfig
import threading
import time
from typing import TYPE_CHECKING, Any, NoReturn

from app.computer_use.backend import (
    EVENT_FEATURES,
    OPTIONAL_FEATURES,
    PROTOCOL_FEATURES,
    REQUIRED_FEATURES,
    Changes,
    Element,
    Frame,
    PathResult,
    Rect,
    WindowInfo,
)
from app.computer_use.engine import OpError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from mcp import ClientSession

logger = logging.getLogger(__name__)

#: The protocol this host speaks (CTR-0236); the major must match.
PROTOCOL = "chatwalaau.computer-use/1"
#: The provider executable of the chatwalaau-computer-use wheel (CTR-0238).
PROVIDER_EXECUTABLE = "chatwalaau-computer-use"

#: Restart policy (PRP-0190 Q3): restart on the next call, at most 3 times in 5 minutes.
MAX_RESTARTS = 3
RESTART_WINDOW_S = 300.0
START_TIMEOUT_S = 30.0
INPUT_TIMEOUT_S = 5.0
CAPTURE_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 2.0
#: How often a running path is checked for an abort to forward as ``input_cancel``.
PATH_POLL_S = 0.05
#: Handles that are not ``hwnd:0x...`` map to integers from here up (never a real HWND).
_SURROGATE_BASE = 1 << 48


class ProviderUnavailable(OpError):
    """The provider cannot serve this call; the tool answers ``provider_unavailable``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__("provider_unavailable", f"{reason}: {detail}" if detail else reason)
        self.code = reason


class ProviderCallError(Exception):
    """An operation the provider refused with a CTR-0236 error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def provider_executable() -> str | None:
    """The installed ``chatwalaau-computer-use`` executable, or None (CTR-0238).

    The wheel puts it in the running environment's scripts directory (the venv's
    ``Scripts`` on Windows); ``PATH`` is the second place to look.
    """
    name = PROVIDER_EXECUTABLE + (".exe" if sys.platform == "win32" else "")
    scripts = sysconfig.get_path("scripts")
    if scripts:
        candidate = Path(scripts) / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which(PROVIDER_EXECUTABLE)


def provider_command(raw: str) -> list[str]:
    """The command that starts the provider (PRP-0190 Q4: ``COMPUTER_USE_PROVIDER_COMMAND``).

    Empty -> the ``chatwalaau-computer-use`` executable (PRP-0191). Otherwise a command
    line (Windows quoting: ``"C:\\Program Files\\x\\provider.exe" --flag``).
    """
    raw = (raw or "").strip()
    if not raw:
        return [provider_executable() or PROVIDER_EXECUTABLE]
    parts = shlex.split(raw, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]


def stdio_connect(command: list[str]) -> Callable[[], AbstractAsyncContextManager[ClientSession]]:
    """Spawn ``command`` and open an initialised ``ClientSession`` over its stdio."""

    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[ClientSession]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client

        # Only the MCP SDK's safe variable set, plus UTF-8: the provider needs no API key,
        # token or secret from the backend's environment.
        env = {**get_default_environment(), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        if not Path(command[0]).is_file() and shutil.which(command[0]) is None:
            logger.error(
                "Computer Use provider %r not found; install the chatwalaau-computer-use package "
                "or set COMPUTER_USE_PROVIDER_COMMAND",
                command[0],
            )
        params = StdioServerParameters(command=command[0], args=command[1:], env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            yield session

    return connect


class _Link:
    """One live provider session on its own event-loop thread."""

    def __init__(self, connect: Callable[[], AbstractAsyncContextManager[ClientSession]]) -> None:
        self._connect = connect
        self.loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="computer-use-provider", daemon=True)
        self._thread.start()
        self.session: ClientSession | None = None
        self._closing: asyncio.Event | None = None
        self._main: Future[None] | None = None
        self.dead = False

    def open(self, timeout: float) -> None:
        ready: Future[None] = Future()

        async def main() -> None:
            self._closing = asyncio.Event()
            try:
                async with self._connect() as session:
                    self.session = session
                    ready.set_result(None)
                    await self._closing.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                raise
            finally:
                self.session = None
                self.dead = True

        self._main = asyncio.run_coroutine_threadsafe(main(), self.loop)
        ready.result(timeout)

    def submit(self, name: str, args: dict[str, Any], timeout: float) -> Future[Any]:
        session = self.session
        if session is None or self.dead:
            raise ConnectionError("the provider session is closed")
        return asyncio.run_coroutine_threadsafe(
            session.call_tool(name, args, read_timeout_seconds=timedelta(seconds=timeout)), self.loop
        )

    def call(self, name: str, args: dict[str, Any], timeout: float) -> Any:
        fut = self.submit(name, args, timeout)
        try:
            return fut.result(timeout + 1.0)
        except FutureTimeout:
            fut.cancel()
            raise

    def close(self) -> None:
        if self._closing is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._closing.set)
        if self._main is not None:
            with contextlib.suppress(Exception):
                self._main.result(STOP_TIMEOUT_S + 1.0)
            if not self._main.done():
                self._main.cancel()
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(STOP_TIMEOUT_S)
        if not self._thread.is_alive():
            self.loop.close()
        self.dead = True


class McpDesktopBackend:
    """``DesktopBackend`` over a CTR-0236 provider (CTR-0237)."""

    def __init__(
        self,
        connect: Callable[[], AbstractAsyncContextManager[ClientSession]] | None = None,
        *,
        command: list[str] | None = None,
    ) -> None:
        self.command = command or provider_command("")
        self._connect = connect or stdio_connect(self.command)
        self._lock = threading.RLock()
        self._link: _Link | None = None
        self._started_once = False
        self._restarts: collections.deque[float] = collections.deque()
        self.state = "stopped"  # stopped | running | failed
        self.failure = ""
        self.described: dict[str, Any] = {}
        self._features: frozenset[str] = frozenset()
        self._wire: frozenset[str] = frozenset()
        self._h2i: dict[str, int] = {}
        self._i2h: dict[int, str] = {}
        # A provider lost while it held a button (UDR-0174 D6): release on the next start.
        self._path_in_flight = False
        self._release_pending = False

    # -- lifecycle -------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """For ``GET /api/computer-use/status`` (CTR-0233); never starts the provider."""
        provider = self.described.get("provider") or {}
        out: dict[str, Any] = {
            "state": self.state,
            "name": provider.get("name"),
            "version": provider.get("version"),
            "protocol": self.described.get("protocol"),
            "features": sorted(self._wire) if self._wire else [],
            "pid": self.described.get("pid"),
            "restarts": len(self._restarts),
        }
        if self.failure:
            out["reason"] = self.failure
        return out

    def _ensure(self) -> _Link:
        with self._lock:
            link = self._link
            if link is not None and not link.dead:
                return link
            if self.state == "failed":
                raise ProviderUnavailable("failed", self.failure or "restart limit reached; restart ChatWalaau")
            if link is not None:
                self._drop()
            if self._started_once:
                now = time.monotonic()
                while self._restarts and now - self._restarts[0] > RESTART_WINDOW_S:
                    self._restarts.popleft()
                if len(self._restarts) >= MAX_RESTARTS:
                    self.state = "failed"
                    self.failure = f"the provider failed {MAX_RESTARTS} times in {int(RESTART_WINDOW_S)} s"
                    logger.error("Computer Use provider: %s; not restarting", self.failure)
                    raise ProviderUnavailable("failed", self.failure)
                self._restarts.append(now)
            self._started_once = True
            return self._start()

    def _start(self) -> _Link:
        link = _Link(self._connect)
        try:
            link.open(START_TIMEOUT_S)
            result = link.call("describe", {}, CAPTURE_TIMEOUT_S)
        except Exception as exc:
            link.close()
            self.state = "stopped"
            self.failure = f"start_failed: {type(exc).__name__}"
            logger.warning("Computer Use provider could not start (%s)", " ".join(self.command[:3]), exc_info=True)
            raise ProviderUnavailable("start_failed", type(exc).__name__) from None
        described = _structured(result)
        declared = frozenset(described.get("features") or [])
        protocol = str(described.get("protocol") or "")
        # The host does no image processing: the frame features are required here (Q3).
        missing = sorted((REQUIRED_FEATURES | PROTOCOL_FEATURES) - declared)
        if getattr(result, "isError", False) or protocol.split("/")[0] != PROTOCOL.split("/")[0] or missing:
            link.close()
            self.state = "stopped"
            self.failure = f"incompatible: protocol {protocol or '?'}, missing {missing}"
            logger.error("Computer Use provider is incompatible: %s", self.failure)
            raise ProviderUnavailable("incompatible", self.failure)
        self.described = described
        self._wire = declared
        self._features = declared & (REQUIRED_FEATURES | OPTIONAL_FEATURES | EVENT_FEATURES)
        self._link = link
        self.state = "running"
        self.failure = ""
        # Handles and element keys of an earlier provider process mean nothing now.
        self._h2i.clear()
        self._i2h.clear()
        if self._release_pending and "input.path" in declared:
            self._release_pending = False
            try:
                released = _structured(link.call("input_release", {}, INPUT_TIMEOUT_S)).get("released") or []
                logger.warning(
                    "Computer Use: released %s after a provider was lost during a path", released or "nothing"
                )
            except Exception:
                logger.warning("Computer Use: input_release after a lost path failed", exc_info=True)
        logger.info(
            "Computer Use provider %s %s running (pid %s)",
            (described.get("provider") or {}).get("name"),
            (described.get("provider") or {}).get("version"),
            described.get("pid"),
        )
        return link

    def _drop(self) -> None:
        link, self._link = self._link, None
        if link is not None:
            link.close()
        if self.state == "running":
            self.state = "stopped"

    def close(self) -> None:
        """FastAPI shutdown: close stdin (the provider exits), kill after a grace period."""
        with self._lock:
            self._drop()

    # -- calls -----------------------------------------------------------------------------------

    def _call(self, name: str, args: dict[str, Any], timeout: float) -> tuple[dict[str, Any], bytes | None]:
        link = self._ensure()
        try:
            result = link.call(name, args, timeout)
        except FutureTimeout:
            self._lost("timeout", name)
        except Exception as exc:
            # A closed pipe, a dead process, or the SDK's own read timeout.
            reason = "timeout" if "timed out" in str(exc).lower() else "lost"
            self._lost(reason, f"{name}: {type(exc).__name__}")
        body = _structured(result)
        if getattr(result, "isError", False):
            raise ProviderCallError(str(body.get("error") or "internal"), str(body.get("message") or ""))
        return body, _image(result)

    def _lost(self, reason: str, detail: str) -> NoReturn:
        logger.warning("Computer Use provider %s (%s); it restarts on the next call", reason, detail)
        if self._path_in_flight:
            self._release_pending = True
        with self._lock:
            self._drop()
            self.failure = f"{reason}: {detail}"
        raise ProviderUnavailable(reason, detail)

    def features(self) -> frozenset[str]:
        self._ensure()
        return self._features

    # -- handles -----------------------------------------------------------------------------------

    def _hwnd(self, handle: str) -> int:
        known = self._h2i.get(handle)
        if known is not None:
            return known
        # Windows handles keep their numeric value, so ids are the real window handles;
        # any other opaque form gets a surrogate (CTR-0236: handles are never interpreted
        # beyond this mapping).
        value: int | None = None
        if handle.startswith("hwnd:0x"):
            with contextlib.suppress(ValueError):
                value = int(handle[7:], 16)
        if value is None or value in self._i2h:
            value = _SURROGATE_BASE + len(self._h2i)
        self._h2i[handle] = value
        self._i2h[value] = handle
        return value

    def _handle(self, hwnd: int) -> str:
        handle = self._i2h.get(hwnd)
        return handle if handle is not None else f"hwnd:0x{int(hwnd):X}"

    def _window(self, raw: dict[str, Any] | None) -> WindowInfo | None:
        if not raw:
            return None
        return WindowInfo(
            hwnd=self._hwnd(str(raw["handle"])),
            title=str(raw.get("title") or ""),
            process=str(raw.get("process") or ""),
            pid=int(raw.get("pid") or 0),
            rect=_rect(raw["rect"]),
            monitor=int(raw.get("monitor") or 0),
            minimized=bool(raw.get("minimized")),
        )

    # -- windows -----------------------------------------------------------------------------------

    def list_windows(self) -> list[WindowInfo]:
        body, _ = self._call("windows_list", {}, CAPTURE_TIMEOUT_S)
        return [w for w in (self._window(r) for r in body.get("windows") or []) if w is not None]

    def window(self, hwnd: int) -> WindowInfo | None:
        try:
            body, _ = self._call("windows_get", {"handle": self._handle(hwnd)}, INPUT_TIMEOUT_S)
        except ProviderCallError as exc:
            if exc.code == "not_found":
                return None
            raise
        return self._window(body.get("window"))

    def foreground(self) -> WindowInfo | None:
        body, _ = self._call("windows_foreground", {}, INPUT_TIMEOUT_S)
        return self._window(body.get("window"))

    def focus(self, hwnd: int, size: tuple[int, int] | None) -> WindowInfo | None:
        args: dict[str, Any] = {"handle": self._handle(hwnd)}
        if size is not None:
            args["size"] = [int(size[0]), int(size[1])]
        try:
            body, _ = self._call("windows_focus", args, CAPTURE_TIMEOUT_S)
        except ProviderCallError as exc:
            if exc.code in ("focus_failed", "not_found"):
                return None
            raise
        return self._window(body.get("window"))

    def has_modal_dialog(self, target: WindowInfo) -> bool:
        body, _ = self._call("windows_dialog", {"handle": self._handle(target.hwnd)}, INPUT_TIMEOUT_S)
        return bool(body.get("modal"))

    def desktop_locked(self) -> bool:
        body, _ = self._call("session_locked", {}, INPUT_TIMEOUT_S)
        return bool(body.get("locked"))

    # -- perception ------------------------------------------------------------------------------

    def _keep(self, rect: Rect, thumbnail: bool) -> tuple[dict[str, Any], str]:
        """Capture ``rect`` into the provider's frame cache: (body, frame id)."""
        args: dict[str, Any] = {"rect": _rect_wire(rect), "thumbnail": thumbnail, "keep": True}
        body, _ = self._call("screen_capture", args, CAPTURE_TIMEOUT_S)
        frame_id = body.get("frame")
        if not frame_id:
            raise ProviderCallError("internal", "screen_capture returned no frame handle")
        return body, str(frame_id)

    def capture(self, rect: Rect) -> Frame:
        body, frame_id = self._keep(rect, thumbnail=True)
        captured = _rect(body.get("rect") or _rect_wire(rect))
        thumb = body.get("thumbnail")
        try:
            gray = base64.b64decode(str(thumb.get("gray") or ""), validate=True) if isinstance(thumb, dict) else b""
        except (binascii.Error, ValueError):
            gray = b""
        if not gray:
            raise ProviderCallError("internal", "screen_capture returned no thumbnail")
        return Frame(rect=captured, gray=gray, encode=self._encoder(frame_id, captured))

    def _encoder(self, frame_id: str, rect: Rect) -> Callable[[int, int], bytes]:
        def render(fid: str, w: int, h: int) -> bytes:
            _, png = self._call("screen_encode", {"frame": fid, "width": w, "height": h}, CAPTURE_TIMEOUT_S)
            if png is None:
                raise ProviderCallError("internal", "screen_encode returned no image")
            return png

        def encode(w: int, h: int) -> bytes:
            try:
                return render(frame_id, w, h)
            except ProviderCallError as exc:
                if exc.code != "not_found":
                    raise
            # Evicted (more than FRAME_CACHE captures since): render a fresh capture instead.
            _, fresh = self._keep(rect, thumbnail=False)
            return render(fresh, w, h)

        return encode

    def elements(self, hwnd: int, max_count: int, budget_s: float) -> list[Element]:
        args = {"handle": self._handle(hwnd), "max": int(max_count), "budget_ms": int(budget_s * 1000)}
        body, _ = self._call("ui_elements", args, budget_s + 5.0)
        out: list[Element] = []
        for row in body.get("elements") or []:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                out.append(
                    Element(
                        key=str(row["key"]),
                        control=str(row.get("control") or ""),
                        name=str(row.get("name") or ""),
                        rect=_rect(row["rect"]),
                    )
                )
        return out

    def element_rect(self, element: Element) -> Rect | None:
        try:
            body, _ = self._call("ui_element_rect", {"key": str(element.key)}, INPUT_TIMEOUT_S)
        except ProviderCallError as exc:
            if exc.code == "not_found":
                return None
            raise
        raw = body.get("rect")
        return _rect(raw) if raw else None

    def find_element(self, hwnd: int, name: str, control: str | None) -> bool:
        args: dict[str, Any] = {"handle": self._handle(hwnd), "name": name}
        if control:
            args["control"] = control
        body, _ = self._call("ui_find", args, CAPTURE_TIMEOUT_S)
        return bool(body.get("found"))

    def caret_rect(self) -> Rect | None:
        body, _ = self._call("ui_caret", {}, INPUT_TIMEOUT_S)
        raw = body.get("rect")
        return _rect(raw) if raw else None

    def changes(self, rect: Rect, since: int | None, timeout_s: float, ignore: list[Rect]) -> Changes:
        """``screen_changes`` (PRP-0191 A1): repaints since ``since``, waiting up to ``timeout_s``."""
        args: dict[str, Any] = {
            "rect": _rect_wire(rect),
            "timeout_ms": max(0, min(2000, int(timeout_s * 1000))),
            "ignore": [_rect_wire(r) for r in ignore[:64]],
        }
        if since is not None:
            args["since"] = int(since)
        try:
            body, _ = self._call("screen_changes", args, INPUT_TIMEOUT_S + args["timeout_ms"] / 1000)
        except ProviderCallError as exc:
            logger.debug("screen_changes refused (%s); this wait polls", exc.code)
            return Changes(available=False)
        return Changes(
            available=bool(body.get("available")),
            seq=int(body.get("seq") or 0),
            changed=bool(body.get("changed")),
        )

    # -- path input and the glow (PRP-0192) --------------------------------------------------------

    def path(
        self,
        points: list[tuple[int, int]],
        button: str,
        modifiers: list[str],
        hold_ms: int,
        duration_ms: int,
        hover_ms: int,
        takeover_px: int,
        should_cancel: Callable[[], bool] | None = None,
    ) -> PathResult:
        """``input_path``; sends ``input_cancel`` once ``should_cancel()`` holds (UDR-0174 D7)."""
        args = {
            "points": [[int(x), int(y)] for x, y in points],
            "button": button,
            "modifiers": list(modifiers),
            "hold_ms": int(hold_ms),
            "duration_ms": int(duration_ms),
            "hover_ms": int(hover_ms),
            "takeover_px": int(takeover_px),
        }
        timeout = INPUT_TIMEOUT_S + (hold_ms + duration_ms + hover_ms) / 1000
        link = self._ensure()
        self._path_in_flight = True
        try:
            try:
                fut = link.submit("input_path", args, timeout)
            except Exception as exc:
                self._lost("lost", f"input_path: {type(exc).__name__}")
            deadline = time.monotonic() + timeout + 1.0
            cancel_sent = False
            while True:
                try:
                    result = fut.result(PATH_POLL_S)
                    break
                except FutureTimeout:
                    if not cancel_sent and should_cancel is not None and should_cancel():
                        cancel_sent = True
                        with contextlib.suppress(Exception):
                            link.call("input_cancel", {}, INPUT_TIMEOUT_S)
                    if time.monotonic() > deadline:
                        fut.cancel()
                        self._lost("timeout", "input_path")
                except Exception as exc:
                    reason = "timeout" if "timed out" in str(exc).lower() else "lost"
                    self._lost(reason, f"input_path: {type(exc).__name__}")
        finally:
            self._path_in_flight = False
        body = _structured(result)
        if getattr(result, "isError", False):
            code, message = str(body.get("error") or "internal"), str(body.get("message") or "")
            if code == "input_blocked":
                raise OSError(message or "input blocked")
            raise ProviderCallError(code, message)
        interrupted = body.get("interrupted")
        return PathResult(
            completed=bool(body.get("completed")),
            interrupted=str(interrupted) if interrupted else None,
            moved=int(body.get("moved") or 0),
        )

    def release_input(self) -> list[str]:
        body, _ = self._call("input_release", {}, INPUT_TIMEOUT_S)
        return [str(n) for n in body.get("released") or []]

    def overlay_show(self, hwnd: int, ttl_s: float) -> bool:
        args = {"handle": self._handle(hwnd), "ttl_ms": max(1000, min(600_000, int(ttl_s * 1000)))}
        body, _ = self._call("overlay_show", args, INPUT_TIMEOUT_S)
        if not body.get("shown"):
            logger.debug("Computer Use glow not shown: %s", body.get("reason"))
        return bool(body.get("shown"))

    def overlay_hide(self) -> None:
        self._call("overlay_hide", {}, INPUT_TIMEOUT_S)

    # -- input -----------------------------------------------------------------------------------

    def _input(self, name: str, args: dict[str, Any], timeout: float | None = None) -> None:
        try:
            self._call(name, args, timeout if timeout is not None else INPUT_TIMEOUT_S)
        except ProviderCallError as exc:
            if exc.code == "input_blocked":
                # The failure SendInput itself raises (UIPI, the secure desktop).
                raise OSError(exc.message or "input blocked") from None
            raise

    def cursor_pos(self) -> tuple[int, int]:
        body, _ = self._call("input_cursor", {}, INPUT_TIMEOUT_S)
        return (int(body.get("x") or 0), int(body.get("y") or 0))

    def move(self, x: int, y: int) -> None:
        self._input("input_move", {"x": int(x), "y": int(y)})

    def click(self, x: int, y: int, button: str, count: int) -> None:
        self._input("input_click", {"x": int(x), "y": int(y), "button": button, "count": int(count)})

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._input("input_drag", {"x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2)})

    def scroll(self, x: int, y: int, dy: int, dx: int) -> None:
        self._input("input_scroll", {"x": int(x), "y": int(y), "dy": int(dy), "dx": int(dx)})

    def keys(self, chord: list[str]) -> None:
        self._input("input_keys", {"keys": list(chord)})

    def type_text(self, text: str) -> None:
        # Unicode key events take time per character; allow for long text.
        self._input("input_type_text", {"text": text}, INPUT_TIMEOUT_S + len(text) / 100)

    def paste_text(self, text: str) -> None:
        self._input("input_paste", {"text": text})


# ---- result helpers ------------------------------------------------------------------------------


def _structured(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", None) or []:
        if getattr(item, "type", "") == "text":
            with contextlib.suppress(ValueError, TypeError):
                parsed = json.loads(item.text)
                if isinstance(parsed, dict):
                    return parsed
    return {}


def _image(result: Any) -> bytes | None:
    for item in getattr(result, "content", None) or []:
        if getattr(item, "type", "") == "image":
            with contextlib.suppress(binascii.Error, TypeError, ValueError):
                return base64.b64decode(item.data)
    return None


def _rect(raw: Any) -> Rect:
    return Rect(int(raw["left"]), int(raw["top"]), int(raw["right"]), int(raw["bottom"]))


def _rect_wire(r: Rect) -> dict[str, int]:
    return {"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom}


__all__ = [
    "MAX_RESTARTS",
    "RESTART_WINDOW_S",
    "McpDesktopBackend",
    "ProviderCallError",
    "ProviderUnavailable",
    "provider_command",
    "stdio_connect",
]
