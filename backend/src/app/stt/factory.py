"""STT provider factory and model-kind dispatch (PRP-0061, UDR-0036; PRP-0241, UDR-0222).

Encapsulates the rule that decides which STT transport (REST
audio.transcriptions vs Realtime WebSocket session) to use for the
catalog's ``transcription`` offering. The router (`app.stt.router`) is
unaware of the kind -- it only ever sees the abstract `STTProvider` Protocol.

Source (UDR-0222 D1 / D4): the Model Offering Catalog's single
``transcription`` offering, resolved on EACH request by
:func:`current_stt_provider` and cached by its inputs, so a catalog save or an
App Settings save applies to the next request without a restart. No offering
-> None -> the router answers 503.

Dispatch order (UDR-0036 D1, inputs per UDR-0222 D3):

1. the offering's ``transport`` in {"rest", "realtime"} -> explicit override.
2. lower(model_ref) contains "realtime" -> realtime.
3. otherwise -> rest.

Failure modes:

- No endpoint (neither the offering's nor AZURE_OPENAI_ENDPOINT) -> None with
  a reason, logged once per configuration.
- PyAV / websockets are default dependencies (PRP-0061 amendment,
  UDR-0036 D4 revised); if they are absent from a broken install,
  the realtime path raises ImportError at first use.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from app.models_catalog import Offering
    from app.stt.provider import STTProvider

logger = logging.getLogger(__name__)

ModelKind = Literal["rest", "realtime"]

# The REST transcription api-version used when the offering declares none.
DEFAULT_REST_API_VERSION = "2024-10-21"


def resolve_kind(deployment: str, override: str) -> ModelKind:
    """Classify the transcription deployment into a transport kind.

    Pure function -- safe to unit-test without environment fixtures.
    """
    norm_override = (override or "").strip().lower()
    if norm_override in {"rest", "realtime"}:
        return norm_override  # type: ignore[return-value]
    if "realtime" in (deployment or "").lower():
        return "realtime"
    return "rest"


def create_stt_provider(
    offering: Offering,
    *,
    default_endpoint: str = "",
    realtime_audio_rate: int = 24000,
) -> tuple[STTProvider | None, str]:
    """Build the STT provider for a ``transcription`` offering.

    Returns ``(provider, "")``, or ``(None, reason)`` when it cannot be built. The
    endpoint falls back to ``default_endpoint`` (AZURE_OPENAI_ENDPOINT) and the key to
    the shared Azure credential lane when the offering omits them (UDR-0222 D3).
    """
    endpoint = (offering.endpoint or default_endpoint or "").strip()
    if not endpoint:
        return None, (f"transcription offering '{offering.id}' has no endpoint and AZURE_OPENAI_ENDPOINT is unset")
    deployment = offering.model_ref
    api_key = offering.api_key()
    kind = resolve_kind(deployment, offering.transport or "")

    if kind == "realtime":
        from app.stt.realtime import AzureOpenAIRealtimeWhisperProvider

        # Resolve the URL ?model= value. Per Microsoft Learn
        # `realtime-audio-websockets`, Azure requires a voice Realtime
        # deployment here (gpt-realtime / gpt-realtime-mini /
        # gpt-realtime-1.5). An offering using gpt-realtime-whisper MUST
        # set `connection_model_ref` to a voice deployment; otherwise the
        # WebSocket handshake returns HTTP 400. The fallback to `model_ref`
        # exists only for self-connecting models (e.g. gpt-4o-realtime-*).
        conn = (offering.connection_model_ref or "").strip() or deployment
        if conn == deployment and "whisper" in deployment.lower():
            logger.warning(
                "STT realtime: transcription offering '%s' has no connection_model_ref and "
                "model_ref=%r looks like a transcription-only model. Azure requires a VOICE "
                "Realtime deployment (gpt-realtime / gpt-realtime-mini / gpt-realtime-1.5) "
                "for the URL ?model= value. Expect HTTP 400 at WebSocket handshake until you "
                "set connection_model_ref to a voice Realtime deployment.",
                offering.id,
                deployment,
            )
        api_version = offering.api_version or ""
        logger.info(
            "STT lane: realtime (offering=%s, transcription_model=%s, connection_model=%s, ws-path=%s, rate=%d Hz)",
            offering.id,
            deployment,
            conn,
            f"preview api-version={api_version}" if api_version else "GA /openai/v1/realtime?model=...",
            realtime_audio_rate,
        )
        return (
            AzureOpenAIRealtimeWhisperProvider(
                endpoint=endpoint,
                deployment=deployment,
                connection_deployment=conn,
                api_version=api_version,
                audio_rate=realtime_audio_rate,
                api_key=api_key,
            ),
            "",
        )

    # The REST transcription lane.
    from openai import AzureOpenAI

    from app.azure_credential import get_azure_openai_kwargs
    from app.stt.whisper import AzureOpenAIWhisperProvider

    api_version = offering.api_version or DEFAULT_REST_API_VERSION
    logger.info("STT lane: rest (offering=%s, deployment=%s, api-version=%s)", offering.id, deployment, api_version)
    credential: dict[str, Any] = {"api_key": api_key} if api_key else get_azure_openai_kwargs()
    rest_client = AzureOpenAI(azure_endpoint=endpoint, api_version=api_version, **credential)
    return AzureOpenAIWhisperProvider(rest_client, model=deployment), ""


# ---- Request-time resolution (UDR-0222 D4) ---------------------------------

_cache: tuple[Any, STTProvider | None, str] | None = None


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
        offering.connection_model_ref,
        offering.transport,
        settings.azure_openai_endpoint,
        settings.whisper_realtime_audio_rate,
    )


def current_stt_provider() -> tuple[STTProvider | None, str]:
    """The STT provider for the current catalog and App Settings, or ``(None, reason)``.

    DEMO_MODE selects the demo provider (UDR-0041). Otherwise the catalog's
    ``transcription`` offering decides; the built provider is reused until one of its
    inputs changes, so a request never rebuilds it and a save never leaves a stale one.
    """
    global _cache
    from app.demo import is_demo_mode

    if is_demo_mode():
        key: Any = "demo"
        if _cache is None or _cache[0] != key:
            from app.demo.stt import DemoSTTProvider

            logger.info("STT lane: demo (DEMO_MODE)")
            _cache = (key, DemoSTTProvider(), "")
        return _cache[1], _cache[2]

    from app import models_catalog
    from app.core.config import settings

    offering = models_catalog.transcription_offering()
    if offering is None:
        return None, "no transcription offering in the Model Offering Catalog"
    key = _inputs_key(offering)
    if _cache is None or _cache[0] != key:
        provider, reason = create_stt_provider(
            offering,
            default_endpoint=settings.azure_openai_endpoint,
            realtime_audio_rate=int(settings.whisper_realtime_audio_rate),
        )
        if provider is None:
            logger.warning("STT is unavailable (POST /api/transcribe -> 503): %s", reason)
        _cache = (key, provider, reason)
    return _cache[1], _cache[2]


def reset_for_tests() -> None:
    """Forget the cached provider so the next request resolves it again."""
    global _cache
    _cache = None
