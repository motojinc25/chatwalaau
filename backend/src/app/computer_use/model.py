"""The desktop seam's data types and feature vocabulary (CTR-0231 / CTR-0236, RES-0007).

The host's engine uses them (``app.computer_use.backend`` re-exports these names) and
its MCP adapter builds them from the wire of the desktop provider (CTR-0236). The provider
itself is the native ``chatwalaau-computer-use`` executable (PRP-0191, ``computer-use/``),
which speaks the same vocabulary.

All coordinates are PHYSICAL virtual-desktop pixels; image coordinates exist only in
the host's perception layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

#: The minimum any desktop provider implements (RES-0007 F2).
REQUIRED_FEATURES: frozenset[str] = frozenset(
    {"windows.list", "windows.focus", "screen.capture", "input.pointer", "input.keys", "input.text"}
)
#: Precision / convenience layers, each with a host fallback.
OPTIONAL_FEATURES: frozenset[str] = frozenset(
    {"windows.resize", "windows.dialog", "session.lock", "ui.elements", "ui.caret", "input.clipboard"}
)
#: Transport-level features of the MCP protocol (CTR-0236, PRP-0190 Section 2.3): a
#: thumbnail with every capture and in-memory frame handles rendered on request. OPTIONAL in
#: the protocol, REQUIRED by this host, which does no image processing (PRP-0191 Q3).
PROTOCOL_FEATURES: frozenset[str] = frozenset({"screen.thumbnail", "screen.frames"})
#: OS change events (PRP-0191 A1, UDR-0173 D10 / D11): the provider REPORTS repaints and can
#: wait for one; the host uses them only as the trigger to capture, never as a verdict.
#: Declared by the provider's platform; whether a rectangle can be watched is decided per call.
EVENT_FEATURES: frozenset[str] = frozenset({"screen.changes"})

#: Grayscale thumbnail every frame carries, for change and stability tests.
THUMB_W = 192
THUMB_H = 108


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def intersect(self, other: Rect) -> Rect | None:
        left, top = max(self.left, other.left), max(self.top, other.top)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect(left, top, right, bottom)


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    process: str
    pid: int
    rect: Rect
    monitor: int = 0
    minimized: bool = False


@dataclass(frozen=True)
class Element:
    """One UI Automation element, as found. ``key`` is opaque to the engine."""

    key: object
    control: str
    name: str
    rect: Rect


@dataclass(frozen=True)
class Changes:
    """One ``screen_changes`` answer (PRP-0191 A1).

    ``available`` False: the rectangle cannot be watched now -- poll instead. ``changed``: a
    repaint since the token touched the rectangle (outside the ignored areas), or the provider
    cannot tell. ``seq`` is the token for the next call.
    """

    available: bool
    seq: int = 0
    changed: bool = False


@dataclass
class Frame:
    """One capture of a screen rectangle.

    ``gray`` is a THUMB_W x THUMB_H 8-bit grayscale thumbnail of the whole rectangle.
    ``encode(w, h)`` returns PNG bytes of the capture resized to ``w`` x ``h``; the
    pixels themselves stay inside the backend and are never persisted by it.
    """

    rect: Rect
    gray: bytes
    encode: Callable[[int, int], bytes]
    crop: Callable[[Rect], Frame] | None = None


__all__ = [
    "EVENT_FEATURES",
    "OPTIONAL_FEATURES",
    "PROTOCOL_FEATURES",
    "REQUIRED_FEATURES",
    "THUMB_H",
    "THUMB_W",
    "Changes",
    "Element",
    "Frame",
    "Rect",
    "WindowInfo",
]
