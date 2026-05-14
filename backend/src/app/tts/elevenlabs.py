"""ElevenLabs TTS provider (CTR-0039).

Uses the official ElevenLabs Python SDK for text-to-speech synthesis.
"""

import logging

from elevenlabs import ElevenLabs

logger = logging.getLogger(__name__)


class ElevenLabsTTSProvider:
    """TTS provider using ElevenLabs official SDK."""

    def __init__(self, api_key: str, voice_id: str, model_id: str = "eleven_multilingual_v2") -> None:
        self._client = ElevenLabs(api_key=api_key)
        self._voice_id = voice_id
        self._model_id = model_id

    async def synthesize(self, text: str) -> bytes:
        audio_iterator = self._client.text_to_speech.convert(
            text=text,
            voice_id=self._voice_id,
            model_id=self._model_id,
            output_format="mp3_44100_128",
        )
        # convert() returns an iterator of bytes chunks
        return b"".join(audio_iterator)
