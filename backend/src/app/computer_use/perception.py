"""Screen Perception Engine (CTR-0231, PRP-0189 Section 2.7, UDR-0171 D5 / D7).

Owns everything between "pixels on the desktop" and "what the model is shown":

* the IMAGE <-> SCREEN coordinate mapping stored with every observation, so a pixel
  target is always mapped with the mapping of the observation it names (R6);
* the size the image is sent at -- a ChatWalaau-controlled long edge, further clamped
  on the Anthropic lane, whose service resizes large images server-side and would
  otherwise answer in coordinates of an image ChatWalaau never saw;
* change detection (dHash, then block-mean differences with masks) and stability
  detection -- never a fixed sleep (R4, R9, R11) -- captured on the provider's repaint
  reports when it has them (PRP-0191 A1), by polling otherwise.

No Windows import here: the desktop is reached only through ``DesktopBackend``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import TYPE_CHECKING

from app.computer_use.backend import THUMB_H, THUMB_W, Element, Frame, Rect, WindowInfo, has

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.computer_use.backend import Changes, DesktopBackend

#: Stability / change polling interval. A POLL, not a settle sleep: every wait ends as
#: soon as its condition holds, and the abort event is checked on every tick.
POLL_S = 0.12
#: Block size on the thumbnail (4x4 thumb pixels -> 48 x 27 = 1,296 blocks).
BLOCK = 4
BLOCKS_X = THUMB_W // BLOCK
BLOCKS_Y = THUMB_H // BLOCK
#: A block counts as changed when its mean moved by more than this (0..255).
BLOCK_DELTA = 6.0
#: A region changing in more than this share of the samples of one wait is animated.
ANIMATED_SHARE = 0.8
#: Anthropic resizes an image whose long edge exceeds 1568 px or whose area exceeds
#: about 1.15 megapixels, and the model then answers in the RESIZED space. Sending at
#: most that keeps the mapping exact (PRP-0189 Q2 answered 1920; this clamp is what
#: makes 1920 safe on the Anthropic lane).
ANTHROPIC_MAX_EDGE = 1568
ANTHROPIC_MAX_PIXELS = 1_150_000
#: Upper bound for the zoom factor of a region capture.
MAX_ZOOM = 3.0


class Aborted(Exception):
    """The human (or the Stop button) asked the desktop work to stop."""

    def __init__(self, by: str) -> None:
        super().__init__(by)
        self.by = by


# ---- Sizing and mapping -----------------------------------------------------------------


def image_size_for(width: int, height: int, max_edge: int, provider: str = "") -> tuple[int, int]:
    """The size an image of ``width`` x ``height`` is sent at (never upscaled here)."""
    if width <= 0 or height <= 0:
        return (1, 1)
    long_edge = max(width, height)
    scale = min(1.0, max_edge / long_edge)
    if provider == "anthropic":
        scale = min(scale, ANTHROPIC_MAX_EDGE / long_edge, math.sqrt(ANTHROPIC_MAX_PIXELS / (width * height)))
    w, h = max(1, int(width * scale)), max(1, int(height * scale))
    if provider == "anthropic":
        # Integer rounding can land a few pixels over the area limit; step down until inside.
        while w * h > ANTHROPIC_MAX_PIXELS and w > 1 and h > 1:
            scale *= 0.995
            w, h = max(1, int(width * scale)), max(1, int(height * scale))
    return (w, h)


@dataclass(frozen=True)
class Mapping:
    """Image coordinates of one observation <-> physical virtual-desktop pixels."""

    rect: Rect
    image_w: int
    image_h: int

    @property
    def sx(self) -> float:
        return self.rect.width / self.image_w

    @property
    def sy(self) -> float:
        return self.rect.height / self.image_h

    def to_screen(self, ix: int, iy: int) -> tuple[int, int]:
        ix = min(max(ix, 0), self.image_w - 1)
        iy = min(max(iy, 0), self.image_h - 1)
        return (self.rect.left + int((ix + 0.5) * self.sx), self.rect.top + int((iy + 0.5) * self.sy))

    def to_image(self, r: Rect) -> tuple[int, int, int, int] | None:
        clipped = r.intersect(self.rect)
        if clipped is None:
            return None
        return (
            int((clipped.left - self.rect.left) / self.sx),
            int((clipped.top - self.rect.top) / self.sy),
            int((clipped.right - self.rect.left) / self.sx),
            int((clipped.bottom - self.rect.top) / self.sy),
        )

    def image_rect_to_screen(self, x1: int, y1: int, x2: int, y2: int) -> Rect:
        left, top = self.to_screen(min(x1, x2), min(y1, y2))
        right, bottom = self.to_screen(max(x1, x2), max(y1, y2))
        return Rect(left, top, max(right, left + 1), max(bottom, top + 1))


# ---- Change detection ------------------------------------------------------------------


def _block_means(gray: bytes) -> list[float]:
    means: list[float] = []
    for by in range(BLOCKS_Y):
        for bx in range(BLOCKS_X):
            total = 0
            for dy in range(BLOCK):
                row = (by * BLOCK + dy) * THUMB_W + bx * BLOCK
                total += sum(gray[row : row + BLOCK])
            means.append(total / (BLOCK * BLOCK))
    return means


def dhash(gray: bytes) -> int:
    """64-bit difference hash of a 9x8 downsample of the thumbnail."""
    cells: list[float] = []
    cw, ch = THUMB_W / 9, THUMB_H / 8
    for gy in range(8):
        for gx in range(9):
            x0, y0 = int(gx * cw), int(gy * ch)
            x1, y1 = max(x0 + 1, int((gx + 1) * cw)), max(y0 + 1, int((gy + 1) * ch))
            total, n = 0, 0
            for y in range(y0, y1, 2):
                row = y * THUMB_W
                seg = gray[row + x0 : row + x1 : 2]
                total += sum(seg)
                n += len(seg)
            cells.append(total / max(n, 1))
    bits = 0
    for gy in range(8):
        for gx in range(8):
            bits = (bits << 1) | int(cells[gy * 9 + gx] > cells[gy * 9 + gx + 1])
    return bits


def mask_blocks(frame_rect: Rect, rects: list[Rect]) -> set[int]:
    """Block indices covered by ``rects`` (screen coordinates) within ``frame_rect``."""
    out: set[int] = set()
    if frame_rect.width <= 0 or frame_rect.height <= 0:
        return out
    bw = frame_rect.width / BLOCKS_X
    bh = frame_rect.height / BLOCKS_Y
    for r in rects:
        clipped = r.intersect(frame_rect)
        if clipped is None:
            continue
        x0 = int((clipped.left - frame_rect.left) / bw)
        x1 = min(BLOCKS_X - 1, int((clipped.right - 1 - frame_rect.left) / bw))
        y0 = int((clipped.top - frame_rect.top) / bh)
        y1 = min(BLOCKS_Y - 1, int((clipped.bottom - 1 - frame_rect.top) / bh))
        for by in range(y0, y1 + 1):
            for bx in range(x0, x1 + 1):
                out.add(by * BLOCKS_X + bx)
    return out


def changed_blocks(a: bytes, b: bytes, masked: set[int] | None = None) -> set[int]:
    if a == b:
        return set()
    ma, mb = _block_means(a), _block_means(b)
    masked = masked or set()
    return {i for i, (x, y) in enumerate(zip(ma, mb, strict=True)) if i not in masked and abs(x - y) > BLOCK_DELTA}


def changed_fraction(a: bytes, b: bytes, masked: set[int] | None = None) -> float:
    total = BLOCKS_X * BLOCKS_Y - len(masked or ())
    if total <= 0:
        return 0.0
    return len(changed_blocks(a, b, masked)) / total


def differs(a: bytes, b: bytes, masked: set[int] | None = None) -> bool:
    """Any visible change at all -- decides whether an image is worth sending (R4)."""
    if a == b:
        return False
    return bool(changed_blocks(a, b, masked))


# ---- Observations --------------------------------------------------------------------------


@dataclass
class Observation:
    """What the model was shown (or told) about the target at one moment."""

    id: str
    window: WindowInfo
    mapping: Mapping
    frame: Frame
    elements: list[tuple[str, Element]] = field(default_factory=list)
    dialog: bool = False
    zoom: bool = False
    created: float = field(default_factory=time.monotonic)

    def element(self, eid: str) -> Element | None:
        for key, el in self.elements:
            if key == eid:
                return el
        return None

    def public(self, *, changed: bool | None = None, settled: bool | None = None) -> dict:
        """The compact text form returned to the model (CTR-0230 Observation)."""
        out: dict = {
            "id": self.id,
            "window": self.window.title,
            "image": {"w": self.mapping.image_w, "h": self.mapping.image_h},
            "dialog": self.dialog,
        }
        if self.zoom:
            out["zoom_of"] = [self.mapping.rect.width, self.mapping.rect.height]
        if changed is not None:
            out["changed"] = changed
        if settled is not None:
            out["settled"] = settled
        rows = []
        for eid, el in self.elements:
            box = self.mapping.to_image(el.rect)
            if box is not None:
                rows.append([eid, el.control, el.name[:60], list(box)])
        out["elements"] = rows
        return out


def build_observation(
    backend: DesktopBackend,
    *,
    obs_id: str,
    target: WindowInfo,
    max_edge: int,
    provider: str,
    elements_max: int,
    region: Mapping | None = None,
    region_image: tuple[int, int, int, int] | None = None,
) -> Observation:
    """Capture the target (or a zoomed region of an earlier observation)."""
    fresh = backend.window(target.hwnd) or target
    if region is not None and region_image is not None:
        rect = region.image_rect_to_screen(*region_image)
        rect = rect.intersect(fresh.rect) or rect
        zoom = min(MAX_ZOOM, max_edge / max(rect.width, rect.height, 1))
        image_w = max(1, int(rect.width * zoom))
        image_h = max(1, int(rect.height * zoom))
        if provider == "anthropic":
            image_w, image_h = image_size_for(image_w, image_h, max_edge, provider)
    else:
        rect = fresh.rect
        image_w, image_h = image_size_for(rect.width, rect.height, max_edge, provider)
    frame = backend.capture(rect)
    mapping = Mapping(rect, image_w, image_h)

    found: list[tuple[str, Element]] = []
    if elements_max > 0 and has(backend, "ui.elements"):
        fg = backend.foreground()
        # A same-process dialog in front of the target is where the next click goes:
        # its elements come first (PRP-0189 Section 2.10, the Save-dialog case).
        sources = [fg.hwnd] if fg is not None and fg.hwnd != fresh.hwnd and fg.pid == fresh.pid else []
        sources.append(fresh.hwnd)
        n = 0
        for hwnd in sources:
            for el in backend.elements(hwnd, elements_max - n, 1.5):
                if mapping.to_image(el.rect) is None:
                    continue
                n += 1
                found.append((f"e{n}", el))
                if n >= elements_max:
                    break
            if n >= elements_max:
                break
    return Observation(
        id=obs_id,
        window=fresh,
        mapping=mapping,
        frame=frame,
        elements=found,
        dialog=backend.has_modal_dialog(fresh) if has(backend, "windows.dialog") else False,
        zoom=region is not None,
    )


# ---- Waits ----------------------------------------------------------------------------------
#
# Both waits judge the screen ONLY by thumbnail comparison. With the provider's repaint reports
# (``screen.changes``, PRP-0191 A1, UDR-0173 D10) they capture only after a repaint touched the
# rectangle, and a quiet period ends without a capture: "nothing repainted" means the last
# capture is still exact. Without them -- or once the provider cannot watch the rectangle --
# they poll every POLL_S, as before.

#: Longest single repaint wait: the abort event is checked at least this often (UDR-0173 D13).
EVENT_SLICE_S = 0.5
#: Ignore rectangles sent per wait (CTR-0236 ``screen_changes``).
MAX_IGNORE = 64


def _tick(abort_check: Callable[[float], str | None]) -> None:
    by = abort_check(POLL_S)
    if by:
        raise Aborted(by)


def _pace(last_capture: float, abort_check: Callable[[float], str | None]) -> None:
    """Keep captures at least POLL_S apart: a 60 fps animation cannot become a capture loop."""
    left = POLL_S - (time.monotonic() - last_capture)
    by = abort_check(left if left > 0 else 0.0)
    if by:
        raise Aborted(by)


def ignore_rects(frame_rect: Rect, blocks: set[int]) -> list[Rect]:
    """Screen rectangles covering ``blocks`` (row runs merged), at most MAX_IGNORE of them.

    Dropping the rest only makes the provider report MORE repaints, never fewer.
    """
    if not blocks or frame_rect.width <= 0 or frame_rect.height <= 0:
        return []
    bw = frame_rect.width / BLOCKS_X
    bh = frame_rect.height / BLOCKS_Y
    out: list[Rect] = []
    for by in range(BLOCKS_Y):
        bx = 0
        while bx < BLOCKS_X:
            if by * BLOCKS_X + bx not in blocks:
                bx += 1
                continue
            run = bx
            while bx < BLOCKS_X and by * BLOCKS_X + bx in blocks:
                bx += 1
            out.append(
                Rect(
                    frame_rect.left + int(run * bw),
                    frame_rect.top + int(by * bh),
                    frame_rect.left + math.ceil(bx * bw),
                    frame_rect.top + math.ceil((by + 1) * bh),
                )
            )
            if len(out) >= MAX_IGNORE:
                return out
    return out


class _Repaints:
    """The provider's repaint reports for ONE wait; ``active`` False means this wait polls."""

    def __init__(self, backend: DesktopBackend, rect: Rect) -> None:
        self.backend = backend
        self.rect = rect
        self.seq = 0
        self.active = has(backend, "screen.changes")
        if self.active:
            # The token is taken BEFORE the capture it guards, so nothing falls in between.
            self._take(backend.changes(rect, None, 0.0, []))

    def _take(self, got: Changes) -> bool:
        if not got.available:
            self.active = False
            return False
        self.seq = got.seq
        return got.changed

    def wait(self, until: float, ignore: list[Rect], abort_check: Callable[[float], str | None]) -> bool | None:
        """True: a repaint (look now). False: none until ``until``. None: poll from here on."""
        while self.active:
            by = abort_check(0.0)
            if by:
                raise Aborted(by)
            left = until - time.monotonic()
            if left <= 0:
                return False
            if self._take(self.backend.changes(self.rect, self.seq, min(left, EVENT_SLICE_S), ignore)):
                return True
        return None


def wait_for_change(
    backend: DesktopBackend,
    rect: Rect,
    base: bytes,
    *,
    timeout_s: float,
    threshold: float,
    abort_check: Callable[[float], str | None],
    masked: set[int] | None = None,
) -> tuple[bool, Frame]:
    """Wait until the rectangle differs from ``base`` by more than ``threshold``."""
    deadline = time.monotonic() + timeout_s
    repaints = _Repaints(backend, rect)
    ignore = ignore_rects(rect, masked or set())
    frame = backend.capture(rect)
    last_capture = time.monotonic()
    while True:
        if changed_fraction(base, frame.gray, masked) > threshold:
            return True, frame
        if time.monotonic() >= deadline:
            return False, frame
        seen = repaints.wait(deadline, ignore, abort_check) if repaints.active else None
        if seen is False:
            return False, frame  # nothing repainted until the deadline: the frame is current
        if seen:
            _pace(last_capture, abort_check)
        else:
            _tick(abort_check)
        frame = backend.capture(rect)
        last_capture = time.monotonic()


def wait_until_stable(
    backend: DesktopBackend,
    rect: Rect,
    *,
    stable_s: float,
    timeout_s: float,
    threshold: float,
    abort_check: Callable[[float], str | None],
) -> tuple[bool, Frame, int]:
    """Wait until nothing changed for ``stable_s``; returns (settled, frame, animated).

    The caret is masked, the cursor is never captured, and a block that keeps changing in
    most samples is treated as animation and masked for the rest of this wait, so a spinner
    or a clock cannot keep a screen "unstable" forever (PRP-0189 Section 2.7). Masked blocks
    are also left out of the repaint reports (PRP-0191 A1).
    """
    start = time.monotonic()
    deadline = start + timeout_s
    caret = backend.caret_rect() if has(backend, "ui.caret") else None
    masked = mask_blocks(rect, [caret]) if caret else set()
    repaints = _Repaints(backend, rect)
    prev = backend.capture(rect)
    last_capture = quiet_since = time.monotonic()
    samples = 0
    change_counts: dict[int, int] = {}
    animated: set[int] = set()
    while True:
        seen = None
        if repaints.active:
            until = min(quiet_since + stable_s, deadline)
            seen = repaints.wait(until, ignore_rects(rect, masked | animated), abort_check)
        if seen is False:
            # Nothing repainted: the previous capture is still the screen.
            now = time.monotonic()
            if now - quiet_since >= stable_s:
                return True, prev, len(animated)
            if now >= deadline:
                return False, prev, len(animated)
            continue
        if seen:
            _pace(last_capture, abort_check)
        else:
            _tick(abort_check)
        cur = backend.capture(rect)
        last_capture = time.monotonic()
        samples += 1
        moved = changed_blocks(prev.gray, cur.gray, masked | animated)
        for i in moved:
            change_counts[i] = change_counts.get(i, 0) + 1
        if samples >= 5:
            animated |= {i for i, n in change_counts.items() if n / samples > ANIMATED_SHARE}
            moved -= animated
        total = BLOCKS_X * BLOCKS_Y - len(masked | animated)
        if total > 0 and len(moved) / total > threshold:
            quiet_since = time.monotonic()
        prev = cur
        now = time.monotonic()
        if now - quiet_since >= stable_s:
            return True, cur, len(animated)
        if now >= deadline:
            return False, cur, len(animated)


__all__ = [
    "ANTHROPIC_MAX_EDGE",
    "EVENT_SLICE_S",
    "POLL_S",
    "Aborted",
    "Mapping",
    "Observation",
    "build_observation",
    "changed_blocks",
    "changed_fraction",
    "dhash",
    "differs",
    "ignore_rects",
    "image_size_for",
    "mask_blocks",
    "wait_for_change",
    "wait_until_stable",
]
