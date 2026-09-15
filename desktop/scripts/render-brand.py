"""Rasterize the product favicon for the Windows Desktop build (CTR-0213, PRP-0167).

Renders `frontend/public/favicon.svg` -- the single brand source (assets/docs/guides/
brand-assets.md) -- into the pixel sizes electron-builder needs, and writes:

- `icon-<px>.png` for every ICO size (the Node side packs them into icon.ico);
- `sidebar.rgb` / `header.rgb`, raw 24-bit RGB canvases for the two NSIS bitmaps.

Only PyMuPDF is used (already in the backend environment). MuPDF's SVG parser ignores
`<linearGradient>`, and the brand guide's site recipe substitutes a flat midpoint colour
for that reason. An application icon is the brand's most visible surface, so instead of
flattening it this script keeps the canonical ocean gradient by compositing:

  1. the rounded square alone -> the shape MASK (anti-aliased alpha);
  2. the artwork with the background removed -> the FOREGROUND (bubble, wave, dot);
  3. a real vertical gradient (#0EA5B7 -> #0E7490) painted through the mask, with the
     foreground composited over it.

Usage: python render-brand.py <favicon.svg> <output-dir>
"""

from __future__ import annotations

from pathlib import Path
import sys

import fitz

# Canonical palette (brand-assets.md section 2).
OCEAN_TOP = (0x0E, 0xA5, 0xB7)
OCEAN_BOTTOM = (0x0E, 0x74, 0x90)
HEADER_BG = (0xFF, 0xFF, 0xFF)

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
SIDEBAR = (164, 314)  # NSIS welcome / finish page
HEADER = (150, 57)  # NSIS inner-page header
SUPERSAMPLE = 4  # render large, then box-filter down: crisp edges at 16 px


def render(svg_text: str, px: int) -> bytearray:
    """Render an SVG to a straight-alpha RGBA buffer of px * px."""
    doc = fitz.open(stream=svg_text.encode("utf-8"), filetype="svg")
    zoom = px / doc[0].rect.width
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
    return bytearray(pix.samples)


def downsample(src: bytearray, size: int, factor: int) -> bytearray:
    """Box-filter an RGBA buffer of (size*factor)^2 down to size^2."""
    big = size * factor
    out = bytearray(size * size * 4)
    area = factor * factor
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for sy in range(y * factor, y * factor + factor):
                row = sy * big * 4
                for sx in range(x * factor, x * factor + factor):
                    i = row + sx * 4
                    alpha = src[i + 3]
                    # Weight colour by alpha so transparent pixels do not darken edges.
                    r += src[i] * alpha
                    g += src[i + 1] * alpha
                    b += src[i + 2] * alpha
                    a += alpha
            o = (y * size + x) * 4
            if a:
                out[o] = min(255, r // a)
                out[o + 1] = min(255, g // a)
                out[o + 2] = min(255, b // a)
            out[o + 3] = a // area
    return out


def compose_icon(mask: bytearray, fg: bytearray, size: int) -> bytearray:
    """Ocean gradient through the rounded-square mask, artwork composited over it."""
    out = bytearray(size * size * 4)
    denom = max(size - 1, 1)
    for y in range(size):
        t = y / denom
        base = tuple(round(OCEAN_TOP[c] + (OCEAN_BOTTOM[c] - OCEAN_TOP[c]) * t) for c in range(3))
        for x in range(size):
            i = (y * size + x) * 4
            shape_a = mask[i + 3]
            fg_a = fg[i + 3]
            # src-over of the foreground onto the masked gradient.
            out_a = fg_a + shape_a * (255 - fg_a) // 255
            if out_a:
                for c in range(3):
                    top = fg[i + c] * fg_a
                    bottom = base[c] * shape_a * (255 - fg_a) // 255
                    out[i + c] = min(255, (top + bottom) // out_a)
            out[i + 3] = out_a
    return out


def to_png(rgba: bytearray, size: int) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, size, size, bytes(rgba), True)
    return pix.tobytes("png")


def paste(canvas: bytearray, cw: int, icon: bytearray, size: int, left: int, top: int) -> None:
    """Alpha-composite a square RGBA icon onto an RGB canvas."""
    for y in range(size):
        cy = top + y
        for x in range(size):
            cx = left + x
            i = (y * size + x) * 4
            a = icon[i + 3]
            if not a:
                continue
            o = (cy * cw + cx) * 3
            for c in range(3):
                canvas[o + c] = (icon[i + c] * a + canvas[o + c] * (255 - a)) // 255


def gradient_canvas(width: int, height: int) -> bytearray:
    canvas = bytearray(width * height * 3)
    denom = max(height - 1, 1)
    for y in range(height):
        t = y / denom
        row = tuple(round(OCEAN_TOP[c] + (OCEAN_BOTTOM[c] - OCEAN_TOP[c]) * t) for c in range(3))
        for x in range(width):
            o = (y * width + x) * 3
            canvas[o] = row[0]
            canvas[o + 1] = row[1]
            canvas[o + 2] = row[2]
    return canvas


def flat_canvas(width: int, height: int, colour: tuple[int, int, int]) -> bytearray:
    return bytearray(bytes(colour) * (width * height))


def main() -> None:
    svg_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    svg = svg_path.read_text(encoding="utf-8")
    if 'fill="url(#ocean)"' not in svg:
        raise SystemExit("favicon.svg no longer has the ocean-gradient background; update this script")
    mask_svg = svg.replace('fill="#FFFFFF"', 'fill="none"').replace('stroke="#0EA5B7"', 'stroke="none"')
    mask_svg = mask_svg.replace('fill="#F59E0B"', 'fill="none"').replace('fill="url(#ocean)"', 'fill="#FFFFFF"')
    fg_svg = svg.replace('fill="url(#ocean)"', 'fill="none"')

    icons: dict[int, bytearray] = {}
    for size in ICO_SIZES:
        big = size * SUPERSAMPLE
        mask = downsample(render(mask_svg, big), size, SUPERSAMPLE)
        fg = downsample(render(fg_svg, big), size, SUPERSAMPLE)
        rgba = compose_icon(mask, fg, size)
        icons[size] = rgba
        (out_dir / f"icon-{size}.png").write_bytes(to_png(rgba, size))
        print(f"rendered {size}x{size}")

    # NSIS welcome/finish sidebar: ocean gradient with the logo in the upper third.
    sidebar = gradient_canvas(*SIDEBAR)
    logo = icons[128]
    paste(sidebar, SIDEBAR[0], logo, 128, (SIDEBAR[0] - 128) // 2, 56)
    (out_dir / "sidebar.rgb").write_bytes(bytes(sidebar))

    # NSIS inner-page header: white plate with the logo on the right (MUI right-aligns it).
    header = flat_canvas(*HEADER, HEADER_BG)
    small = icons[48]
    paste(header, HEADER[0], small, 48, HEADER[0] - 48 - 8, (HEADER[1] - 48) // 2)
    (out_dir / "header.rgb").write_bytes(bytes(header))
    print(f"rendered sidebar {SIDEBAR[0]}x{SIDEBAR[1]} and header {HEADER[0]}x{HEADER[1]}")


if __name__ == "__main__":
    main()
