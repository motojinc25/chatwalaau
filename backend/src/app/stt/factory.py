"""STT provider factory and model-kind dispatch (PRP-0061, UDR-0036).

Encapsulates the rule that decides which STT transport (REST
audio.transcriptions vs Realtime WebSocket session) to use for the
configured deployment. The router (`app.stt.router`) is unaware of
the kind -- it only ever sees the abstract `STTProvider` Protocol.

Dispatch order (UDR-0036 D1):

1. WHISPER_MODEL_KIND in {"rest", "realtime"} -> explicit override.
2. lower(WHISPER_DEPLOYMENT_NAME) contains "realtime" -> realtime.
3. otherwise -> rest.

Failure modes:

- Missing endpoint / deployment -> return None (caller skips STT
  wiring, preserving pre-PRP-0061 silent-disable behavior).
- PyAV / websockets are default dependencies (PRP-0061 amendment,
  UDR-0036 D4 revised); if they are absent from a broken install,
  the realtime path raises ImportError at first use.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.stt.provider import STTProvider

logger = logging.getLogger(__name__)

ModelKind = Literal["rest", "realtime"]


def resolve_kind(deployment: str, override: str) -> ModelKind:
    """Classify the configured STT deployment into a transport kind.

    Pure function -- safe to unit-test without environment fixtures.
    """
    norm_override = (override or "").strip().lower()
    if norm_override in {"rest", "realtime"}:
        return norm_override  # type: ignore[return-value]
    if "realtime" in (deployment or "").lower():
        return "realtime"
    return "rest"


def create_stt_provider(
    *,
    azure_openai_endpoint: str,
    deployment: str,
    kind_override: str,
    realtime_connection_deployment: str = "",
    api_version_rest: str = "2024-10-21",
    api_version_realtime: str = "",
    realtime_audio_rate: int = 24000,
) -> STTProvider | None:
    """Build the STT provider for the active runtime configuration.

    Returns None when the endpoint is unset (STT is silently disabled,
    matching pre-PRP-0061 behavior). Otherwise returns either the REST
    provider or the Realtime provider per UDR-0036.

    The function is intentionally synchronous and side-effect-free
    apart from one INFO log emission per call -- it is invoked from
    `app.main` at process import time.
    """
    if not azure_openai_endpoint:
        return None

    kind = resolve_kind(deployment, kind_override)

    if kind == "realtime":
        from app.stt.realtime import AzureOpenAIRealtimeWhisperProvider

        # Resolve the URL ?model= value. Per Microsoft Learn
        # `realtime-audio-websockets`, Azure requires a voice Realtime
        # deployment here (gpt-realtime / gpt-realtime-mini /
        # gpt-realtime-1.5). Operators using gpt-realtime-whisper MUST
        # provide WHISPER_REALTIME_CONNECTION_DEPLOYMENT pointing to a
        # voice deployment; otherwise the WebSocket handshake returns
        # HTTP 400. The fallback to `deployment` exists only for
        # self-connecting models (e.g. gpt-4o-realtime-*).
        conn = (realtime_connection_deployment or "").strip() or deployment
        if conn == deployment and "whisper" in (deployment or "").lower():
            logger.warning(
                "STT realtime: WHISPER_REALTIME_CONNECTION_DEPLOYMENT is "
                "unset and WHISPER_DEPLOYMENT_NAME=%r looks like a "
                "transcription-only model. Azure requires a VOICE "
                "Realtime deployment (gpt-realtime / gpt-realtime-mini / "
                "gpt-realtime-1.5) for the URL ?model= value. Expect "
                "HTTP 400 at WebSocket handshake until you deploy a "
                "voice Realtime model and set "
                "WHISPER_REALTIME_CONNECTION_DEPLOYMENT=<voice-deployment>.",
                deployment,
            )

        logger.info(
            "STT lane: realtime (transcription_model=%s, connection_model=%s, ws-path=%s, rate=%d Hz)",
            deployment or "<unset>",
            conn or "<unset>",
            (
                f"preview api-version={api_version_realtime}"
                if api_version_realtime
                else "GA /openai/v1/realtime?model=..."
            ),
            realtime_audio_rate,
        )
        return AzureOpenAIRealtimeWhisperProvider(
            endpoint=azure_openai_endpoint,
            deployment=deployment,
            connection_deployment=conn,
            api_version=api_version_realtime,
            audio_rate=realtime_audio_rate,
        )

    # kind == "rest" -- preserves byte-for-byte pre-PRP-0061 behavior.
    from openai import AzureOpenAI

    from app.azure_credential import get_azure_openai_kwargs
    from app.stt.whisper import AzureOpenAIWhisperProvider

    logger.info(
        "STT lane: rest (deployment=%s, api-version=%s)",
        deployment or "<unset>",
        api_version_rest,
    )
    rest_client = AzureOpenAI(
        azure_endpoint=azure_openai_endpoint,
        api_version=api_version_rest,
        **get_azure_openai_kwargs(),
    )
    return AzureOpenAIWhisperProvider(rest_client, model=deployment)
