"""Azure OpenAI Realtime API TTS provider (PRP-0063, UDR-0038).

Opens a Realtime session per request, sends the message text as an input
item, requests an audio response, collects the streamed PCM16 deltas,
encodes them to MP3, and returns the bytes.

This is the OUTPUT mirror of `app.stt.realtime` (which drives a
transcription-only Realtime session for STT). Per UDR-0038 D3 the
external boundary (POST /api/tts) MUST remain synchronous JSON-in /
MP3-out; this provider achieves that by driving the Realtime state
machine to `response.done` before returning. There is no partial-audio
emission to the caller in this revision.

Unlike the STT whisper case, `gpt-realtime-2` is itself a VOICE model,
so the deployment name goes DIRECTLY into the URL `?model=` query -- no
transcription-vs-voice deployment split is required.

The WebSocket is closed via the `async with` context manager so an
exception path still releases the resource. Auth header selection
follows UDR-0034 via `app.azure_credential.get_realtime_websocket_auth_header()`.
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
from app.tts.audio_encode import encode_pcm16_to_mp3

logger = logging.getLogger(__name__)

# Per-request timeout. Short message playback settles well under 30 s;
# the 60 s ceiling guards against an unreachable upstream.
_SESSION_TIMEOUT_SEC = 60.0

# gpt-realtime is a CONVERSATIONAL audio model, not a TTS engine. Without
# an explicit persona it treats the supplied text as a user turn and
# generates a spoken *reply* instead of reading the text aloud. This
# instruction constrains it to verbatim read-aloud so the lane behaves
# as Text-to-Speech (CTR-0039), matching the ElevenLabs lane (PRP-0063
# operator feedback, UDR-0038).
_TTS_INSTRUCTIONS = (
    "You are a strict text-to-speech engine, not a conversational "
    "assistant. Speak the provided message aloud word-for-word, exactly "
    "as written, in its original language. Do not answer it, reply to "
    "it, react, greet, translate, summarize, rephrase, explain, or add "
    "or remove any words. Output only the spoken rendition of the exact "
    "text provided, nothing else."
)


class AzureOpenAIRealtimeTTSProvider:
    """TTS provider that drives an Azure OpenAI Realtime session.

    Constructor inputs are pre-validated at the factory level. This class
    itself is transport-only: it does not read environment variables.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        voice: str = "alloy",
        api_version: str = "",
        audio_rate: int = 24000,
    ) -> None:
        self._endpoint = endpoint
        # Voice Realtime deployment -- referenced in the URL ?model=
        # query. The canonical example is "gpt-realtime-2".
        self._deployment = deployment
        self._voice = voice
        # Empty api_version selects the GA path (recommended for
        # gpt-realtime-2 and any 2025-08-28+ Realtime model). A non-empty
        # value falls back to the preview path for legacy models.
        self._api_version = (api_version or "").strip()
        self._audio_rate = audio_rate

    @property
    def ws_url(self) -> str:
        """Compose the Azure Realtime WebSocket URL.

        Two URL shapes are supported (Microsoft Learn
        `realtime-audio-websockets`), identical to the STT lane:

        - GA path (default, when api_version is empty):
          `wss://<host>/openai/v1/realtime?model=<deployment>`
        - Preview path (when api_version is set):
          `wss://<host>/openai/realtime?api-version=<v>&deployment=<d>`

        Tolerates both `https://host/` and bare `host` forms of
        AZURE_OPENAI_ENDPOINT.
        """
        raw = self._endpoint
        if "://" not in raw:
            raw = f"https://{raw}"
        parsed = urlparse(raw)
        host = parsed.netloc.rstrip("/")
        if self._api_version:
            return f"wss://{host}/openai/realtime?api-version={self._api_version}&deployment={self._deployment}"
        # GA path -- preferred for gpt-realtime-2.
        return f"wss://{host}/openai/v1/realtime?model={self._deployment}"

    async def synthesize(self, text: str) -> bytes:
        """Synchronous-looking synthesis over an async Realtime session."""
        header_name, header_value = get_realtime_websocket_auth_header()
        url = self.ws_url
        logger.debug("Realtime TTS connecting (deployment=%s)", self._deployment)

        try:
            async with websockets.connect(
                url,
                additional_headers={header_name: header_value},
                max_size=None,
                open_timeout=10.0,
                close_timeout=5.0,
            ) as ws:
                pcm = await asyncio.wait_for(
                    self._drive_session(ws, text),
                    timeout=_SESSION_TIMEOUT_SEC,
                )
        except TimeoutError as e:
            msg = f"Realtime TTS session timed out after {_SESSION_TIMEOUT_SEC}s"
            raise RuntimeError(msg) from e
        except WebSocketException as e:
            msg = f"Realtime TTS WebSocket error: {e}"
            raise RuntimeError(msg) from e

        # Encode off the event loop -- PyAV is synchronous and CPU-bound.
        return await asyncio.to_thread(encode_pcm16_to_mp3, pcm, self._audio_rate)

    async def _drive_session(self, ws: websockets.WebSocketClientProtocol, text: str) -> bytes:
        # 1. Drain the server-pushed handshake event (session.created).
        first = await ws.recv()
        first_event = json.loads(first)
        first_type = first_event.get("type", "")
        if first_type not in ("session.created", "transcription_session.created"):
            logger.warning("Realtime TTS: unexpected initial event type %r", first_type)

        # 2. Configure the session for audio output and, critically, set
        #    the verbatim read-aloud persona so the conversational model
        #    speaks the text rather than answering it. The URL `?model=`
        #    creates a realtime session; we request audio-only output in
        #    the configured PCM format and voice so the deltas arrive as
        #    raw PCM16 that audio_encode can turn into MP3.
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "instructions": _TTS_INSTRUCTIONS,
                        "output_modalities": ["audio"],
                        "audio": {
                            "output": {
                                "format": {"type": "audio/pcm", "rate": self._audio_rate},
                                "voice": self._voice,
                            },
                        },
                    },
                }
            )
        )

        # 3. Add the text to synthesize as the input item (data, not a
        #    directive -- the persona above governs how it is rendered).
        await ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
        )

        # 4. Ask the server to speak the response. The per-response
        #    instructions repeat the verbatim persona so it applies even
        #    on API versions where response.instructions replaces (rather
        #    than augments) the session instructions.
        await ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": _TTS_INSTRUCTIONS,
                        "output_modalities": ["audio"],
                    },
                }
            )
        )

        # 5. Drain server events, collecting base64 PCM16 audio deltas
        #    until the response completes / fails. Both the GA event name
        #    (`response.output_audio.delta`) and the older
        #    (`response.audio.delta`) are accepted so a schema shift does
        #    not silently drop audio.
        audio_parts: list[bytes] = []
        async for raw_msg in ws:
            event = json.loads(raw_msg)
            etype = event.get("type", "")
            if etype in ("response.output_audio.delta", "response.audio.delta"):
                delta = event.get("delta")
                if delta:
                    audio_parts.append(base64.b64decode(delta))
            elif etype in ("response.done", "response.completed"):
                break
            elif etype in ("response.failed", "error"):
                err = event.get("error") or event.get("response", {}).get("status_details") or {}
                raise RuntimeError(f"Realtime TTS server error: {err}")
            # Any other event type (session.updated, response.created,
            # rate-limits, output_audio.done, ...) is acknowledged
            # implicitly by continuing the drain.

        if not audio_parts:
            msg = "Realtime TTS session closed without any audio output"
            raise RuntimeError(msg)
        return b"".join(audio_parts)
