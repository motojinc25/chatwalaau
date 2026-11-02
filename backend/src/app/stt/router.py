"""Speech-to-Text REST endpoint (CTR-0021).

POST /api/transcribe accepts an audio file upload and returns transcribed text.
The provider comes from the catalog's transcription offering, resolved per request
(PRP-0241, UDR-0222 D4); an explicitly installed provider (the test seam) wins.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from app.auth import verify_api_key
from app.stt.factory import current_stt_provider
from app.stt.provider import STTProvider

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB (Whisper API limit)

router = APIRouter(prefix="/api", tags=["STT"])

_provider: STTProvider | None = None


def set_stt_provider(provider: STTProvider | None) -> None:
    """Install a fixed provider (tests); None returns to catalog resolution."""
    global _provider
    _provider = provider


@router.post("/transcribe", dependencies=[Depends(verify_api_key)])
async def transcribe(file: UploadFile) -> dict[str, str]:
    provider = _provider
    if provider is None:
        provider, reason = current_stt_provider()
        if provider is None:
            raise HTTPException(status_code=503, detail=f"STT provider not configured: {reason}")

    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="File must be an audio type")

    audio_data = await file.read()

    if len(audio_data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 25 MB limit")

    text = await provider.transcribe(audio_data, file.content_type or "audio/webm")
    return {"text": text}
