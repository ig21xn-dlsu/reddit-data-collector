"""Conservative rate limiter + retry/backoff helper for the Arctic Shift API.

Arctic Shift documents *dynamic* rate limits (based on server load and query
complexity), not a fixed quota. Its docs say a couple of requests per second
is fine for normal users, and a 429 response carries `X-RateLimit-Reset`
(seconds until reset) / `X-RateLimit-Reset-At` headers.

This module never bypasses limits: it spaces requests out, honours the
server's wait instructions, and backs off exponentially on 429/5xx. It is
reusable: `ArcticShiftClient` accepts one, and the future collector can share
the same instance.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


def _get_header(headers: Mapping[str, Any], name: str) -> Any:
    """Case-insensitive header lookup (works for plain dicts and CaseInsensitiveDict)."""
    try:
        value = headers.get(name)  # type: ignore[union-attr]
    except AttributeError:
        value = None
    if value is not None:
        return value
    lowered = name.lower()
    try:
        items = headers.items()  # type: ignore[union-attr]
    except AttributeError:
        return None
    for key, val in items:
        if isinstance(key, str) and key.lower() == lowered:
            return val
    return None


def parse_retry_after_secs(headers: Mapping[str, Any], now: datetime | None = None) -> float | None:
    """Return how long the server asks us to wait, in seconds, or None.

    Checks, in order:
    1. Standard `Retry-After` header (delay seconds, or an HTTP date).
    2. Arctic Shift's `X-RateLimit-Reset` header (seconds until reset).
    """
    raw = _get_header(headers, "Retry-After")
    if raw is not None:
        text = str(raw).strip()
        if text:
            try:
                return max(0.0, float(text))
            except ValueError:
                pass  # not delay-seconds; try HTTP date below
            try:
                reset_at = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                reset_at = None
            if reset_at is not None:
                if reset_at.tzinfo is None:
                    reset_at = reset_at.replace(tzinfo=timezone.utc)
                ref = now or datetime.now(timezone.utc)
                return max(0.0, (reset_at - ref).total_seconds())

    raw = _get_header(headers, "X-RateLimit-Reset")
    if raw is not None:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        if value >= 0:
            return value
    return None


class RateLimiter:
    """Spaces requests and computes retry delays. Thread-safe.

    - `min_interval_secs`: minimum gap between request starts (1.0 = max ~1 req/s).
    - `max_retries`: retries *after* the first attempt (0 = single shot).
    - `backoff_base_secs` / `backoff_max_secs`: delay for attempt N is
      `min(base * 2**N, max)`; a server-provided wait always wins if larger.
    - `sleep_fn` / `clock_fn` are injectable so tests avoid real sleeping.
    """

    def __init__(
        self,
        min_interval_secs: float = 1.0,
        max_retries: int = 5,
        backoff_base_secs: float = 1.0,
        backoff_max_secs: float = 60.0,
        sleep_fn: Callable[[float], None] | None = None,
        clock_fn: Callable[[], float] | None = None,
    ) -> None:
        for name, value in (
            ("min_interval_secs", min_interval_secs),
            ("backoff_base_secs", backoff_base_secs),
            ("backoff_max_secs", backoff_max_secs),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a number >= 0, got {value!r}")
        if min_interval_secs == 0:
            raise ValueError("min_interval_secs must be > 0 (use a small value, not 0, to stay polite)")
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
            raise ValueError(f"max_retries must be an integer >= 0, got {max_retries!r}")
        self.min_interval_secs = float(min_interval_secs)
        self.max_retries = max_retries
        self.backoff_base_secs = float(backoff_base_secs)
        self.backoff_max_secs = float(backoff_max_secs)
        self._sleep = sleep_fn or time.sleep
        self._clock = clock_fn or time.monotonic
        self._lock = threading.Lock()
        self._last_request_at: float | None = None

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        sleep_fn: Callable[[float], None] | None = None,
        clock_fn: Callable[[], float] | None = None,
    ) -> "RateLimiter":
        """Build from a validated config dict: collection.throttle_qps etc."""
        collection = config.get("collection", {}) if isinstance(config, Mapping) else {}
        if not isinstance(collection, Mapping):
            collection = {}
        qps = collection.get("throttle_qps", 1.0)
        if not isinstance(qps, (int, float)) or isinstance(qps, bool) or qps <= 0:
            raise ValueError(f"throttle_qps must be a positive number, got {qps!r}")
        return cls(
            min_interval_secs=1.0 / float(qps),
            max_retries=collection.get("max_retries", 5),
            backoff_base_secs=collection.get("backoff_base_secs", 1.0),
            backoff_max_secs=collection.get("backoff_max_secs", 60.0),
            sleep_fn=sleep_fn,
            clock_fn=clock_fn,
        )

    @property
    def throttle_qps(self) -> float:
        return 1.0 / self.min_interval_secs

    def wait_for_turn(self) -> float:
        """Sleep if the previous request was too recent. Returns seconds waited."""
        with self._lock:
            now = self._clock()
            if self._last_request_at is None:
                self._last_request_at = now
                return 0.0
            wait = self.min_interval_secs - (now - self._last_request_at)
            if wait <= 0:
                self._last_request_at = now
                return 0.0
            logger.info("Rate limiting: waiting %.2fs before next request", wait)
            self._sleep(wait)
            self._last_request_at = self._clock()
            return wait

    def backoff_for_attempt(self, attempt: int, retry_after_secs: float | None = None) -> float:
        """Delay before retry number `attempt` (0-based). Server wait wins if larger."""
        if attempt < 0:
            raise ValueError(f"attempt must be >= 0, got {attempt!r}")
        backoff = min(self.backoff_base_secs * (2.0**attempt), self.backoff_max_secs)
        if retry_after_secs is not None:
            if retry_after_secs < 0:
                raise ValueError(f"retry_after_secs must be >= 0, got {retry_after_secs!r}")
            return max(backoff, float(retry_after_secs))
        return backoff

    def wait_before_retry(self, attempt: int, retry_after_secs: float | None = None, reason: str = "") -> float:
        """Sleep the backoff delay for a failed attempt. Returns seconds waited."""
        delay = self.backoff_for_attempt(attempt, retry_after_secs)
        detail = f" ({reason})" if reason else ""
        logger.warning("Waiting %.2fs before retry %d/%d%s", delay, attempt + 1, self.max_retries, detail)
        self._sleep(delay)
        return delay


__all__ = ["RateLimiter", "parse_retry_after_secs"]
