"""Retirement of the speech-model settings (PRP-0241, UDR-0222 D7).

The speech MODELS moved from App Settings into the Model Offering Catalog as the
``transcription`` and ``speech`` offerings. This module is the one place that knows
what each retired key meant, so the three surfaces that meet an old value say the
same thing:

- the App Settings store drops a retired key with a warning naming its successor
  and absorbs the voice of the provider that was in use into ``tts_voice``
  (``app.app_settings.store``);
- startup names each retired key still set in ``.env`` or the environment
  (``app.main``);
- ``chatwalaau models migrate-speech`` turns the old values into the offerings to
  add (``app.cli.models``).

The catalog is never written automatically.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# The retired App Settings keys (lower-case) -> the field that replaces each one.
RETIRED_SPEECH_KEYS: dict[str, str] = {
    "whisper_deployment_name": "the 'transcription' offering's model_ref",
    "whisper_model_kind": "the 'transcription' offering's transport",
    "whisper_realtime_connection_deployment": "the 'transcription' offering's connection_model_ref",
    "azure_openai_realtime_api_version": "the api_version of the 'transcription' / 'speech' offering",
    "tts_provider": "the 'speech' offering's provider (azure-openai or elevenlabs)",
    "tts_model_id": "the elevenlabs 'speech' offering's model_ref",
    "tts_realtime_deployment": "the azure-openai 'speech' offering's model_ref",
    "tts_voice_id": "the App Setting tts_voice",
    "tts_realtime_voice": "the App Setting tts_voice",
}

VOICE_KEYS = ("tts_voice_id", "tts_realtime_voice")

# The old TTS_PROVIDER default: an unset or unknown value meant elevenlabs.
_OLD_DEFAULT_TTS_PROVIDER = "elevenlabs"
_DEFAULT_ELEVENLABS_MODEL = "eleven_multilingual_v2"


def old_tts_provider(values: dict[str, Any]) -> str:
    """The TTS provider the old settings selected (UDR-0038 D1: unknown -> elevenlabs)."""
    raw = values.get("tts_provider")
    name = raw.strip().lower() if isinstance(raw, str) else ""
    return name if name in ("elevenlabs", "azure-realtime") else _OLD_DEFAULT_TTS_PROVIDER


def voice_key_in_use(values: dict[str, Any]) -> str:
    """Which retired voice key the old provider read."""
    return "tts_realtime_voice" if old_tts_provider(values) == "azure-realtime" else "tts_voice_id"


def retirement_warning(key: str, value: Any) -> str:
    """The store / startup message for a retired key, naming its successor."""
    shown = f"{key}={value!r}" if isinstance(value, str) and value.strip() else key
    return (
        f"{shown} is retired (PRP-0241) and no longer has any effect; the speech models are "
        f"Model Offering Catalog offerings now -- use {RETIRED_SPEECH_KEYS[key]}. "
        "`chatwalaau models migrate-speech` shows the offerings to add"
    )


# ---- .env / environment -----------------------------------------------------


def retired_env_values(env_file: Path | None = None) -> dict[str, str]:
    """{retired key: value} for every retired speech variable still SET.

    Read from the process environment first, then from the backend ``.env`` (the
    file Settings reads). Settings ignores these names (``extra="ignore"``), so this
    is the only place they are still looked at.
    """
    file_values: dict[str, str | None] = {}
    path = env_file if env_file is not None else Path(".env")
    if path.is_file():
        from dotenv import dotenv_values

        file_values = dict(dotenv_values(path))
    found: dict[str, str] = {}
    for key in RETIRED_SPEECH_KEYS:
        name = key.upper()
        value = os.environ.get(name)
        if value is None:
            value = file_values.get(name)
        if value is not None and value.strip():
            found[key] = value.strip()
    return found


# ---- The offerings the old values describe (models migrate-speech) ----------


def _unique_id(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    taken.add(candidate)
    return candidate


def planned_offerings(values: dict[str, Any], taken_ids: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """The ``transcription`` / ``speech`` offerings the old values describe, and notes.

    ``values`` are the retired keys (lower-case) from the store and ``.env``. Only a
    configuration the old code would actually have used becomes an offering: an STT
    deployment name, an azure-realtime TTS deployment, or an ElevenLabs voice id
    (the old ElevenLabs lane needed one). ``taken_ids`` keeps the new ids unique.
    """
    offerings: list[dict[str, Any]] = []
    notes: list[str] = []
    realtime_version = str(values.get("azure_openai_realtime_api_version") or "").strip()

    deployment = str(values.get("whisper_deployment_name") or "").strip()
    if deployment:
        from app.stt.factory import resolve_kind

        kind_raw = str(values.get("whisper_model_kind") or "").strip().lower()
        stt: dict[str, Any] = {
            "id": _unique_id("speech-to-text", taken_ids),
            "provider": "azure-openai",
            "model_ref": deployment,
            "operations": ["transcription"],
        }
        if kind_raw in ("rest", "realtime"):
            stt["transport"] = kind_raw
        connection = str(values.get("whisper_realtime_connection_deployment") or "").strip()
        realtime = resolve_kind(deployment, kind_raw) == "realtime"
        if realtime and connection:
            stt["connection_model_ref"] = connection
        # The old REST lane used a fixed api-version; the realtime version applied to
        # the realtime lane only.
        if realtime and realtime_version:
            stt["api_version"] = realtime_version
        offerings.append(stt)
    else:
        notes.append("No STT deployment was configured (whisper_deployment_name); no transcription offering.")

    provider = old_tts_provider(values)
    if provider == "azure-realtime":
        tts_deployment = str(values.get("tts_realtime_deployment") or "").strip()
        if tts_deployment:
            tts: dict[str, Any] = {
                "id": _unique_id("text-to-speech", taken_ids),
                "provider": "azure-openai",
                "model_ref": tts_deployment,
                "operations": ["speech"],
            }
            if realtime_version:
                tts["api_version"] = realtime_version
            offerings.append(tts)
        else:
            notes.append("tts_provider was azure-realtime but tts_realtime_deployment is empty; no speech offering.")
    elif str(values.get("tts_voice_id") or "").strip():
        offerings.append(
            {
                "id": _unique_id("text-to-speech", taken_ids),
                "provider": "elevenlabs",
                "model_ref": str(values.get("tts_model_id") or "").strip() or _DEFAULT_ELEVENLABS_MODEL,
                "operations": ["speech"],
                "api_key_env": "ELEVENLABS_API_KEY",
            }
        )
    else:
        notes.append("ElevenLabs was not configured (no tts_voice_id); no speech offering.")
    return offerings, notes


__all__ = [
    "RETIRED_SPEECH_KEYS",
    "VOICE_KEYS",
    "old_tts_provider",
    "planned_offerings",
    "retired_env_values",
    "retirement_warning",
    "voice_key_in_use",
]
