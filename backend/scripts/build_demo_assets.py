"""Generate bundled demo assets for ChatWalaʻau Demo Mode (PRP-0066).

Run manually to (re)create:

- backend/src/app/demo/assets/placeholder_tts.mp3        (silent ~2s MP3)
- backend/src/app/demo/assets/placeholder_image_1.png    (1024x1024 demo card)
- backend/src/app/demo/assets/placeholder_image_2.png    (1024x1024 demo card)
- backend/src/app/demo/assets/placeholder_image_edit.png (1024x1024 demo card)
- backend/src/app/demo/assets/demo_rag_corpus.pdf        (small PDF for RAG seeding)

All assets are author-original (programmatic). No third-party media is
copied; UDR-0041 D6 / risk assessment commitment.

Invocation::

    uv run python scripts/build_demo_assets.py

Idempotent: running again overwrites every output deterministically.
"""

from __future__ import annotations

from pathlib import Path
import struct
import zlib

import av
import fitz  # PyMuPDF

ASSET_DIR = Path(__file__).resolve().parent.parent / "src" / "app" / "demo" / "assets"


# ---- MP3 (silent ~2 s, MPEG audio layer III, mono 44.1 kHz) ----------------


def write_silent_mp3(target: Path, *, seconds: float = 2.0) -> None:
    """Encode ~`seconds` of silence as MP3 using PyAV (libmp3lame).

    Result is a real, decodable MP3 file (~25 KB) that every modern
    browser plays back.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 44100
    layout = "mono"
    frame_size = 1152  # MP3 frames

    with av.open(str(target), mode="w", format="mp3") as out_container:
        stream = out_container.add_stream("mp3", rate=sample_rate)
        stream.layout = layout

        frames_needed = int((seconds * sample_rate) / frame_size) + 1
        for _ in range(frames_needed):
            frame = av.AudioFrame(format="s16p", layout=layout, samples=frame_size)
            frame.sample_rate = sample_rate
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))  # all zero -> silence
            for packet in stream.encode(frame):
                out_container.mux(packet)

        # flush
        for packet in stream.encode(None):
            out_container.mux(packet)


# ---- PNG (1024x1024, stdlib only) ------------------------------------------


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    chunk = chunk_type + data
    crc = zlib.crc32(chunk) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", crc)


def write_solid_png(target: Path, width: int, height: int, rgb: tuple[int, int, int]) -> None:
    """Write a minimal opaque-RGB PNG of the given uniform colour.

    Pure stdlib (no Pillow): a compressed raster of ``height`` rows where
    each row is ``[filter_byte=0] + RGB triples``.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(
        ">IIBBBBB",
        width,
        height,
        8,  # bit depth
        2,  # colour type 2 = truecolour RGB
        0,  # compression
        0,  # filter
        0,  # interlace
    )

    r, g, b = rgb
    row = bytes([0]) + bytes([r, g, b]) * width
    raw = row * height
    idat = zlib.compress(raw, level=9)

    blob = sig + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")
    target.write_bytes(blob)


# ---- PDF (small bundled corpus for RAG seeding) ----------------------------


def write_demo_corpus_pdf(target: Path) -> None:
    """Write a tiny ~3-page PDF that the demo RAG pipeline auto-ingests.

    Content is author-original prose about ChatWalaʻau itself so a demo
    reviewer searching the corpus gets self-referential citations that
    make the RAG surface feel "alive."
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    pages = [
        (
            "ChatWalaʻau Demo Corpus",
            "ChatWalaʻau is an open-source AI agent runtime distributed via PyPI as the\n"
            "`chatwalaau` package. The name combines `chat` with the Hawaiian word\n"
            "`walaʻau`, meaning `to chat`, `to talk`, or `to converse`.\n\n"
            "The runtime serves a single-page application that connects to a FastAPI\n"
            "backend. The backend orchestrates AI agents using Microsoft Agent\n"
            "Framework (MAF). Streaming chat is driven by the AG-UI protocol over\n"
            "Server-Sent Events.\n\n"
            "Features include multimodal chat, voice input via Azure OpenAI Whisper or\n"
            "the new Realtime API, on-demand text-to-speech via ElevenLabs or Azure\n"
            "OpenAI Realtime voice models, image generation via Azure OpenAI Images,\n"
            "and Retrieval-Augmented Generation backed by a local ChromaDB store.",
        ),
        (
            "Demo Mode (Chapter 2)",
            "Demo Mode is enabled by setting DEMO_MODE=true in the environment. When\n"
            "enabled, every metered external provider routes through an in-process\n"
            "dummy implementation:\n\n"
            "  - The chat client is replaced by DemoChatClient, which streams scripted\n"
            "    scenarios from a keyword-routed lookup table.\n"
            "  - The STT provider returns a fixed bilingual sentence.\n"
            "  - The TTS provider returns a bundled placeholder MP3.\n"
            "  - generate_image and edit_image return one of two bundled PNG cards.\n"
            "  - The RAG embedder is replaced by a deterministic stdlib hash function\n"
            "    that produces 1536-dimensional unit vectors matching the dimensionality\n"
            "    of text-embedding-3-small.\n\n"
            "Weather queries continue to use Open-Meteo because it is free and\n"
            "requires no API key. This is the one live external integration retained\n"
            "in demo mode so the demo has a real-feeling anchor.",
        ),
        (
            "Deployment Topology (Chapter 3)",
            "The reference cloud target for ChatWalaʻau Demo Mode is Azure Web App for\n"
            "Containers. The Dockerfile under assets/docker/ produces a single image\n"
            "that bundles the React SPA inside the Python wheel and exposes port 8000.\n"
            "TLS terminates at the platform front door, so the application itself runs\n"
            "HTTP.\n\n"
            "Authentication is NOT relaxed in demo mode. A demo bound to a non-loopback\n"
            "host MUST set either API_KEY (Bearer auth for CLI clients) or both\n"
            "AUTH_USERNAME and AUTH_PASSWORD_HASH (ID/PW + opaque session cookie for\n"
            "SPA users). The startup validators that warn on missing credentials fire\n"
            "identically in demo and live modes.\n\n"
            "State resets on every container restart because the demo deployment does\n"
            "not mount Azure Files. The bundled demo corpus PDF is auto-ingested into\n"
            "ChromaDB during the FastAPI lifespan startup whenever the collection is\n"
            "empty.",
        ),
    ]

    doc = fitz.open()
    for title, body in pages:
        page = doc.new_page(width=595, height=842)  # A4 portrait, in points
        # Title
        page.insert_text(
            (72, 96),
            title,
            fontsize=18,
            fontname="helv",
            color=(0.1, 0.1, 0.2),
        )
        # Body
        page.insert_textbox(
            fitz.Rect(72, 130, 523, 770),
            body,
            fontsize=11,
            fontname="helv",
            color=(0.05, 0.05, 0.05),
            align=0,
        )
    doc.save(str(target))
    doc.close()


# ---- main ------------------------------------------------------------------


def main() -> None:
    print(f"Writing assets into {ASSET_DIR}")
    ASSET_DIR.mkdir(parents=True, exist_ok=True)

    write_silent_mp3(ASSET_DIR / "placeholder_tts.mp3")
    print("  - placeholder_tts.mp3")

    # Three distinct uniform colours so reviewers can tell the cards apart.
    write_solid_png(ASSET_DIR / "placeholder_image_1.png", 1024, 1024, (50, 80, 160))
    print("  - placeholder_image_1.png")
    write_solid_png(ASSET_DIR / "placeholder_image_2.png", 1024, 1024, (200, 90, 60))
    print("  - placeholder_image_2.png")
    write_solid_png(ASSET_DIR / "placeholder_image_edit.png", 1024, 1024, (90, 160, 80))
    print("  - placeholder_image_edit.png")

    write_demo_corpus_pdf(ASSET_DIR / "demo_rag_corpus.pdf")
    print("  - demo_rag_corpus.pdf")

    print("Done.")


if __name__ == "__main__":
    main()
