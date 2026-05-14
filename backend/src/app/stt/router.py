"""Speech-to-Text REST endpoint (CTR-0021).

POST /api/transcribe accepts an audio file upload and returns transcribed text.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from app.auth import verify_api_key
from app.stt.provider import STTProvider

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB (Whisper API limit)

router = APIRouter(prefix="/api", tags=["STT"])

_provider: STTProvider | None = None


def set_stt_provider(provider: STTProvider) -> None:
    global _provider
    _provider = provider


@router.post("/transcribe", dependencies=[Depends(verify_api_key)])
async def transcribe(file: UploadFile) -> dict[str, str]:
    if _provider is None:
        raise HTTPException(status_code=503, detail="STT provider not configured")

    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="File must be an audio type")

    audio_data = await file.read()

    if len(audio_data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 25 MB limit")

    text = await _provider.transcribe(audio_data, file.content_type or "audio/webm")
    return {"text": text}
