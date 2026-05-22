"""Azure OpenAI Realtime API STT provider (PRP-0061, UDR-0036).

Opens a transcription-only Realtime session per request, streams the
decoded PCM payload, awaits the .completed event, and returns the
assembled transcript text.

Per UDR-0036 D3, the external boundary (POST /api/transcribe) MUST
remain synchronous. This provider achieves that by driving the
Realtime state machine to its terminal event before returning. There
is no partial-result emission to the caller in this revision.

The WebSocket is closed via the `async with` context manager so an
exception path still releases the resource (UDR-0036 risk mitigation).

Auth header selection follows UDR-0034: API key when
AZURE_OPENAI_API_KEY is set, Entra ID Bearer otherwise. Both forms are
served by `app.azure_credential.get_realtime_websocket_auth_header()`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from urllib.parse import urlparse

import websockets
from websockets.exceptions import WebSocketException

from app.azure_credential import get_realtime_websocket_auth_header
from app.stt.audio_decode import decode_to_pcm16

logger = logging.getLogger(__name__)

# Send-side chunking. The Realtime API documents no hard upper bound
# on a single input_audio_buffer.append, but very large frames have
# been observed to stall the receiver. 32 KB of base64 payload (~24 KB
# of PCM ~= 0.5 seconds at 24 kHz mono) is the established sweet spot.
_APPEND_CHUNK_B64_BYTES = 32 * 1024

# Per-request timeout. Realtime sessions for short voice-input clips
# settle well under 30 s; the 60 s ceiling guards against an
# unreachable upstream.
_SESSION_TIMEOUT_SEC = 60.0


class AzureOpenAIRealtimeWhisperProvider:
    """STT provider that drives an Azure OpenAI Realtime session.

    Constructor inputs are pre-validated at the factory level. This
    class itself is transport-only: it does not read environment
    variables.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        connection_deployment: str = "",
        api_version: str = "",
        audio_rate: int = 24000,
    ) -> None:
        self._endpoint = endpoint
        # Transcription model deployment -- referenced in the session
        # payload's audio.input.transcription.model field. The
        # canonical example is "gpt-realtime-whisper".
        self._deployment = deployment
        # Connection deployment -- referenced in the URL ?model= query.
        # Azure requires this to be a VOICE Realtime deployment, one of
        # the three model families documented in Microsoft Learn
        # `realtime-audio-websockets`. When empty, falls back to the
        # transcription deployment, which works only for self-connecting
        # Realtime models such as the gpt-4o-realtime preview family.
        self._connection_deployment = (connection_deployment or "").strip() or deployment
        # Empty api_version selects the GA path (recommended for
        # gpt-realtime-whisper and any 2025-08-28+ Realtime model).
        # A non-empty value falls back to the preview path for legacy
        # models such as gpt-4o-realtime-preview.
        self._api_version = (api_version or "").strip()
        self._audio_rate = audio_rate

    @property
    def ws_url(self) -> str:
        """Compose the Azure Realtime WebSocket URL.

        Two URL shapes are supported (Microsoft Learn
        `realtime-audio-websockets`):

        - GA path (default, used when api_version is empty):
          `wss://<host>/openai/v1/realtime?model=<connection-deployment>`
        - Preview path (used when api_version is set):
          `wss://<host>/openai/realtime?api-version=<v>&deployment=<c>`

        The `?model=` / `?deployment=` value MUST be a VOICE Realtime
        deployment name (gpt-realtime / gpt-realtime-mini /
        gpt-realtime-1.5). The transcription deployment
        (gpt-realtime-whisper) is specified in the session payload, not
        the URL. Mixing GA path with `api-version=` or preview path
        with `model=` returns HTTP 400 at handshake time.
        Tolerates both `https://host/` and bare `host` forms of
        AZURE_OPENAI_ENDPOINT.
        """
        raw = self._endpoint
        if "://" not in raw:
            raw = f"https://{raw}"
        parsed = urlparse(raw)
        host = parsed.netloc.rstrip("/")
        if self._api_version:
            return (
                f"wss://{host}/openai/realtime?api-version={self._api_version}&deployment={self._connection_deployment}"
            )
        # GA path -- preferred for gpt-realtime-whisper.
        return f"wss://{host}/openai/v1/realtime?model={self._connection_deployment}"

    async def transcribe(self, audio_data: bytes, content_type: str) -> str:
        """Synchronous-looking transcription over an async Realtime session."""
        pcm = await asyncio.to_thread(
            decode_to_pcm16,
            audio_data,
            content_type,
            self._audio_rate,
        )
        header_name, header_value = get_realtime_websocket_auth_header()
        url = self.ws_url
        logger.debug("Realtime STT connecting (deployment=%s)", self._deployment)

        try:
            async with websockets.connect(
                url,
                additional_headers={header_name: header_value},
                max_size=None,
                open_timeout=10.0,
                close_timeout=5.0,
            ) as ws:
                return await asyncio.wait_for(
                    self._drive_session(ws, pcm),
                    timeout=_SESSION_TIMEOUT_SEC,
                )
        except TimeoutError as e:
            msg = f"Realtime STT session timed out after {_SESSION_TIMEOUT_SEC}s"
            raise RuntimeError(msg) from e
        except WebSocketException as e:
            msg = f"Realtime STT WebSocket error: {e}"
            raise RuntimeError(msg) from e

    async def _drive_session(self, ws: websockets.WebSocketClientProtocol, pcm: bytes) -> str:
        # 1. Drain the server-pushed handshake event. The GA Realtime
        #    path sends `session.created` regardless of the session
        #    `type` field. Older preview paths and transcription-mode
        #    proxies may send `transcription_session.created`. Accept
        #    either and warn on anything else so future schema shifts
        #    are visible.
        first = await ws.recv()
        first_event = json.loads(first)
        first_type = first_event.get("type", "")
        if first_type not in ("session.created", "transcription_session.created"):
            logger.warning(
                "Realtime STT: unexpected initial event type %r",
                first_type,
            )

        # 2. Configure the session for transcription as a side effect
        #    of a realtime session (out-of-band transcription pattern).
        #
        #    Azure constraint: the URL `?model=<voice-deployment>`
        #    creates a REALTIME session. Sending `session.update` with
        #    `session.type = "transcription"` to that session is
        #    rejected by the server as
        #      "Passing a transcription session update event to a
        #       realtime session is not allowed".
        #
        #    Workaround per Microsoft Learn realtime-audio quickstart:
        #    keep `session.type = "realtime"`, configure
        #    `audio.input.transcription.model` to point at the
        #    transcription deployment (`gpt-realtime-whisper`), and
        #    never send `response.create`. The server transcribes the
        #    input audio automatically and emits
        #    `conversation.item.input_audio_transcription.{delta,
        #    completed}` events while skipping any voice output.
        #
        #    `output_modalities = ["text"]` is set defensively to
        #    forbid TTS even if a stray response.create slips in.
        #    `turn_detection = null` keeps us in manual-commit mode;
        #    the SPA microphone UX is press-and-release so we send the
        #    full clip then commit ourselves.
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "output_modalities": ["text"],
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": self._audio_rate},
                                "transcription": {"model": self._deployment},
                                "turn_detection": None,
                            },
                        },
                    },
                }
            )
        )

        # 3. Stream the decoded PCM as base64 in bounded chunks.
        b64 = base64.b64encode(pcm).decode("ascii")
        for offset in range(0, len(b64), _APPEND_CHUNK_B64_BYTES):
            await ws.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": b64[offset : offset + _APPEND_CHUNK_B64_BYTES],
                    }
                )
            )

        # 4. Commit the buffer so the server starts transcribing the
        #    delimited utterance. response.create is NOT sent: this is
        #    a transcription-only session, not a TTS / conversation
        #    generation session.
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

        # 5. Drain server events until completion / failure.
        text_parts: list[str] = []
        async for raw_msg in ws:
            event = json.loads(raw_msg)
            etype = event.get("type", "")
            if etype == "conversation.item.input_audio_transcription.delta":
                delta = event.get("delta", "")
                if delta:
                    text_parts.append(delta)
            elif etype == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript")
                if transcript is not None:
                    return transcript
                return "".join(text_parts)
            elif etype == "conversation.item.input_audio_transcription.failed":
                err = event.get("error") or {}
                raise RuntimeError(f"Realtime STT transcription failed: {err}")
            elif etype == "error":
                err = event.get("error") or {}
                raise RuntimeError(f"Realtime STT server error: {err}")
            # Any other event type (rate-limits, session.updated, etc.)
            # is acknowledged implicitly by continuing the drain.

        # The server closed the stream without a terminal event.
        if text_parts:
            return "".join(text_parts)
        msg = "Realtime STT session closed without a completed event"
        raise RuntimeError(msg)
