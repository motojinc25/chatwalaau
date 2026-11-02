"""TTS provider factory and selection dispatch (PRP-0063, UDR-0038; PRP-0241, UDR-0222).

Encapsulates the rule that decides which TTS provider backs the
text-to-speech endpoint (CTR-0039). The router (`app.tts.router`) is
unaware of the choice -- it only ever sees the abstract `TTSProvider`
Protocol.

Selection (UDR-0222 D2, superseding UDR-0038 D1): the provider of the Model
Offering Catalog's single ``speech`` offering.

- provider azure-openai -> Azure OpenAI Realtime voice model (e.g. gpt-realtime-2).
- provider elevenlabs   -> ElevenLabs SDK provider.
- no speech offering    -> None -> the router answers 503.

The offering is resolved on EACH request by :func:`current_tts_provider` and the
built provider is cached by its inputs (UDR-0222 D4), so a catalog save or a
``tts_voice`` save applies to the next request without a restart.

Adding a future provider is one provider value allowed on the ``speech``
operation (``models_catalog.SPEECH_PROVIDERS``), one ``TTSProvider``
implementation, and one branch here (UDR-0222 D6).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models_catalog import Offering
    from app.tts.provider import TTSProvider

logger = logging.getLogger(__name__)

# The Azure Realtime voice used when `tts_voice` is empty. ElevenLabs has no default:
# a voice id belongs to the operator's account.
DEFAULT_REALTIME_VOICE = "alloy"


def create_tts_provider(
    offering: Offering,
    *,
    voice: str = "",
    default_endpoint: str = "",
    realtime_audio_rate: int = 24000,
) -> tuple[TTSProvider | None, str]:
    """Build the TTS provider for a ``speech`` offering.

    Returns ``(provider, "")``, or ``(None, reason)`` when it cannot be built.
    ``voice`` is the ``tts_voice`` App Setting (UDR-0222 D5): an ElevenLabs voice id or
    an Azure voice name; empty selects the provider default where one exists.
    """
    voice = (voice or "").strip()

    if offering.provider == "azure-openai":
        endpoint = (offering.endpoint or default_endpoint or "").strip()
        if not endpoint:
            return None, f"speech offering '{offering.id}' has no endpoint and AZURE_OPENAI_ENDPOINT is unset"

        from app.tts.realtime import AzureOpenAIRealtimeTTSProvider

        api_version = offering.api_version or ""
        logger.info(
            "TTS lane: azure-realtime (offering=%s, deployment=%s, voice=%s, ws-path=%s, rate=%d Hz)",
            offering.id,
            offering.model_ref,
            voice or DEFAULT_REALTIME_VOICE,
            f"preview api-version={api_version}" if api_version else "GA /openai/v1/realtime?model=...",
            realtime_audio_rate,
        )
        return (
            AzureOpenAIRealtimeTTSProvider(
                endpoint=endpoint,
                deployment=offering.model_ref,
                voice=voice or DEFAULT_REALTIME_VOICE,
                api_version=api_version,
                audio_rate=realtime_audio_rate,
                api_key=offering.api_key(),
            ),
            "",
        )

    if offering.provider == "elevenlabs":
        api_key = offering.api_key()
        if not api_key:
            return None, f"speech offering '{offering.id}' references {offering.api_key_env}, which is not set"
        if not voice:
            return None, "the elevenlabs speech offering needs a voice id in the App Setting 'TTS voice' (tts_voice)"

        from app.tts.elevenlabs import ElevenLabsTTSProvider

        logger.info("TTS lane: elevenlabs (offering=%s, model=%s)", offering.id, offering.model_ref)
        return ElevenLabsTTSProvider(api_key=api_key, voice_id=voice, model_id=offering.model_ref), ""

    # Unreachable for a validated catalog (SPEECH_PROVIDERS), kept as a clear refusal.
    return None, f"speech offering '{offering.id}' uses unsupported provider {offering.provider!r}"


# ---- Request-time resolution (UDR-0222 D4) ---------------------------------

_cache: tuple[Any, TTSProvider | None, str] | None = None


def _inputs_key(offering: Offering) -> tuple[Any, ...]:
    """Everything the built provider depends on; a change rebuilds it."""
    from app.core.config import settings

    return (
        offering.id,
        offering.provider,
        offering.model_ref,
        offering.endpoint,
        offering.api_version,
        offering.api_key_env,
        offering.api_key(),
        settings.azure_openai_endpoint,
        settings.tts_voice,
        settings.tts_realtime_audio_rate,
    )


def current_tts_provider() -> tuple[TTSProvider | None, str]:
    """The TTS provider for the current catalog and App Settings, or ``(None, reason)``.

    DEMO_MODE selects the demo provider (UDR-0041). Otherwise the catalog's ``speech``
    offering decides; the built provider is reused until one of its inputs changes.
    """
    global _cache
    from app.demo import is_demo_mode

    if is_demo_mode():
        key: Any = "demo"
        if _cache is None or _cache[0] != key:
            from app.demo.tts import DemoTTSProvider

            logger.info("TTS lane: demo (DEMO_MODE)")
            _cache = (key, DemoTTSProvider(), "")
        return _cache[1], _cache[2]

    from app import models_catalog
    from app.core.config import settings

    offering = models_catalog.speech_offering()
    if offering is None:
        return None, "no speech offering in the Model Offering Catalog"
    key = _inputs_key(offering)
    if _cache is None or _cache[0] != key:
        provider, reason = create_tts_provider(
            offering,
            voice=settings.tts_voice,
            default_endpoint=settings.azure_openai_endpoint,
            realtime_audio_rate=int(settings.tts_realtime_audio_rate),
        )
        if provider is None:
            logger.warning("TTS is unavailable (POST /api/tts -> 503): %s", reason)
        _cache = (key, provider, reason)
    return _cache[1], _cache[2]


def reset_for_tests() -> None:
    """Forget the cached provider so the next request resolves it again."""
    global _cache
    _cache = None
