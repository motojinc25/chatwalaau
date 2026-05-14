"""TTS provider abstraction (CTR-0039).

Defines the TTSProvider Protocol for text-to-speech processing.
Implementations can be swapped without changing the router.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class TTSProvider(Protocol):
    """Protocol for text-to-speech providers."""

    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to speech audio.

        Args:
            text: Text content to synthesize.

        Returns:
            Audio bytes in MP3 format.
        """
        ...
