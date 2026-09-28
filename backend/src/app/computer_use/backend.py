"""The desktop seam behind Computer Use (CTR-0231 / CTR-0232, PRP-0189).

Perception and execution logic talk to the desktop ONLY through
:class:`DesktopBackend`. At runtime it is always
``app.computer_use.mcp_backend.McpDesktopBackend`` (PRP-0190 amendment A1), which forwards
to the stdio MCP desktop provider (CTR-0236 / CTR-0237) -- since PRP-0191 the native
``chatwalaau-computer-use`` executable (CAP-012, ``computer-use/``); tests use a fake.
That is what lets every rule of the engine run on any CI host (PRP-0189 Section 5).

All coordinates here are PHYSICAL virtual-desktop pixels. Image coordinates exist
only in :mod:`app.computer_use.perception`, which owns the mapping between the two.

REQUIRED vs OPTIONAL (PRP-0189 amendment A6, RES-0007). A backend declares what it can
do with :meth:`DesktopBackend.features`. The REQUIRED set is the minimum any desktop
provider -- in any language, in-process or behind MCP -- must implement: list / focus
windows, capture a rectangle, pointer input at COORDINATES, key chords and Unicode
text. Everything else raises precision or convenience and is OPTIONAL: UI Automation
elements (element targets, element conditions), the caret (a stability mask), modal
dialog detection, window resize, session-lock detection and clipboard paste. The
engine degrades when an optional feature is absent instead of failing the tool: an
element target is refused with ``unsupported`` and the model falls back to x / y.

PATH input and the GLOW (PRP-0192, OPTIONAL ``input.path`` / ``ui.overlay``): drag options and
``draw`` run as one timed path with a held button that the provider always releases; the glow
is a capture-excluded band around the target. Without ``input.path`` only the plain straight
``drag`` exists; without ``ui.overlay`` there is no glow.

EVENT features (PRP-0191 A1): ``screen.changes`` lets the waits of the perception layer
capture only after the provider reported a repaint; without it (or when a rectangle cannot
be watched) they poll. The verdict is the same thumbnail comparison either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

# The seam types and the feature vocabulary (app.computer_use.model, PRP-0191 moved them
# back from the retired Python provider package); re-exported here unchanged.
from app.computer_use.model import (
    EVENT_FEATURES,
    OPTIONAL_FEATURES,
    PROTOCOL_FEATURES,
    REQUIRED_FEATURES,
    THUMB_H,
    THUMB_W,
    Changes,
    Element,
    Frame,
    PathResult,
    Rect,
    WindowInfo,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def has(backend: object, feature: str) -> bool:
    """True when ``backend`` declares ``feature`` (a backend without ``features()`` is minimal)."""
    declared = getattr(backend, "features", None)
    return feature in (declared() if callable(declared) else REQUIRED_FEATURES)


class DesktopBackend(Protocol):
    """What the engine needs from a desktop. Every call runs on the desktop worker.

    Methods of an OPTIONAL feature the backend does not declare are never called.
    """

    def features(self) -> frozenset[str]: ...

    # -- windows -------------------------------------------------------------------
    def list_windows(self) -> list[WindowInfo]: ...

    def window(self, hwnd: int) -> WindowInfo | None: ...

    def foreground(self) -> WindowInfo | None: ...

    def focus(self, hwnd: int, size: tuple[int, int] | None) -> WindowInfo | None: ...

    def has_modal_dialog(self, target: WindowInfo) -> bool: ...

    def desktop_locked(self) -> bool: ...

    # -- perception ----------------------------------------------------------------
    def capture(self, rect: Rect) -> Frame: ...

    def elements(self, hwnd: int, max_count: int, budget_s: float) -> list[Element]: ...

    def element_rect(self, element: Element) -> Rect | None: ...

    def find_element(self, hwnd: int, name: str, control: str | None) -> bool: ...

    def caret_rect(self) -> Rect | None: ...

    def changes(self, rect: Rect, since: int | None, timeout_s: float, ignore: list[Rect]) -> Changes: ...  # EVENT

    # -- input -----------------------------------------------------------------------
    def cursor_pos(self) -> tuple[int, int]: ...

    def move(self, x: int, y: int) -> None: ...

    def click(self, x: int, y: int, button: str, count: int) -> None: ...

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None: ...

    def scroll(self, x: int, y: int, dy: int, dx: int) -> None: ...

    def keys(self, chord: list[str]) -> None: ...

    def type_text(self, text: str) -> None: ...  # REQUIRED input.text (Unicode)

    def paste_text(self, text: str) -> None: ...  # OPTIONAL input.clipboard

    def path(  # OPTIONAL input.path
        self,
        points: list[tuple[int, int]],
        button: str,
        modifiers: list[str],
        hold_ms: int,
        duration_ms: int,
        hover_ms: int,
        takeover_px: int,
        should_cancel: Callable[[], bool] | None = None,
    ) -> PathResult: ...

    def release_input(self) -> list[str]: ...  # OPTIONAL input.path

    def overlay_show(self, hwnd: int, ttl_s: float) -> bool: ...  # OPTIONAL ui.overlay

    def overlay_hide(self) -> None: ...  # OPTIONAL ui.overlay


__all__ = [
    "EVENT_FEATURES",
    "OPTIONAL_FEATURES",
    "PROTOCOL_FEATURES",
    "REQUIRED_FEATURES",
    "THUMB_H",
    "THUMB_W",
    "Changes",
    "DesktopBackend",
    "Element",
    "Frame",
    "PathResult",
    "Rect",
    "WindowInfo",
    "has",
]
