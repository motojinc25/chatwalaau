"""STT provider abstraction (CTR-0021).

Defines the STTProvider Protocol for speech-to-text processing.
Implementations can be swapped without changing the router.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class STTProvider(Protocol):
    """Protocol for speech-to-text providers."""

    async def transcribe(self, audio_data: bytes, content_type: str) -> str:
        """Transcribe audio data to text.

        Args:
            audio_data: Raw audio bytes.
            content_type: MIME type of the audio (e.g. "audio/webm", "audio/wav").

        Returns:
            Transcribed text string.
        """
        ...
