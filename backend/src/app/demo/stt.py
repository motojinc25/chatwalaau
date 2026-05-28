"""DemoSTTProvider (PRP-0066, UDR-0041 D3).

Implements the existing ``app.stt.provider.STTProvider`` Protocol.
Returns a fixed bilingual sentence after a small artificial delay so
the recording-stop -> textarea-insert UX (CTR-0020) is exercised
without consuming Azure OpenAI Whisper credits.

No new Protocol seam is introduced; this is a third lane behind the
existing factory dispatch in ``app.stt.factory.create_stt_provider``.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_DEMO_TRANSCRIPT = "音声入力のデモです / This is a demo voice input."


class DemoSTTProvider:
    """STT provider that ignores audio and returns a fixed demo transcript.

    The artificial delay simulates the wait-for-Whisper UX so the
    spinner and ChatInput insert flow look the same as production.
    """

    def __init__(self, *, simulated_delay_ms: int = 250) -> None:
        self._delay_s = max(0, min(2000, simulated_delay_ms)) / 1000.0

    async def transcribe(self, audio_data: bytes, content_type: str) -> str:
        # Touch the inputs so static analysis does not flag them as unused.
        _ = (len(audio_data), content_type)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        logger.info("DemoSTTProvider returned fixed transcript (%d bytes received)", len(audio_data))
        return _DEMO_TRANSCRIPT


__all__ = ["DemoSTTProvider"]
