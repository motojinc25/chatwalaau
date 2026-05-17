"""Password hashing for the web SPA authentication lane (CTR-0093, PRP-0057).

Uses Python stdlib ``hashlib.scrypt`` -- no new package dependency.

Hash format (single-line, deliberately small and grep-able):

    scrypt$N=<n>,r=<r>,p=<p>$<base64-salt>$<base64-hash>

Constraints validated by ``parse_password_hash``:

- scheme prefix exactly ``scrypt$``
- ``N`` integer in [1024, 1048576] AND a power of two
- ``r`` integer in [1, 32]
- ``p`` integer in [1, 16]
- salt base64-url-decodable, 8..32 bytes
- hash base64-url-decodable, 32..128 bytes

The reference parameters used by the operator-side recipe are
``N=16384, r=8, p=1, salt=16 bytes, hash=64 bytes``. ``verify_password``
is intentionally written so the cost of a mismatch is the same as the
cost of a match (constant-time bytes compare on the derived key).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac

_SCHEME = "scrypt"
_N_MIN = 1024
_N_MAX = 1_048_576
_R_MIN = 1
_R_MAX = 32
_P_MIN = 1
_P_MAX = 16
_SALT_MIN = 8
_SALT_MAX = 32
_HASH_MIN = 32
_HASH_MAX = 128


@dataclass(frozen=True)
class ParsedPasswordHash:
    """A successfully parsed scrypt password hash."""

    n: int
    r: int
    p: int
    salt: bytes
    digest: bytes
    dklen: int  # equals len(digest); kept for clarity at the call site


def _b64decode(value: str) -> bytes:
    """Decode standard base64 with padding tolerance."""
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, validate=True)


def _is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def _parse_params(params_segment: str) -> tuple[int, int, int]:
    """Parse the ``N=...,r=...,p=...`` segment."""
    pairs: dict[str, str] = {}
    for entry in params_segment.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            msg = f"Malformed scrypt parameter segment: {params_segment!r}"
            raise ValueError(msg)
        key, value = entry.split("=", 1)
        pairs[key.strip()] = value.strip()

    if set(pairs.keys()) != {"N", "r", "p"}:
        msg = f"scrypt parameter keys must be exactly N, r, p; got: {sorted(pairs.keys())}"
        raise ValueError(msg)

    try:
        n = int(pairs["N"])
        r = int(pairs["r"])
        p = int(pairs["p"])
    except ValueError as exc:
        msg = f"scrypt parameters must be integers: {params_segment!r}"
        raise ValueError(msg) from exc

    if not (_N_MIN <= n <= _N_MAX) or not _is_power_of_two(n):
        msg = f"scrypt N must be a power of two in [{_N_MIN}, {_N_MAX}]; got {n}"
        raise ValueError(msg)
    if not (_R_MIN <= r <= _R_MAX):
        msg = f"scrypt r must be in [{_R_MIN}, {_R_MAX}]; got {r}"
        raise ValueError(msg)
    if not (_P_MIN <= p <= _P_MAX):
        msg = f"scrypt p must be in [{_P_MIN}, {_P_MAX}]; got {p}"
        raise ValueError(msg)
    return n, r, p


def parse_password_hash(encoded: str) -> ParsedPasswordHash:
    """Parse a stored password hash string.

    Raises ``ValueError`` on any malformed input. The string is treated
    as authoritative for ``N`` / ``r`` / ``p`` so re-hashing for verify
    uses the parameters originally chosen by the operator.
    """
    if not encoded or not isinstance(encoded, str):
        msg = "Password hash must be a non-empty string"
        raise ValueError(msg)

    parts = encoded.strip().split("$")
    if len(parts) != 4:
        msg = "Password hash must have exactly 4 $-separated segments: scrypt$N=...,r=...,p=...$<salt>$<hash>"
        raise ValueError(msg)

    scheme, params_segment, salt_b64, digest_b64 = parts
    if scheme != _SCHEME:
        msg = f"Password hash scheme must be {_SCHEME!r}; got {scheme!r}"
        raise ValueError(msg)

    n, r, p = _parse_params(params_segment)

    try:
        salt = _b64decode(salt_b64)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        msg = "Password hash salt is not valid base64"
        raise ValueError(msg) from exc

    if not (_SALT_MIN <= len(salt) <= _SALT_MAX):
        msg = f"Password hash salt must decode to [{_SALT_MIN}, {_SALT_MAX}] bytes; got {len(salt)}"
        raise ValueError(msg)

    try:
        digest = _b64decode(digest_b64)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        msg = "Password hash digest is not valid base64"
        raise ValueError(msg) from exc

    if not (_HASH_MIN <= len(digest) <= _HASH_MAX):
        msg = f"Password hash digest must decode to [{_HASH_MIN}, {_HASH_MAX}] bytes; got {len(digest)}"
        raise ValueError(msg)

    return ParsedPasswordHash(n=n, r=r, p=p, salt=salt, digest=digest, dklen=len(digest))


def verify_password(plain: str, parsed: ParsedPasswordHash) -> bool:
    """Constant-time verify ``plain`` against a parsed hash.

    Returns ``False`` on any internal error (e.g. malformed inputs that
    slipped past the parser), never raises.
    """
    if plain is None:
        return False
    try:
        candidate = hashlib.scrypt(
            plain.encode("utf-8"),
            salt=parsed.salt,
            n=parsed.n,
            r=parsed.r,
            p=parsed.p,
            dklen=parsed.dklen,
            maxmem=128 * parsed.n * parsed.r * 2,
        )
    except (ValueError, OverflowError):
        return False
    return hmac.compare_digest(candidate, parsed.digest)


def generate_hash(plain: str, *, n: int = 16384, r: int = 8, p: int = 1, salt_bytes: int = 16, dklen: int = 64) -> str:
    """Produce a hash string for the operator-side recipe / tests.

    Not used at request time; exists so the test suite (and a possible
    future ``chatwalaau hash-password`` CLI subcommand) can produce a
    canonical hash without copy-pasting the format string.
    """
    import secrets

    if salt_bytes < _SALT_MIN or salt_bytes > _SALT_MAX:
        msg = f"salt_bytes must be in [{_SALT_MIN}, {_SALT_MAX}]"
        raise ValueError(msg)
    if dklen < _HASH_MIN or dklen > _HASH_MAX:
        msg = f"dklen must be in [{_HASH_MIN}, {_HASH_MAX}]"
        raise ValueError(msg)
    salt = secrets.token_bytes(salt_bytes)
    digest = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen)
    salt_b64 = base64.b64encode(salt).decode("ascii").rstrip("=")
    digest_b64 = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"{_SCHEME}$N={n},r={r},p={p}${salt_b64}${digest_b64}"


__all__ = ["ParsedPasswordHash", "generate_hash", "parse_password_hash", "verify_password"]
