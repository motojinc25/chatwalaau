"""Text-to-Speech REST endpoint (CTR-0039).

POST /api/tts accepts text and returns synthesized audio (MP3).
"""

import logging

from elevenlabs.core import ApiError
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.tts.provider import TTSProvider

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 5000

router = APIRouter(prefix="/api", tags=["TTS"])

_provider: TTSProvider | None = None


def set_tts_provider(provider: TTSProvider) -> None:
    global _provider
    _provider = provider


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_LENGTH)


@router.post("/tts", dependencies=[Depends(verify_api_key)])
async def synthesize(request: TTSRequest):
    if _provider is None:
        raise HTTPException(status_code=503, detail="TTS provider not configured")

    try:
        audio_data = await _provider.synthesize(request.text)
    except ApiError as exc:
        logger.exception("TTS synthesis failed (ElevenLabs API %s): %s", exc.status_code, exc.body)
        if exc.status_code == 402:
            raise HTTPException(status_code=402, detail="TTS quota exceeded or plan upgrade required") from None
        if exc.status_code == 429:
            raise HTTPException(status_code=429, detail="TTS rate limit exceeded, try again later") from None
        raise HTTPException(status_code=502, detail="Speech synthesis failed") from None
    except Exception:
        logger.exception("TTS synthesis failed")
        raise HTTPException(status_code=502, detail="Speech synthesis failed") from None

    return Response(content=audio_data, media_type="audio/mpeg")
