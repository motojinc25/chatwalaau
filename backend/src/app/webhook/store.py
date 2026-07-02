"""Webhook store: per-source enable/disable + receipt records + subscriptions (CTR-0151).

The store is the SSOT for webhook gateway state (UDR-0075 D9), mirroring the Cron Job
Store (CTR-0131). Layout under WEBHOOK_STORE_DIR (default ".webhooks"):

    {source}.state.json                 per-source enable/disable state
    receipts/{source}/{receipt_id}.json one record per received notification + outcome
    subscriptions/{source}/{sub_id}.json one record per live Graph subscription
    dedupe/{source}.json                 persisted idempotency keys (UDR-0076 D8)
    .tick.lock                           single-flight maintenance-scheduler lock

The reader is tolerant: a malformed/unreadable file is skipped, never aborting a tick
or a list call. Receipt bodies are byte-capped by WEBHOOK_RECEIPT_MAX_BYTES.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import settings

logger = logging.getLogger(__name__)

_RECEIPTS_DIRNAME = "receipts"
_SUBS_DIRNAME = "subscriptions"
_DEDUPE_DIRNAME = "dedupe"
_LOCK_FILENAME = ".tick.lock"
_MAX_RECEIPTS_PER_SOURCE = 500


def store_dir() -> Path:
    """Return the configured webhook store directory, created if missing."""
    d = Path(settings.webhook_store_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def lock_path() -> Path:
    """Path of the single-flight maintenance-scheduler lock."""
    return store_dir() / _LOCK_FILENAME


# ---------------------------------------------------------------------------
# Per-source enable/disable state (persisted; governs live subscriptions)
# ---------------------------------------------------------------------------


def _state_path(source: str) -> Path:
    return store_dir() / f"{source}.state.json"


def get_source_state(source: str) -> dict[str, Any]:
    """Return a source's persisted state. Default enabled=True (UDR-0075 D9)."""
    path = _state_path(source)
    if not path.is_file():
        return {"enabled": True}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("enabled", True)
            return data
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt webhook state file: %s", path)
    return {"enabled": True}


def is_source_enabled(source: str) -> bool:
    return bool(get_source_state(source).get("enabled", True))


def set_source_enabled(source: str, enabled: bool) -> dict[str, Any]:
    """Persist a source's enabled flag and return the updated state."""
    state = get_source_state(source)
    state["enabled"] = bool(enabled)
    state["updated_at"] = datetime.now(UTC).isoformat()
    _state_path(source).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


# ---------------------------------------------------------------------------
# Receipt records (one JSON per received notification + outcome)
# ---------------------------------------------------------------------------


def _receipts_dir(source: str) -> Path:
    d = store_dir() / _RECEIPTS_DIRNAME / source
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cap(text: str, cap: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if cap <= 0 or len(encoded) <= cap:
        return text
    clipped = encoded[:cap].decode("utf-8", errors="ignore")
    return f"{clipped}\n... (truncated at {cap} bytes)"


def save_receipt(
    source: str,
    *,
    outcome: str,
    summary: str = "",
    detail: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Persist a receipt record and return it.

    ``outcome`` is one of accepted / duplicate / rejected (UDR-0075 D9). ``detail``
    holds the (capped) raw notification for the UI's receipt view.
    """
    now = datetime.now(UTC)
    receipt_id = f"{now.strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:6]}"
    cap = max(0, int(settings.webhook_receipt_max_bytes))
    detail_text = _cap(json.dumps(detail or {}, ensure_ascii=False), cap)
    record = {
        "id": receipt_id,
        "source": source,
        "outcome": outcome,
        "summary": summary,
        "job_id": job_id,
        "received_at": now.isoformat(),
        "detail": detail_text,
    }
    (_receipts_dir(source) / f"{receipt_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _prune_receipts(source)
    return record


def _prune_receipts(source: str) -> None:
    """Keep at most _MAX_RECEIPTS_PER_SOURCE newest receipts (bounded growth)."""
    files = sorted(_receipts_dir(source).glob("*.json"), reverse=True)
    for stale in files[_MAX_RECEIPTS_PER_SOURCE:]:
        with contextlib.suppress(OSError):
            stale.unlink()


def list_receipts(source: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """Return a source's receipt records, newest first."""
    base = store_dir() / _RECEIPTS_DIRNAME / source
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def get_receipt(source: str, receipt_id: str) -> dict[str, Any] | None:
    """Return one receipt record (id is jailed to the source's receipt dir)."""
    base = (store_dir() / _RECEIPTS_DIRNAME / source).resolve()
    path = (base / f"{receipt_id}.json").resolve()
    if base != path.parent or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def count_receipts(source: str) -> int:
    base = store_dir() / _RECEIPTS_DIRNAME / source
    return len(list(base.glob("*.json"))) if base.is_dir() else 0


# ---------------------------------------------------------------------------
# Subscription records (per live Graph subscription)
# ---------------------------------------------------------------------------


def _subs_dir(source: str) -> Path:
    d = store_dir() / _SUBS_DIRNAME / source
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_subscription(source: str, record: dict[str, Any]) -> dict[str, Any]:
    """Persist a subscription record (keyed by its Graph subscription id)."""
    sub_id = record["id"]
    (_subs_dir(source) / f"{sub_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return record


def list_subscriptions(source: str) -> list[dict[str, Any]]:
    """Return all persisted subscription records for a source, newest first."""
    base = store_dir() / _SUBS_DIRNAME / source
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in base.glob("*.json"):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out


def get_subscription(source: str, sub_id: str) -> dict[str, Any] | None:
    path = _subs_dir(source) / f"{sub_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def delete_subscription(source: str, sub_id: str) -> bool:
    path = _subs_dir(source) / f"{sub_id}.json"
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Persisted idempotency keys (UDR-0076 D8) -- restart-safe dedupe of spawned jobs
# ---------------------------------------------------------------------------


def _dedupe_path(source: str) -> Path:
    d = store_dir() / _DEDUPE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{source}.json"


def claim_dedupe_key(source: str, key: str, *, job_id: str) -> bool:
    """Atomically-ish claim an idempotency key. Returns True if newly claimed.

    A returning False means the key was already claimed (a redelivered or restarted
    notification maps to the same work; the caller skips re-spawning).
    """
    path = _dedupe_path(source)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}
    if key in data:
        return False
    # Bound growth: keep the newest 1000 keys.
    if len(data) >= 1000:
        for old in list(data)[: len(data) - 999]:
            data.pop(old, None)
    data[key] = {"job_id": job_id, "at": datetime.now(UTC).isoformat()}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


__all__ = [
    "claim_dedupe_key",
    "count_receipts",
    "delete_subscription",
    "get_receipt",
    "get_source_state",
    "get_subscription",
    "is_source_enabled",
    "list_receipts",
    "list_subscriptions",
    "lock_path",
    "save_receipt",
    "save_subscription",
    "set_source_enabled",
    "store_dir",
]
