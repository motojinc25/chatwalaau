"""Azure OpenAI Whisper STT provider (CTR-0021).

Uses Azure OpenAI's audio.transcriptions.create() with the Whisper model.
"""

import io
import logging

from openai import AzureOpenAI

logger = logging.getLogger(__name__)

# Map MIME types to file extensions for the Whisper API
_MIME_TO_EXT: dict[str, str] = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/flac": "flac",
}


class AzureOpenAIWhisperProvider:
    """STT provider using Azure OpenAI Whisper model."""

    def __init__(self, client: AzureOpenAI, model: str = "whisper-1") -> None:
        self._client = client
        self._model = model

    async def transcribe(self, audio_data: bytes, content_type: str) -> str:
        ext = _MIME_TO_EXT.get(content_type, "webm")
        filename = f"audio.{ext}"

        audio_file = io.BytesIO(audio_data)
        audio_file.name = filename

        transcription = self._client.audio.transcriptions.create(
            model=self._model,
            file=audio_file,
        )

        return transcription.text
