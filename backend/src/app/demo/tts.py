"""DemoTTSProvider (PRP-0066, UDR-0041 D3).

Implements the existing ``app.tts.provider.TTSProvider`` Protocol.
Returns a bundled placeholder MP3 instead of calling ElevenLabs or
Azure OpenAI Realtime, so the speaker / download UX (CTR-0040) is
exercised end-to-end with zero outbound cost.

No new Protocol seam; this is a third dispatch behind
``app.tts.factory.create_tts_provider`` (PRP-0063, UDR-0038).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _asset_path() -> Path:
    """Return the path to the bundled placeholder MP3."""
    return Path(__file__).resolve().parent / "assets" / "placeholder_tts.mp3"


class DemoTTSProvider:
    """TTS provider that ignores text and returns a bundled placeholder MP3."""

    def __init__(self) -> None:
        path = _asset_path()
        if not path.is_file():
            # The asset is bundled by the build; missing means a broken
            # install or a test fixture without assets. Fall back to a
            # silent 1-second MP3 generated at runtime so the API still
            # returns audio/mpeg bytes and the boundary contract holds.
            logger.warning(
                "DemoTTSProvider asset not found at %s; returning a silent 1s MP3 stub.",
                path,
            )
            self._bytes = _silent_mp3_stub()
        else:
            self._bytes = path.read_bytes()
            logger.info(
                "DemoTTSProvider loaded placeholder MP3 (%d bytes) from %s.",
                len(self._bytes),
                path,
            )

    async def synthesize(self, text: str) -> bytes:
        # Touch the input so static analysis does not flag it as unused.
        _ = len(text)
        # Tiny await keeps the call cooperative on the event loop and
        # mirrors the latency profile of a real synthesis call.
        await asyncio.sleep(0)
        return self._bytes


def _silent_mp3_stub() -> bytes:
    """Return a minimal 'silent MP3' byte sequence used as a fallback.

    Not a perfectly playable MP3 in every browser, but enough to keep
    the ``audio/mpeg`` boundary intact when the bundled asset is
    missing (broken install or pre-asset tests).
    """
    # 32 frames of MPEG-1 Layer III @ 44.1 kHz, 32 kbps, mono silence.
    # This is a minimal "valid-looking" MP3 header sequence; real assets
    # ship in ``assets/placeholder_tts.mp3``.
    frame = bytes.fromhex("fffb904400000000000000000000000000000000000000")
    return frame * 64


__all__ = ["DemoTTSProvider"]
