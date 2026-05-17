"""Process-local opaque session token store (CTR-0095, PRP-0057).

Single in-memory dict guarded by an asyncio lock. No on-disk persistence
in Phase 1; process restart drops all sessions and the SPA re-prompts
on the next 401 via CTR-0096.

Public surface:

- ``SessionToken`` dataclass (token, username, timestamps)
- ``SessionStore`` with ``create`` / ``lookup`` / ``revoke`` /
  ``purge_expired``
- ``get_session_store()`` singleton accessor used by CTR-0083 v4 and
  CTR-0094
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import secrets


@dataclass
class SessionToken:
    """A live session token managed by ``SessionStore``."""

    token: str
    username: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


class SessionStore:
    """Asyncio-safe in-memory session token store.

    All mutations and slide-on-lookup go through a single asyncio lock,
    which is enough for a single-process FastAPI worker handling
    concurrent request handlers. Multi-process scale-out is explicitly
    out of scope for this contract (see UDR-0033).
    """

    def __init__(self, *, ttl_seconds: int) -> None:
        if ttl_seconds < 60:
            msg = "ttl_seconds must be >= 60"
            raise ValueError(msg)
        self._ttl_seconds = ttl_seconds
        self._store: dict[str, SessionToken] = {}
        self._lock = asyncio.Lock()

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
            self._store[token.token] = token
        return token

    async def lookup(self, token_value: str) -> SessionToken | None:
        """Return the session record for ``token_value`` after sliding expiry.

        Returns ``None`` for unknown OR expired tokens; expired tokens
        are removed from the store as a side effect of this call.
        """
        if not token_value:
            return None
        now = datetime.now(UTC)
        async with self._lock:
            record = self._store.get(token_value)
            if record is None:
                return record
            if record.is_expired(now):
                self._store.pop(token_value, None)
                return None
            record.last_used_at = now
            record.expires_at = now + timedelta(seconds=self._ttl_seconds)
            return record

    async def revoke(self, token_value: str) -> None:
        """Delete ``token_value`` from the store. Idempotent."""
        if not token_value:
            return
        async with self._lock:
            self._store.pop(token_value, None)

    async def purge_expired(self) -> int:
        """Remove all expired tokens. Not invoked by any scheduler in Phase 1."""
        now = datetime.now(UTC)
        async with self._lock:
            expired = [t for t, rec in self._store.items() if rec.is_expired(now)]
            for t in expired:
                self._store.pop(t, None)
            return len(expired)

    @property
    def size(self) -> int:
        """Current entry count (for tests / observability; not auth-critical)."""
        return len(self._store)


# Module-level singleton lazily initialised on first access. The
# imported ``settings`` is read at first call so test suites can
# patch ``app.core.config.settings`` before the store is created.
_INSTANCE: SessionStore | None = None
_INSTANCE_LOCK = asyncio.Lock() if False else None  # placeholder; sync access ok


def get_session_store() -> SessionStore:
    """Return the per-process session store, creating it on first call."""
    global _INSTANCE
    if _INSTANCE is None:
        from app.core.config import settings

        _INSTANCE = SessionStore(ttl_seconds=settings.auth_session_ttl_seconds)
    return _INSTANCE


def reset_session_store_for_tests() -> None:
    """Drop the cached singleton. Test-only helper."""
    global _INSTANCE
    _INSTANCE = None


__all__ = [
    "SessionStore",
    "SessionToken",
    "get_session_store",
    "reset_session_store_for_tests",
]
