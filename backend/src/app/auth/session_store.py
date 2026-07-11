"""Opaque session token store with digest-only persistence (CTR-0095 v2).

Single in-memory dict guarded by an asyncio lock, projected to disk as a
JSON file that holds ``sha256(token)`` digests -- never the raw token
(PRP-0110 Part 2, UDR-0089 D2). A disclosed store file therefore yields
no forgeable cookie: the digest cannot be inverted, and a 256-bit random
token has no brute-force surface (which is why a plain hash is correct
here and a slow KDF is not -- contrast ``AUTH_PASSWORD_HASH``, which
protects a human-chosen password and uses scrypt).

Durability rules (UDR-0089):

- D3 write-behind: ``create`` / ``revoke`` flush synchronously; a TTL
  slide performed by ``lookup`` only marks the store dirty and is
  flushed by ``flush_if_dirty`` (lifespan shutdown, or an opportunistic
  caller). A crash between flushes can only lose recent slides, so a
  session expires EARLIER than intended -- never later.
- D4 tolerant load + atomic write: a malformed file is quarantined to
  ``<path>.corrupt-<timestamp>`` and the store starts empty rather than
  raising. Every flush writes a temp file in the same directory, fsyncs,
  and ``os.replace``s it into position.
- D5 credential fingerprint: a change to ``AUTH_USERNAME`` /
  ``AUTH_PASSWORD_HASH`` discards every persisted record, so rotating
  the password signs all sessions out.
- D7 opt-out: ``AUTH_SESSION_PERSIST=false`` restores the pre-PRP-0110
  in-memory-only behavior byte-for-byte.
- D8 single-process: the file is a restart-survival mechanism, NOT a
  multi-process synchronization mechanism.

Public surface (unchanged for callers -- ``create`` still returns the raw
token, ``lookup`` / ``revoke`` still take the raw token):

- ``SessionToken`` dataclass (token, username, timestamps)
- ``SessionStore`` with ``create`` / ``lookup`` / ``revoke`` /
  ``purge_expired``
- ``get_session_store()`` singleton accessor used by CTR-0083 v4 and
  CTR-0094
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import tempfile

_logger = logging.getLogger(__name__)

_STORE_SCHEMA_VERSION = 1


def digest_token(token_value: str) -> str:
    """Return the on-disk / in-memory key for ``token_value``.

    UDR-0089 D2: sha256, NOT a slow KDF. The token is 256 bits of
    ``secrets.token_urlsafe(32)`` entropy, so there is no offline
    brute-force surface to defend, and this runs on the hot path of every
    authenticated request.
    """
    return hashlib.sha256(token_value.encode("utf-8")).hexdigest()


def credential_fingerprint(username: str, password_hash: str) -> str:
    """Fingerprint of the credential config (UDR-0089 D5).

    A mismatch on load discards every persisted record, so rotating the
    password hash signs out all sessions.
    """
    return hashlib.sha256(f"{username}:{password_hash}".encode()).hexdigest()


@dataclass
class SessionToken:
    """A live session token managed by ``SessionStore``.

    ``token`` holds the RAW token in memory only. It is never serialised:
    ``SessionStore._serialise`` writes ``digest`` instead (UDR-0089 D2).
    A record rehydrated from disk carries ``token=""`` because the raw
    value is unrecoverable by design.
    """

    token: str
    username: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


class SessionStore:
    """Asyncio-safe session token store with digest-only persistence.

    All mutations and slide-on-lookup go through a single asyncio lock,
    which is enough for a single-process FastAPI worker handling
    concurrent request handlers. Multi-process scale-out is explicitly
    out of scope for this contract (UDR-0033, UDR-0089 D8).
    """

    def __init__(
        self,
        *,
        ttl_seconds: int,
        store_path: str | os.PathLike[str] | None = None,
        fingerprint: str = "",
    ) -> None:
        if ttl_seconds < 60:
            msg = "ttl_seconds must be >= 60"
            raise ValueError(msg)
        self._ttl_seconds = ttl_seconds
        # Keyed by digest_token(raw). The raw token exists only inside the
        # request that carries it and inside the browser's cookie jar.
        self._store: dict[str, SessionToken] = {}
        self._lock = asyncio.Lock()
        self._path: Path | None = Path(store_path) if store_path else None
        self._fingerprint = fingerprint
        self._dirty = False

    # ---- Persistence (UDR-0089 D2/D3/D4/D5/D6) ----

    @property
    def persistent(self) -> bool:
        return self._path is not None

    def _serialise(self) -> dict[str, object]:
        return {
            "version": _STORE_SCHEMA_VERSION,
            "fingerprint": self._fingerprint,
            "tokens": [
                {
                    "digest": key,
                    "username": rec.username,
                    "created_at": rec.created_at.isoformat(),
                    "last_used_at": rec.last_used_at.isoformat(),
                    "expires_at": rec.expires_at.isoformat(),
                }
                for key, rec in self._store.items()
            ],
        }

    def _quarantine(self, reason: str) -> None:
        """Move an unusable store file aside; never raise (UDR-0089 D4)."""
        if self._path is None or not self._path.exists():
            return
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        target = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        try:
            self._path.replace(target)
            _logger.warning("session token store %s (%s); quarantined to %s", self._path, reason, target.name)
        except OSError as exc:  # pragma: no cover - filesystem edge
            _logger.warning("session token store %s (%s); quarantine failed: %s", self._path, reason, exc)

    def load(self) -> None:
        """Rehydrate from disk. Tolerant: never raises (UDR-0089 D4).

        Drops already-expired records, and discards EVERY record when the
        credential fingerprint does not match the running configuration
        (UDR-0089 D5).
        """
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._quarantine("unreadable or malformed")
            return
        if not isinstance(raw, dict) or not isinstance(raw.get("tokens"), list):
            self._quarantine("unexpected shape")
            return
        if raw.get("fingerprint") != self._fingerprint:
            _logger.info("session token store discarded: credential fingerprint changed (UDR-0089 D5)")
            self._store.clear()
            self._dirty = True
            return

        now = datetime.now(UTC)
        loaded = 0
        for entry in raw["tokens"]:
            if not isinstance(entry, dict):
                continue
            try:
                record = SessionToken(
                    token="",  # unrecoverable by design (UDR-0089 D2)
                    username=str(entry["username"]),
                    created_at=datetime.fromisoformat(str(entry["created_at"])),
                    last_used_at=datetime.fromisoformat(str(entry["last_used_at"])),
                    expires_at=datetime.fromisoformat(str(entry["expires_at"])),
                )
                key = str(entry["digest"])
            except (KeyError, TypeError, ValueError):
                continue
            if not key or record.is_expired(now):
                continue
            self._store[key] = record
            loaded += 1
        _logger.info("session token store loaded: %d live session(s)", loaded)

    def _flush_locked(self) -> None:
        """Atomically write the digest projection. Never raises."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), prefix=".session-tokens-", suffix=".tmp")
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._serialise(), handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                tmp_path.replace(self._path)
            except BaseException:
                # Never leave a temp file behind on a partial write.
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
                raise
            # POSIX only; Windows relies on the containing directory ACL.
            with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
                self._path.chmod(0o600)
            self._dirty = False
        except OSError as exc:
            # A read-only or full filesystem must never break authentication.
            _logger.warning("session token store flush failed: %s", exc)

    async def flush_if_dirty(self) -> None:
        """Flush pending TTL slides (lifespan shutdown; UDR-0089 D3)."""
        if self._path is None:
            return
        async with self._lock:
            if self._dirty:
                self._flush_locked()

    # ---- Public surface (unchanged for callers) ----

    def _make_token_value(self) -> str:
        # 32 bytes -> 256-bit entropy. ``token_urlsafe`` returns a
        # base64url-encoded string with no padding, safe to use as a
        # cookie value with no further escaping.
        return secrets.token_urlsafe(32)

    async def create(self, username: str) -> SessionToken:
        """Issue a new session token for ``username``."""
        if not username:
            msg = "username must be non-empty"
            raise ValueError(msg)
        now = datetime.now(UTC)
        token = SessionToken(
            token=self._make_token_value(),
            username=username,
            created_at=now,
            last_used_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        async with self._lock:
            # Token collision probability with 256-bit random is
            # negligible; we do not loop on collision.
            self._store[digest_token(token.token)] = token
            self._dirty = True
            self._flush_locked()  # security-relevant transition (UDR-0089 D3)
        return token

    async def lookup(self, token_value: str) -> SessionToken | None:
        """Return the session record for ``token_value`` after sliding expiry.

        Returns ``None`` for unknown OR expired tokens; expired tokens
        are removed from the store as a side effect of this call.

        The slide marks the store dirty but does NOT flush: ``lookup``
        runs on every authenticated request, and losing a slide can only
        expire a session early (UDR-0089 D3).
        """
        if not token_value:
            return None
        key = digest_token(token_value)
        now = datetime.now(UTC)
        async with self._lock:
            record = self._store.get(key)
            if record is None:
                return record
            if record.is_expired(now):
                self._store.pop(key, None)
                self._dirty = True
                return None
            record.last_used_at = now
            record.expires_at = now + timedelta(seconds=self._ttl_seconds)
            self._dirty = True
            return record

    async def revoke(self, token_value: str) -> None:
        """Delete ``token_value`` from the store and from disk. Idempotent."""
        if not token_value:
            return
        async with self._lock:
            removed = self._store.pop(digest_token(token_value), None)
            if removed is not None:
                self._dirty = True
                self._flush_locked()  # security-relevant transition (UDR-0089 D3)

    async def purge_expired(self) -> int:
        """Remove all expired tokens."""
        now = datetime.now(UTC)
        async with self._lock:
            expired = [key for key, rec in self._store.items() if rec.is_expired(now)]
            for key in expired:
                self._store.pop(key, None)
            if expired:
                self._dirty = True
                self._flush_locked()
            return len(expired)

    @property
    def size(self) -> int:
        """Current entry count (for tests / observability; not auth-critical)."""
        return len(self._store)


# Module-level singleton lazily initialised on first access. The
# imported ``settings`` is read at first call so test suites can
# patch ``app.core.config.settings`` before the store is created.
_INSTANCE: SessionStore | None = None


def get_session_store() -> SessionStore:
    """Return the per-process session store, creating it on first call.

    The persistence lane is enabled only when the web auth lane exists
    (``AUTH_USERNAME`` set) AND ``AUTH_SESSION_PERSIST`` is true. Otherwise
    no path is configured and nothing is ever read or written
    (UDR-0089 D7).
    """
    global _INSTANCE
    if _INSTANCE is None:
        from app.core.config import settings

        persist = bool(settings.web_auth_enabled and settings.auth_session_persist)
        _INSTANCE = SessionStore(
            ttl_seconds=settings.auth_session_ttl_seconds,
            store_path=settings.auth_session_store_path if persist else None,
            fingerprint=(
                credential_fingerprint(settings.auth_username, settings.auth_password_hash) if persist else ""
            ),
        )
        if persist:
            _INSTANCE.load()
    return _INSTANCE


def reset_session_store_for_tests() -> None:
    """Drop the cached singleton. Test-only helper."""
    global _INSTANCE
    _INSTANCE = None


__all__ = [
    "SessionStore",
    "SessionToken",
    "credential_fingerprint",
    "digest_token",
    "get_session_store",
    "reset_session_store_for_tests",
]
