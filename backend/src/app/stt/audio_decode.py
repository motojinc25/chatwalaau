"""Browser audio container -> PCM16 decoder (PRP-0061, UDR-0036).

The Azure OpenAI Realtime API accepts only raw PCM payloads, but the
browser MediaRecorder ships audio as `audio/webm` Opus (Chrome / Edge
on most platforms; Firefox occasionally uses `audio/ogg` Opus despite
the same MIME label). This module demuxes / decodes / resamples any
incoming container into PCM16 mono at the configured sample rate so
the Realtime provider can stream it via input_audio_buffer.append.

The decoder uses PyAV (`av`), a default dependency of the
`chatwalaau` package since the PRP-0061 amendment (UDR-0036 D4
revised). Per UDR-0036 D5, the actual format is inferred from the
byte stream by PyAV's demuxer, not from the operator-provided
`content_type` hint. This tolerates the Firefox webm/ogg mismatch.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


def decode_to_pcm16(audio_bytes: bytes, content_type: str, target_rate: int = 24000) -> bytes:
    """Decode any browser-supplied audio container to PCM16 mono bytes.

    Args:
        audio_bytes: Raw container bytes (webm/Opus, ogg/Opus, wav, ...).
        content_type: MIME hint from the upload (advisory only -- PyAV
            sniffs the actual format from `audio_bytes`).
        target_rate: Output sample rate in Hz (16000 or 24000).

    Returns:
        Little-endian PCM16 mono samples, ready for base64 encoding
        and `input_audio_buffer.append`.

    Raises:
        ValueError: when the bytes do not contain a decodable audio
            stream.
    """
    import av  # type: ignore[import-untyped]
    import av.audio.resampler  # type: ignore[import-untyped]

    container = av.open(io.BytesIO(audio_bytes))
    try:
        audio_stream = next((s for s in container.streams if s.type == "audio"), None)
        if audio_stream is None:
            msg = f"No audio stream found in upload (content_type={content_type!r})"
            raise ValueError(msg)

        resampler = av.audio.resampler.AudioResampler(
            format="s16",
            layout="mono",
            rate=target_rate,
        )

        chunks: list[bytes] = []
        for frame in container.decode(audio_stream):
            chunks.extend(bytes(r.planes[0]) for r in resampler.resample(frame))

        # Flush the resampler at end-of-stream.
        chunks.extend(bytes(r.planes[0]) for r in resampler.resample(None))

        return b"".join(chunks)
    finally:
        container.close()
