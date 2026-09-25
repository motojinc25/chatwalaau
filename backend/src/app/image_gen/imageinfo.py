"""Minimal PNG / JPEG / WebP header reader (PRP-0187, UDR-0169 D7/D14).

The edit path must know an input's pixel size (to derive the output size from the
source's shape, UDR-0169 D4) and whether a mask PNG has an alpha channel and the
source's dimensions (the Images API requirement). Reading a header is all that is
needed, so this stays on the standard library rather than adding an imaging
dependency to the backend.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class ImageInfo:
    """What the header says about one image."""

    format: str  # "png" | "jpeg" | "webp"
    width: int
    height: int
    has_alpha: bool


def _png_info(data: bytes) -> ImageInfo | None:
    if len(data) < 33 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    color_type = data[25]
    # 4 = grey + alpha, 6 = RGBA. A palette / grey / RGB image carries alpha only
    # through a tRNS chunk, which must appear before the first IDAT.
    has_alpha = color_type in (4, 6)
    if not has_alpha:
        offset = 8
        while offset + 8 <= len(data):
            (length,) = struct.unpack(">I", data[offset : offset + 4])
            chunk = data[offset + 4 : offset + 8]
            if chunk == b"tRNS":
                has_alpha = True
                break
            if chunk in (b"IDAT", b"IEND"):
                break
            offset += 12 + length
    return ImageInfo("png", width, height, has_alpha)


# SOF markers carrying the frame size (all except DHT C4, JPG C8, DAC CC).
_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def _jpeg_info(data: bytes) -> ImageInfo | None:
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        (length,) = struct.unpack(">H", data[offset + 2 : offset + 4])
        if marker in _JPEG_SOF and offset + 9 <= len(data):
            height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
            return ImageInfo("jpeg", width, height, False)
        offset += 2 + length
    return None


def _webp_info(data: bytes) -> ImageInfo | None:
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        has_alpha = bool(data[20] & 0x10)
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return ImageInfo("webp", width, height, has_alpha)
    if kind == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return ImageInfo("webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, bool((bits >> 28) & 1))
    if kind == b"VP8 " and len(data) >= 30:
        width, height = struct.unpack("<HH", data[26:30])
        return ImageInfo("webp", width & 0x3FFF, height & 0x3FFF, False)
    return None


def read_image_info(data: bytes) -> ImageInfo | None:
    """Header facts for a PNG / JPEG / WebP payload, or None when unrecognized."""
    try:
        if data.startswith(_PNG_SIGNATURE):
            return _png_info(data)
        if data.startswith(b"\xff\xd8"):
            return _jpeg_info(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp_info(data)
    except (struct.error, IndexError):
        return None
    return None


__all__ = ["ImageInfo", "read_image_info"]
