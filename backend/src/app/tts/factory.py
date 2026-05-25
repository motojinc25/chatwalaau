"""TTS provider factory and selection dispatch (PRP-0063, UDR-0038).

Encapsulates the rule that decides which TTS provider backs the
text-to-speech endpoint (CTR-0039). The router (`app.tts.router`) is
unaware of the choice -- it only ever sees the abstract `TTSProvider`
Protocol.

Selection (UDR-0038 D1):

- TTS_PROVIDER=elevenlabs (default) -> ElevenLabs SDK provider.
- TTS_PROVIDER=azure-realtime        -> Azure OpenAI Realtime provider
                                        (e.g. gpt-realtime-2).
- unknown / empty                    -> treated as "elevenlabs"
                                        (back-compat, no hard failure).

Unlike STT (UDR-0036), selection is an EXPLICIT provider name rather
than a deployment-name substring, because the two backings are
heterogeneous services with no shared model-name namespace.

Returns None when the selected provider is not configured, so the
router keeps returning 503 -- byte-for-byte the pre-PRP-0063 behavior
for an unconfigured ElevenLabs install.

Adding a future provider is one new `TTSProvider` implementation, one
new TTS_PROVIDER value, and one branch here (UDR-0038 D7).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.tts.provider import TTSProvider

logger = logging.getLogger(__name__)

ProviderName = Literal["elevenlabs", "azure-realtime"]


def resolve_provider(name: str) -> ProviderName:
    """Classify the configured TTS_PROVIDER value.

    Pure function -- safe to unit-test without environment fixtures.
    Unknown / empty resolves to "elevenlabs" (back-compat default).
    """
    norm = (name or "").strip().lower()
    if norm in ("elevenlabs", "azure-realtime"):
        return norm  # type: ignore[return-value]
    return "elevenlabs"


def create_tts_provider(
    *,
    provider: str,
    elevenlabs_api_key: str,
    tts_voice_id: str,
    tts_model_id: str = "eleven_multilingual_v2",
    azure_openai_endpoint: str = "",
    realtime_deployment: str = "",
    realtime_voice: str = "alloy",
    realtime_api_version: str = "",
    realtime_audio_rate: int = 24000,
) -> TTSProvider | None:
    """Build the TTS provider for the active runtime configuration.

    Returns None when the selected provider is unconfigured (the router
    then returns 503). The function is intentionally synchronous and
    side-effect-free apart from one INFO log emission per call -- it is
    invoked from `app.main` at process import time.
    """
    selected = resolve_provider(provider)

    if selected == "azure-realtime":
        if not azure_openai_endpoint or not realtime_deployment:
            logger.warning(
                "TTS provider 'azure-realtime' selected but "
                "AZURE_OPENAI_ENDPOINT or TTS_REALTIME_DEPLOYMENT is "
                "unset; TTS is disabled (POST /api/tts -> 503). Set "
                "TTS_REALTIME_DEPLOYMENT to your voice Realtime "
                "deployment (e.g. gpt-realtime-2).",
            )
            return None

        from app.tts.realtime import AzureOpenAIRealtimeTTSProvider

        logger.info(
            "TTS lane: azure-realtime (deployment=%s, voice=%s, ws-path=%s, rate=%d Hz)",
            realtime_deployment,
            realtime_voice,
            (
                f"preview api-version={realtime_api_version}"
                if realtime_api_version
                else "GA /openai/v1/realtime?model=..."
            ),
            realtime_audio_rate,
        )
        return AzureOpenAIRealtimeTTSProvider(
            endpoint=azure_openai_endpoint,
            deployment=realtime_deployment,
            voice=realtime_voice,
            api_version=realtime_api_version,
            audio_rate=realtime_audio_rate,
        )

    # selected == "elevenlabs" -- preserves pre-PRP-0063 behavior.
    if not elevenlabs_api_key or not tts_voice_id:
        # Unconfigured: router returns 503, same as before this PRP.
        return None

    from app.tts.elevenlabs import ElevenLabsTTSProvider

    logger.info("TTS lane: elevenlabs (model=%s)", tts_model_id)
    return ElevenLabsTTSProvider(
        api_key=elevenlabs_api_key,
        voice_id=tts_voice_id,
        model_id=tts_model_id,
    )
