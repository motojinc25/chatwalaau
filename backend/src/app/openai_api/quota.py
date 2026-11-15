"""Responses Quota Guard (CTR-0285, PRP-0254, UDR-0235).

Admission limits for ``POST /v1/responses`` only (D1). Nothing here applies to another lane.

- **Admission, not interruption** (D3). A request is refused with 429 when a window has
  already REACHED a limit (``used >= limit``). A running turn is never cut, so the turns in
  flight when a token limit is reached may finish above it; ``responses_max_concurrent``
  and the per-call output cap bound that overshoot.
- **Requests and tokens, never money** (D2). Input = uncached + cache creation (+ cache
  reads when ``responses_count_cache_reads``). Output = ``output_token_count``, which
  already holds reasoning. Total = input + output.
- **Global limits** are App Settings (group ``openai_api_limits``, D4); **per-key limits**
  live on the key record (``limits``, D5). Both apply.
- **Windows** (D6): a sliding 60 s for per-minute; calendar day and month in
  ``responses_limit_timezone``. A counter whose period key is stale reads as zero, so no
  job resets anything.
- **Counters are in memory** (D7), rebuilt from the ledger's ``openai-api`` records when
  counting starts and when the time zone changes. Tokens are added at turn end, before and
  independently of the ledger append, so a failed append (UDR-0136 D6) never lets traffic
  past a limit. While no limit at all is configured the guard counts nothing and the next
  activation rebuilds from the ledger -- the ledger is the truth for that period.
- **Per process** (D10), like the ledger lock: under several workers or instances each
  enforces its own counters.
- The 429 body is the OpenAI error shape and never names the key (D8).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
import json
import logging
import math
import threading
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core import config as _config

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)

LANE = "openai-api"
WARN_RATIO = 0.8
MINUTE_SECS = 60.0

#: Global limits: code -> (setting, window, metric). Window: minute | day | month | live.
GLOBAL_LIMITS: dict[str, tuple[str, str, str]] = {
    "requests_per_minute": ("responses_requests_per_minute", "minute", "requests"),
    "requests_per_day": ("responses_requests_per_day", "day", "requests"),
    "requests_per_month": ("responses_requests_per_month", "month", "requests"),
    "input_tokens_per_day": ("responses_input_tokens_per_day", "day", "input"),
    "input_tokens_per_month": ("responses_input_tokens_per_month", "month", "input"),
    "output_tokens_per_day": ("responses_output_tokens_per_day", "day", "output"),
    "output_tokens_per_month": ("responses_output_tokens_per_month", "month", "output"),
    "total_tokens_per_day": ("responses_total_tokens_per_day", "day", "total"),
    "total_tokens_per_month": ("responses_total_tokens_per_month", "month", "total"),
}
CONCURRENCY_CODE = "concurrent_requests"
CONCURRENCY_SETTING = "responses_max_concurrent"
INPUT_CHARS_SETTING = "responses_max_input_chars"

#: Per-key limits: key record field -> (window, metric). Codes are ``key_<field>``.
KEY_LIMITS: dict[str, tuple[str, str]] = {
    "requests_per_minute": ("minute", "requests"),
    "requests_per_day": ("day", "requests"),
    "requests_per_month": ("month", "requests"),
    "total_tokens_per_day": ("day", "total"),
    "total_tokens_per_month": ("month", "total"),
}

_WINDOW_WORD = {"minute": "per-minute", "day": "daily", "month": "monthly"}
_METRIC_WORD = {
    "requests": "request",
    "input": "input token",
    "output": "output token",
    "total": "total token",
}


class QuotaError(Exception):
    """A refusal in the OpenAI error shape (D8). Carries no key name, id or secret."""

    def __init__(
        self, *, status: int, error_type: str, code: str, message: str, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def body(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": self.error_type, "code": self.code, "param": None}}

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}


def limit_message(code: str) -> str:
    """The refusal text for ``code``. Built from the code alone, never from a key (D8)."""
    if code == CONCURRENCY_CODE:
        return "Too many concurrent requests to /v1/responses. Retry shortly."
    per_key = code.startswith("key_")
    base = code[4:] if per_key else code
    window, metric = KEY_LIMITS[base] if per_key else GLOBAL_LIMITS[base][1:]
    owner = "This API key's" if per_key else "The"
    return f"{owner} {_WINDOW_WORD[window]} {_METRIC_WORD[metric]} limit for /v1/responses has been reached."


# ---- settings and clock ------------------------------------------------------------


def _setting(name: str, default: Any) -> Any:
    return getattr(_config.settings, name, default)


def _int_setting(name: str) -> int:
    try:
        value = int(_setting(name, 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def timezone_name() -> str:
    name = str(_setting("responses_limit_timezone", "UTC") or "UTC").strip() or "UTC"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return name


def count_cache_reads() -> bool:
    return bool(_setting("responses_count_cache_reads", True))


def now() -> datetime:
    """The guard's clock (patched by tests)."""
    return datetime.now(UTC)


def _zone(name: str) -> tzinfo:
    return UTC if name == "UTC" else ZoneInfo(name)


def _period_keys(moment: datetime, zone: tzinfo) -> tuple[str, str]:
    local = moment.astimezone(zone)
    return local.date().isoformat(), f"{local:%Y-%m}"


def _seconds_to_next_day(moment: datetime, zone: tzinfo) -> int:
    local = moment.astimezone(zone)
    nxt = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    return max(1, math.ceil((nxt - local).total_seconds()))


def _seconds_to_next_month(moment: datetime, zone: tzinfo) -> int:
    local = moment.astimezone(zone)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    nxt = datetime(year, month, 1, tzinfo=zone)
    return max(1, math.ceil((nxt - local).total_seconds()))


# ---- token arithmetic -----------------------------------------------------------------


def _num(source: Mapping[str, Any] | None, key: str) -> int:
    value = (source or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def tokens_of(turn: Mapping[str, Any] | None, *, cache_reads: bool) -> tuple[int, int]:
    """``(input, output)`` of a ledger record or a turn summary (D2)."""
    if not turn:
        return 0, 0
    # A turn summary omits the uncached figure when no cache read was reported: the
    # reported input then already IS the uncached count (UDR-0135; ledger.build_record).
    base = "uncached_input_token_count" if "uncached_input_token_count" in turn else "input_token_count"
    input_tokens = _num(turn, base) + _num(turn, "cache_creation_input_token_count")
    if cache_reads:
        input_tokens += _num(turn, "cache_read_input_token_count")
    return input_tokens, _num(turn, "output_token_count")


# ---- input size -------------------------------------------------------------------------


def _text_chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_chars(part) for part in value)
    if isinstance(value, dict):
        total = 0
        for key in ("text", "content", "output", "arguments"):
            if key in value:
                total += _text_chars(value[key])
        if "contents" in value:
            total += _text_chars(value["contents"])
        return total
    return 0


def check_input_size(input_data: Any, history_thread_id: str | None = None) -> None:
    """400 ``input_too_large`` when the input text (plus stored history) exceeds the cap."""
    cap = _int_setting(INPUT_CHARS_SETTING)
    if cap <= 0:
        return
    chars = _text_chars(input_data)
    if history_thread_id:
        from app.openai_api.session import _read_session

        session = _read_session(history_thread_id) or {}
        chars += _text_chars(session.get("messages") or [])
    if chars > cap:
        raise QuotaError(
            status=400,
            error_type="invalid_request_error",
            code="input_too_large",
            message=(
                f"The input is {chars} characters, above this server's limit of {cap} for /v1/responses "
                "(stored history included)."
            ),
        )


# ---- counters ---------------------------------------------------------------------------


@dataclass
class _Window:
    period: str = ""
    requests: int = 0
    input: int = 0
    output: int = 0

    def roll(self, period: str) -> None:
        if self.period != period:
            self.period, self.requests, self.input, self.output = period, 0, 0, 0

    def get(self, metric: str) -> int:
        if metric == "total":
            return self.input + self.output
        return int(getattr(self, metric))


@dataclass
class _Counters:
    day: _Window = field(default_factory=_Window)
    month: _Window = field(default_factory=_Window)
    minute: deque[float] = field(default_factory=deque)

    def roll(self, day_key: str, month_key: str, at: float) -> None:
        self.day.roll(day_key)
        self.month.roll(month_key)
        while self.minute and self.minute[0] <= at - MINUTE_SECS:
            self.minute.popleft()

    def used(self, window: str, metric: str) -> int:
        if window == "minute":
            return len(self.minute)
        return (self.day if window == "day" else self.month).get(metric)

    def add_request(self, at: float) -> None:
        self.minute.append(at)
        self.day.requests += 1
        self.month.requests += 1

    def add_tokens(self, input_tokens: int, output_tokens: int) -> None:
        for window in (self.day, self.month):
            window.input += input_tokens
            window.output += output_tokens


@dataclass
class Ticket:
    """One admitted request. ``finish`` is idempotent and safe from a ``finally``."""

    guard: QuotaGuard | None
    key_id: str | None
    counted: bool
    turn: dict[str, Any] | None = None
    done: bool = False

    def observe(self, turn: Mapping[str, Any] | None) -> None:
        """Remember the latest turn total (a ledger-shaped summary)."""
        if turn:
            self.turn = dict(turn)

    def finish(self, turn: Mapping[str, Any] | None = None) -> None:
        if turn:
            self.observe(turn)
        if self.done:
            return
        self.done = True
        if self.guard is not None:
            self.guard._finish(self)


def _key_limits_of(record: Any) -> dict[str, int]:
    limits = getattr(record, "limits", None) or {}
    return {k: int(v) for k, v in limits.items() if k in KEY_LIMITS and isinstance(v, int) and v > 0}


def _any_key_limits() -> bool:
    try:
        from app.api_keys.store import get_store

        return any(_key_limits_of(record) for record in get_store().list())
    except Exception:
        logger.debug("quota: key store not readable for limits", exc_info=True)
        return False


def _global_limits() -> dict[str, int]:
    return {code: value for code, (setting, _w, _m) in GLOBAL_LIMITS.items() if (value := _int_setting(setting)) > 0}


class QuotaGuard:
    """The process-wide guard. Every public method is thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded_tz: str | None = None
        self._global = _Counters()
        self._keys: dict[str, _Counters] = {}
        self._in_flight = 0
        self._warned: set[tuple[str, str]] = set()

    # -- state ---------------------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._loaded_tz = None
            self._global = _Counters()
            self._keys = {}
            self._in_flight = 0
            self._warned = set()

    def _counters(self, key_id: str | None) -> _Counters:
        if key_id is None:
            return self._global
        counters = self._keys.get(key_id)
        if counters is None:
            counters = self._keys[key_id] = _Counters()
            # Born in the current periods, or the next roll would zero what it counts.
            counters.day.period = self._global.day.period
            counters.month.period = self._global.month.period
        return counters

    def _rebuild(self, tz_name: str, moment: datetime) -> None:
        """Day and month counters from the ledger (D7). Keeps the minute windows."""
        zone = _zone(tz_name)
        day_key, month_key = _period_keys(moment, zone)
        cache_reads = count_cache_reads()
        fresh_global = _Counters(minute=self._global.minute)
        fresh_keys: dict[str, _Counters] = {k: _Counters(minute=c.minute) for k, c in self._keys.items()}
        fresh_global.roll(day_key, month_key, moment.timestamp())
        for counters in fresh_keys.values():
            counters.roll(day_key, month_key, moment.timestamp())
        for record in _ledger_records(moment):
            local = _record_local(record, zone)
            if local is None or f"{local:%Y-%m}" != month_key:
                continue
            same_day = local.date().isoformat() == day_key
            input_tokens, output_tokens = tokens_of(record, cache_reads=cache_reads)
            targets = [fresh_global]
            key_id = record.get("key_id")
            if isinstance(key_id, str) and key_id:
                counters = fresh_keys.get(key_id)
                if counters is None:
                    counters = fresh_keys[key_id] = _Counters()
                    counters.roll(day_key, month_key, moment.timestamp())
                targets.append(counters)
            for counters in targets:
                counters.month.requests += 1
                counters.month.input += input_tokens
                counters.month.output += output_tokens
                if same_day:
                    counters.day.requests += 1
                    counters.day.input += input_tokens
                    counters.day.output += output_tokens
        self._global, self._keys = fresh_global, fresh_keys
        self._loaded_tz = tz_name

    def _ensure(self, moment: datetime) -> tzinfo:
        tz_name = timezone_name()
        if self._loaded_tz != tz_name:
            self._rebuild(tz_name, moment)
        zone = _zone(tz_name)
        day_key, month_key = _period_keys(moment, zone)
        at = moment.timestamp()
        self._global.roll(day_key, month_key, at)
        for counters in self._keys.values():
            counters.roll(day_key, month_key, at)
        return zone

    # -- admission ---------------------------------------------------------------------

    def admit(self, key_id: str | None, key_record: Any = None) -> Ticket:
        """Count one request or raise :class:`QuotaError` (429). Runs nothing itself."""
        global_limits = _global_limits()
        concurrency = _int_setting(CONCURRENCY_SETTING)
        key_limits = _key_limits_of(key_record)
        if not global_limits and not concurrency and not key_limits and not _any_key_limits():
            # Nothing is configured: count nothing; the next activation rebuilds (D7).
            with self._lock:
                self._loaded_tz = None
            return Ticket(guard=None, key_id=key_id, counted=False)
        moment = now()
        with self._lock:
            zone = self._ensure(moment)
            if concurrency and self._in_flight >= concurrency:
                raise QuotaError(
                    status=429,
                    error_type="rate_limit_exceeded",
                    code=CONCURRENCY_CODE,
                    message=limit_message(CONCURRENCY_CODE),
                    retry_after=1,
                )
            checks: list[tuple[str, _Counters, str, str, int]] = [
                (code, self._global, window, metric, global_limits[code])
                for code, (_s, window, metric) in GLOBAL_LIMITS.items()
                if code in global_limits
            ]
            if key_id is not None:
                key_counters = self._counters(key_id)
                checks += [
                    (f"key_{name}", key_counters, window, metric, key_limits[name])
                    for name, (window, metric) in KEY_LIMITS.items()
                    if name in key_limits
                ]
            for code, counters, window, metric, limit in checks:
                if counters.used(window, metric) >= limit:
                    raise self._refusal(code, counters, window, moment, zone)
            at = moment.timestamp()
            self._global.add_request(at)
            if key_id is not None:
                self._counters(key_id).add_request(at)
            self._in_flight += 1
            self._warn_after(checks, metric_filter="requests")
        return Ticket(guard=self, key_id=key_id, counted=True)

    def _refusal(self, code: str, counters: _Counters, window: str, moment: datetime, zone: tzinfo) -> QuotaError:
        if window == "minute":
            oldest = counters.minute[0] if counters.minute else moment.timestamp()
            retry = max(1, math.ceil(oldest + MINUTE_SECS - moment.timestamp()))
            error_type = "rate_limit_exceeded"
        elif window == "day":
            retry, error_type = _seconds_to_next_day(moment, zone), "insufficient_quota"
        else:
            retry, error_type = _seconds_to_next_month(moment, zone), "insufficient_quota"
        return QuotaError(status=429, error_type=error_type, code=code, message=limit_message(code), retry_after=retry)

    def _finish(self, ticket: Ticket) -> None:
        if not ticket.counted:
            return
        input_tokens, output_tokens = tokens_of(ticket.turn, cache_reads=count_cache_reads())
        moment = now()
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            if self._loaded_tz is None:
                # Counting was switched off meanwhile; the ledger carries this turn.
                return
            self._ensure(moment)
            self._global.add_tokens(input_tokens, output_tokens)
            if ticket.key_id is not None:
                self._counters(ticket.key_id).add_tokens(input_tokens, output_tokens)
            global_limits = _global_limits()
            checks: list[tuple[str, _Counters, str, str, int]] = [
                (code, self._global, window, metric, global_limits[code])
                for code, (_s, window, metric) in GLOBAL_LIMITS.items()
                if code in global_limits and metric != "requests"
            ]
            self._warn_after(checks, metric_filter=None)

    def _warn_after(self, checks: Iterable[tuple[str, _Counters, str, str, int]], *, metric_filter: str | None) -> None:
        """One WARNING per limit and window at 80% and at 100% (never a key name)."""
        for code, counters, window, metric, limit in checks:
            if metric_filter is not None and metric != metric_filter:
                continue
            if window == "minute":
                continue
            used = counters.used(window, metric)
            period = (counters.day if window == "day" else counters.month).period
            for level, threshold in (("100%", 1.0), ("80%", WARN_RATIO)):
                if used >= limit * threshold:
                    mark = (f"{code}:{level}", period)
                    if mark not in self._warned:
                        self._warned.add(mark)
                        logger.warning(
                            "/v1/responses %s limit %s reached %s (%d of %d) for %s",
                            "per-key" if code.startswith("key_") else "global",
                            code,
                            level,
                            used,
                            limit,
                            period,
                        )
                    break

    # -- status ------------------------------------------------------------------------

    def snapshot(self, key_ids: Iterable[str] = ()) -> dict[str, Any]:
        """Usage today and this month, global and per key, for the API Keys screen."""
        moment = now()
        with self._lock:
            if self._loaded_tz is None:
                # Not counting: read the ledger fresh, without keeping the result.
                probe = QuotaGuard()
                probe._ensure(moment)
                source = probe
            else:
                self._ensure(moment)
                source = self
            tz_name = source._loaded_tz or "UTC"

            def usage(counters: _Counters) -> dict[str, int]:
                return {
                    "requests_today": counters.day.requests,
                    "requests_month": counters.month.requests,
                    "input_tokens_today": counters.day.input,
                    "input_tokens_month": counters.month.input,
                    "output_tokens_today": counters.day.output,
                    "output_tokens_month": counters.month.output,
                    "total_tokens_today": counters.day.input + counters.day.output,
                    "total_tokens_month": counters.month.input + counters.month.output,
                }

            keys = {key_id: usage(source._keys.get(key_id) or _Counters()) for key_id in key_ids}
            return {
                "timezone": tz_name,
                "in_flight": self._in_flight,
                "counting": self._loaded_tz is not None,
                "usage": usage(source._global),
                "keys": keys,
            }


def global_limit_settings() -> dict[str, int]:
    """Every global limit's configured value (0 = none), keyed by its code."""
    values = {code: _int_setting(setting) for code, (setting, _w, _m) in GLOBAL_LIMITS.items()}
    values[CONCURRENCY_CODE] = _int_setting(CONCURRENCY_SETTING)
    values["input_chars"] = _int_setting(INPUT_CHARS_SETTING)
    return values


# ---- ledger reading ---------------------------------------------------------------------


def _ledger_records(moment: datetime) -> list[dict[str, Any]]:
    """``openai-api`` records of the current and previous UTC month files."""
    from app.usage.ledger import ledger_dir

    first = moment.astimezone(UTC).date().replace(day=1)
    previous = (first - timedelta(days=1)).replace(day=1)
    out: list[dict[str, Any]] = []
    root = ledger_dir()
    for month in (previous, first):
        path = root / f"{month:%Y-%m}.jsonl"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("quota: usage ledger month unreadable: %s", path, exc_info=True)
            continue
        for line in text.splitlines():
            if LANE not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("lane") == LANE:
                out.append(record)
    return out


def _record_local(record: Mapping[str, Any], zone: tzinfo) -> datetime | None:
    raw = record.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(zone)


_GUARD = QuotaGuard()


def guard() -> QuotaGuard:
    return _GUARD


def reset() -> None:
    """Forget every counter (tests, and nothing else)."""
    _GUARD.reset()


__all__ = [
    "GLOBAL_LIMITS",
    "KEY_LIMITS",
    "QuotaError",
    "QuotaGuard",
    "Ticket",
    "check_input_size",
    "global_limit_settings",
    "guard",
    "limit_message",
    "reset",
    "tokens_of",
]
