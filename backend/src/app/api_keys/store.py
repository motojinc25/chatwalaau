"""API Key Store (CTR-0282, PRP-0253, UDR-0234).

Managed API keys replace ``API_KEY`` and ``RESPONSES_API_KEY``. The store is the JSONC file
named by ``API_KEYS_FILE`` (default ``api_keys.jsonc``, relative to the working directory).

- A secret is ``cwk_<id>_<secret>`` (D4): 8 base32 characters of id and 32 random bytes in
  base64url. It is returned once, by :meth:`KeyStore.create` / :meth:`KeyStore.reissue`,
  and never stored: only ``sha256(secret)`` is kept, compared in constant time (D3).
- Each key has ONE scope (D2): ``responses`` < ``api`` < ``admin``.
- A key can be disabled (kept but refused, PRP-0253 Q1), can expire, and its name, scope,
  expiry and enabled flag may change. Its secret can only be reissued (D5).
- Writes are atomic (temporary file + ``os.replace``) under a process lock; readers reload
  when the file's mtime changes, so a CLI edit or a second instance is seen without a
  restart.
- A missing file means no keys. An unreadable or invalid file is logged at ERROR and read
  as no keys: authentication fails closed and the server keeps running (D9).
- Every change appends one line to ``<stem>.audit.jsonl`` beside the file: time, actor,
  action, key id, name and scope -- never a secret or a hash (D10).

This module imports nothing from ``app`` at load time, so ``app.core.config`` may use it.
"""

from __future__ import annotations

import base64
import contextlib
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import threading
from typing import Any

logger = logging.getLogger(__name__)

SCOPES: tuple[str, ...] = ("responses", "api", "admin")
SCOPE_RANK: dict[str, int] = {scope: rank for rank, scope in enumerate(SCOPES)}
SECRET_PREFIX = "cwk_"
NAME_MAX = 64
#: ``last_used_at`` is written at most this often per key, so a busy key does not rewrite
#: the file on every request.
LAST_USED_WRITE_INTERVAL_SECS = 300
SCHEMA_VERSION = 1
_HEADER = (
    "// ChatWalaau API keys (PRP-0253). Managed in the API Keys screen or with `chatwalaau keys`.\n"
    "// Only SHA-256 hashes are stored; an issued secret cannot be recovered from this file.\n"
)

# Authentication outcomes.
OK = "ok"
INVALID = "invalid"
EXPIRED = "expired"
DISABLED = "disabled"


class KeyStoreError(Exception):
    """Base of the store's caller-facing errors."""


class KeyNotFound(KeyStoreError):
    pass


class DuplicateKeyName(KeyStoreError):
    pass


class InvalidKeyInput(KeyStoreError, ValueError):
    pass


class StoreUnavailable(KeyStoreError):
    """The store has no path, or its file cannot be written."""


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def scope_allows(granted: str | None, required: str) -> bool:
    """True when ``granted`` is at least ``required`` (responses < api < admin)."""
    return SCOPE_RANK.get(granted or "", -1) >= SCOPE_RANK[required]


def now_utc() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    """Parse an ISO-8601 time; a naive value is UTC. ``None`` / empty -> ``None``."""
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise InvalidKeyInput(f"Invalid time: {value!r}. Use ISO-8601, for example 2027-01-31T00:00:00Z.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class KeyRecord:
    id: str
    name: str
    scope: str
    hash: str
    prefix: str
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    expires_at: str | None = None
    last_used_at: str | None = None
    created_by: str = ""

    def expired(self, at: datetime | None = None) -> bool:
        expiry = parse_time(self.expires_at) if self.expires_at else None
        return expiry is not None and expiry <= (at or now_utc())

    def active(self, at: datetime | None = None) -> bool:
        return self.enabled and not self.expired(at)

    def public(self) -> dict[str, Any]:
        """The record as callers may see it: never the hash."""
        data = asdict(self)
        data.pop("hash")
        data["expired"] = self.expired()
        return data


def _new_secret() -> tuple[str, str]:
    key_id = base64.b32encode(secrets.token_bytes(5)).decode("ascii").lower()
    return key_id, f"{SECRET_PREFIX}{key_id}_{secrets.token_urlsafe(32)}"


def _prefix(secret: str, key_id: str) -> str:
    """``cwk_<id>_`` plus the first 4 random characters -- display only."""
    return secret[: len(SECRET_PREFIX) + len(key_id) + 1 + 4]


def _strip_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments outside JSON strings."""
    out: list[str] = []
    i, n, in_string = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _record_from(raw: dict[str, Any]) -> KeyRecord:
    fields = {k: raw[k] for k in KeyRecord.__dataclass_fields__ if k in raw}
    record = KeyRecord(**fields)
    if record.scope not in SCOPES or not record.id or not record.hash:
        raise ValueError(f"bad key record {record.id!r}")
    return record


def read_records(path: Path) -> list[KeyRecord]:
    """Parse ``path``; raises on an invalid file. A missing file is ``[]``."""
    if not path.is_file():
        return []
    data = json.loads(_strip_comments(path.read_text(encoding="utf-8")) or "{}")
    if not isinstance(data, dict):
        raise ValueError("the key store must be a JSON object")
    return [_record_from(item) for item in data.get("keys") or []]


def count_active_keys(path: str | Path | None) -> int:
    """Active (enabled, unexpired) keys in the file at ``path``; 0 when absent or invalid."""
    if not path:
        return 0
    try:
        return sum(1 for record in read_records(Path(path)) if record.active())
    except Exception:
        return 0


class KeyStore:
    """One API key file. Thread-safe within a process."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._stamp: tuple[int, int] | None = None
        self._records: dict[str, KeyRecord] = {}
        self._by_hash: dict[str, str] = {}

    # ---- reading ---------------------------------------------------------------

    @property
    def audit_path(self) -> Path | None:
        if self.path is None:
            return None
        return self.path.with_name(f"{self.path.stem}.audit.jsonl")

    def _file_stamp(self) -> tuple[int, int] | None:
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _refresh(self) -> None:
        stamp = self._file_stamp()
        if stamp == self._stamp and (stamp is not None or not self._records):
            return
        records: list[KeyRecord] = []
        if stamp is not None and self.path is not None:
            try:
                records = read_records(self.path)
            except Exception:
                logger.exception(
                    "API key store %s is unreadable or invalid; no API key is accepted until it is fixed.",
                    self.path,
                )
                records = []
        self._records = {r.id: r for r in records}
        self._by_hash = {r.hash: r.id for r in records}
        self._stamp = stamp

    def list(self) -> list[KeyRecord]:
        with self._lock:
            self._refresh()
            return sorted(self._records.values(), key=lambda r: (r.created_at, r.name))

    def get(self, key_id: str) -> KeyRecord:
        with self._lock:
            self._refresh()
            record = self._records.get(key_id)
            if record is None:
                raise KeyNotFound(key_id)
            return record

    def has_active(self) -> bool:
        with self._lock:
            self._refresh()
            moment = now_utc()
            return any(r.active(moment) for r in self._records.values())

    def authenticate(self, secret: str | None) -> tuple[str, KeyRecord | None]:
        """``(outcome, record)``; ``record`` only when the outcome is ``ok``."""
        if not secret:
            return INVALID, None
        digest = hash_secret(secret)
        with self._lock:
            self._refresh()
            key_id = self._by_hash.get(digest)
            record = self._records.get(key_id) if key_id else None
        if record is None or not hmac.compare_digest(record.hash, digest):
            return INVALID, None
        if not record.enabled:
            return DISABLED, None
        if record.expired():
            return EXPIRED, None
        self._touch(record)
        return OK, record

    # ---- writing ---------------------------------------------------------------

    def _write(self, records: list[KeyRecord]) -> None:
        if self.path is None:
            raise StoreUnavailable("API_KEYS_FILE is empty, so managed API keys are off.")
        payload = {"schema_version": SCHEMA_VERSION, "keys": [asdict(r) for r in records]}
        body = _HEADER + json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_bytes(body.encode("utf-8"))
            with contextlib.suppress(OSError):
                tmp.chmod(0o600)
            tmp.replace(self.path)
        except OSError as exc:
            raise StoreUnavailable(f"Cannot write the API key store {self.path}: {exc}") from exc
        self._records = {r.id: r for r in records}
        self._by_hash = {r.hash: r.id for r in records}
        self._stamp = self._file_stamp()

    def _audit(self, action: str, record: KeyRecord, actor: str) -> None:
        path = self.audit_path
        if path is None:
            return
        line = {
            "time": _iso(now_utc()),
            "actor": actor,
            "action": action,
            "key_id": record.id,
            "name": record.name,
            "scope": record.scope,
        }
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("API key audit log %s could not be written", path, exc_info=True)

    def _check_name(self, name: str, *, ignore_id: str | None = None) -> str:
        clean = (name or "").strip()
        if not 1 <= len(clean) <= NAME_MAX:
            raise InvalidKeyInput(f"The name must be 1 to {NAME_MAX} characters.")
        for record in self._records.values():
            if record.id != ignore_id and record.name.casefold() == clean.casefold():
                raise DuplicateKeyName(f"A key named {clean!r} already exists.")
        return clean

    @staticmethod
    def _check_scope(scope: str) -> str:
        if scope not in SCOPES:
            raise InvalidKeyInput(f"The scope must be one of {', '.join(SCOPES)}.")
        return scope

    @staticmethod
    def _check_expiry(expires_at: str | None) -> str | None:
        moment = parse_time(expires_at)
        if moment is None:
            return None
        if moment <= now_utc():
            raise InvalidKeyInput("The expiry must be in the future.")
        return _iso(moment)

    def create(self, *, name: str, scope: str, expires_at: str | None, actor: str) -> tuple[KeyRecord, str]:
        with self._lock:
            self._refresh()
            clean = self._check_name(name)
            key_id, secret = _new_secret()
            while key_id in self._records:
                key_id, secret = _new_secret()
            stamp = _iso(now_utc())
            record = KeyRecord(
                id=key_id,
                name=clean,
                scope=self._check_scope(scope),
                hash=hash_secret(secret),
                prefix=_prefix(secret, key_id),
                enabled=True,
                created_at=stamp,
                updated_at=stamp,
                expires_at=self._check_expiry(expires_at),
                created_by=actor,
            )
            self._write([*self._records.values(), record])
            self._audit("create", record, actor)
            return record, secret

    _UNSET: Any = object()

    def update(
        self,
        key_id: str,
        *,
        actor: str,
        name: Any = _UNSET,
        scope: Any = _UNSET,
        expires_at: Any = _UNSET,
        enabled: Any = _UNSET,
    ) -> KeyRecord:
        with self._lock:
            record = self.get(key_id)
            changes: dict[str, Any] = {}
            if name is not self._UNSET:
                changes["name"] = self._check_name(name, ignore_id=key_id)
            if scope is not self._UNSET:
                changes["scope"] = self._check_scope(scope)
            if expires_at is not self._UNSET:
                changes["expires_at"] = self._check_expiry(expires_at)
            if enabled is not self._UNSET:
                changes["enabled"] = bool(enabled)
            if not changes:
                return record
            updated = replace(record, **changes, updated_at=_iso(now_utc()))
            self._write([updated if r.id == key_id else r for r in self._records.values()])
            if "enabled" in changes and changes["enabled"] != record.enabled:
                self._audit("enable" if updated.enabled else "disable", updated, actor)
            if set(changes) - {"enabled"}:
                self._audit("update", updated, actor)
            return updated

    def reissue(self, key_id: str, *, actor: str) -> tuple[KeyRecord, str]:
        with self._lock:
            record = self.get(key_id)
            # The id stays; only the random part is new (D5).
            secret = f"{SECRET_PREFIX}{record.id}_{secrets.token_urlsafe(32)}"
            updated = replace(
                record,
                hash=hash_secret(secret),
                prefix=_prefix(secret, record.id),
                updated_at=_iso(now_utc()),
                last_used_at=None,
            )
            self._write([updated if r.id == key_id else r for r in self._records.values()])
            self._audit("reissue", updated, actor)
            return updated, secret

    def delete(self, key_id: str, *, actor: str) -> None:
        with self._lock:
            record = self.get(key_id)
            self._write([r for r in self._records.values() if r.id != key_id])
            self._audit("delete", record, actor)

    def _touch(self, record: KeyRecord) -> None:
        """Record use, at most once per LAST_USED_WRITE_INTERVAL_SECS per key. Never raises."""
        moment = now_utc()
        last = parse_time(record.last_used_at) if record.last_used_at else None
        if last is not None and (moment - last).total_seconds() < LAST_USED_WRITE_INTERVAL_SECS:
            return
        try:
            with self._lock:
                self._refresh()
                current = self._records.get(record.id)
                if current is None or current.hash != record.hash:
                    return
                touched = replace(current, last_used_at=_iso(moment))
                self._write([touched if r.id == record.id else r for r in self._records.values()])
        except Exception:
            logger.debug("API key last_used_at not written for %s", record.id, exc_info=True)


_stores: dict[str, KeyStore] = {}
_stores_lock = threading.Lock()


def store_for(path: str | Path | None) -> KeyStore:
    """The process-wide store for ``path`` (``None`` / empty -> a store with no keys)."""
    key = str(Path(path).resolve()) if path else ""
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = KeyStore(Path(key) if key else None)
            _stores[key] = store
        return store


def get_store() -> KeyStore:
    """The store named by the live ``settings.api_keys_file``."""
    from app.core import config

    return store_for(getattr(config.settings, "api_keys_file", "") or None)
