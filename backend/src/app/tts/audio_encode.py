"""PCM16 -> MP3 encoder for the Realtime TTS lane (PRP-0063, UDR-0038).

The Azure OpenAI Realtime API emits speech as raw PCM16 chunks, but the
TTS contract (CTR-0039) returns MP3 (`audio/mpeg`) so the SPA can play
it via HTMLAudioElement and download it as an `.mp3` file (CTR-0040).
This module is the OUTPUT mirror of `app.stt.audio_decode` (which
decodes browser audio to PCM16 for the STT realtime lane).

The encoder uses PyAV (`av`), a default dependency of the `chatwalaau`
package since the PRP-0061 amendment (UDR-0036 D4 revised), so no new
dependency is introduced (UDR-0038 D4).
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


def encode_pcm16_to_mp3(pcm_bytes: bytes, sample_rate: int = 24000) -> bytes:
    """Encode little-endian PCM16 mono samples to MP3 bytes.

    Args:
        pcm_bytes: Little-endian PCM16 mono samples (as assembled from
            the Realtime `response.output_audio.delta` events).
        sample_rate: Input sample rate in Hz (e.g. 24000).

    Returns:
        MP3-encoded audio bytes suitable for an `audio/mpeg` response.
        Returns an empty bytes object when given no samples.

    Raises:
        RuntimeError: when the encode pipeline fails.
    """
    if not pcm_bytes:
        return b""

    import av  # type: ignore[import-untyped]
    import av.audio.fifo  # type: ignore[import-untyped]
    import av.audio.resampler  # type: ignore[import-untyped]

    out = io.BytesIO()
    container = av.open(out, mode="w", format="mp3")
    try:
        stream = container.add_stream("mp3", rate=sample_rate)
        codec_ctx = stream.codec_context

        # The MP3 encoder advertises its required sample format / layout;
        # resample the packed PCM16 source into that shape before
        # encoding so we do not depend on libmp3lame's internal format.
        resampler = av.audio.resampler.AudioResampler(
            format=codec_ctx.format,
            layout=codec_ctx.layout,
            rate=codec_ctx.rate,
        )

        # Build a source frame from the raw PCM16 mono payload.
        src = av.AudioFrame(format="s16", layout="mono", samples=len(pcm_bytes) // 2)
        src.sample_rate = sample_rate
        src.planes[0].update(pcm_bytes)

        # libmp3lame requires fixed-size frames (1152 samples); buffer the
        # resampled audio in a FIFO and read exact-size frames so the
        # encoder never sees an out-of-spec frame. The FIFO preserves
        # sample timing so packet pts/dts stay monotonic.
        fifo = av.audio.fifo.AudioFifo()
        for rframe in resampler.resample(src):
            fifo.write(rframe)
        for rframe in resampler.resample(None):  # flush the resampler
            fifo.write(rframe)

        frame_size = codec_ctx.frame_size or 1152
        while fifo.samples >= frame_size:
            frame = fifo.read(frame_size)
            for packet in stream.encode(frame):
                container.mux(packet)
        remaining = fifo.read()  # trailing partial frame (or None)
        if remaining is not None:
            for packet in stream.encode(remaining):
                container.mux(packet)
        for packet in stream.encode(None):  # flush the encoder
            container.mux(packet)
    except Exception as exc:
        # Normalise any PyAV / encoder failure to a single error type.
        msg = f"PCM16 -> MP3 encode failed: {exc}"
        raise RuntimeError(msg) from exc
    finally:
        container.close()

    return out.getvalue()
