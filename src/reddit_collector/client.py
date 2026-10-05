"""Dedicated HTTP client for the Arctic Shift API (posts search only, for now).

Source of truth for endpoints/parameters:
https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md

- Base URL default: https://arctic-shift.photon-reddit.com
- Posts search: GET /api/posts/search
- Supported params (subset used here): subreddit, title, selftext, query,
  author, after, before, limit (1-100), sort (asc|desc by created_utc),
  fields.

Deliberately NOT included: rate-limit bypasses, proxies, or multi-IP
logic. Pass a `RateLimiter` (see rate_limit.py) to space requests and retry
429/5xx with exponential backoff; without one, each call is a single shot
that raises ArcticShiftRateLimitError so the *caller* can back off.
Saving/exporting data is out of scope for this module: it returns parsed
JSON only and never prints or writes files.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

import requests

from .rate_limit import RateLimiter, parse_retry_after_secs

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://arctic-shift.photon-reddit.com"
SEARCH_POSTS_PATH = "/api/posts/search"
COMMENT_TREE_PATH = "/api/comments/tree"

USER_AGENT = "reddit-collector/0.1.0 (academic research; contact: see README)"

_VALID_SORTS = {"asc", "desc"}


class ArcticShiftError(Exception):
    """Base error for all Arctic Shift client failures."""


class ArcticShiftNetworkError(ArcticShiftError):
    """Connection/DNS/timeout failure reaching the API."""


class ArcticShiftAPIError(ArcticShiftError):
    """Non-2xx response or unusable payload."""

    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class ArcticShiftRateLimitError(ArcticShiftAPIError):
    """HTTP 429. Honour retry_after_secs / reset_at before retrying."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        payload: Any = None,
        retry_after_secs: float | None = None,
        reset_at: str | None = None,
    ):
        super().__init__(message, status_code=status_code, payload=payload)
        self.retry_after_secs = retry_after_secs
        self.reset_at = reset_at


class ArcticShiftQueryTimeoutError(ArcticShiftAPIError):
    """API reported a transient query timeout (narrow filters / retry shortly)."""


# Matches transient timeout wording broadly: "timed out", "timeout",
# "time out", "time-out", "slow down", in any casing. The live API answers
# heavy queries with e.g. {"error": "Timeout. Maybe slow down a bit"}.
_TRANSIENT_TIMEOUT = re.compile(r"timed?\s*-?\s*out|slow\s*down", re.IGNORECASE)


def _transient_timeout_message(payload: Any) -> str | None:
    """Return the server's message if it reports a transient timeout, else None."""
    if not isinstance(payload, dict):
        return None
    for key in ("error", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and _TRANSIENT_TIMEOUT.search(value):
            return value.strip()
    return None


def _response_error_message(response: requests.Response) -> tuple[Any, str | None]:
    """Best-effort (payload, transient-timeout-message) from a JSON error body."""
    try:
        payload = response.json()
    except ValueError:
        return None, None
    return payload, _transient_timeout_message(payload)


def _normalise_subreddit(value: str | None) -> str | None:
    if value is None:
        return None
    name = value.strip()
    if name[:2].lower() == "r/":
        name = name[2:]
    return name or None


def _to_api_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    return text or None


class ArcticShiftClient:
    """Thin, single-origin HTTP client for the Arctic Shift posts search API.

    Pass a `RateLimiter` to enable polite spacing + retries; otherwise every
    call is a single attempt. No proxies or multi-IP logic by design.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout_secs: float = 30,
        session: requests.Session | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if not base_url.lower().startswith(("http://", "https://")):
            raise ValueError(f"base_url must start with http(s)://, got {base_url!r}")
        if not isinstance(timeout_secs, (int, float)) or isinstance(timeout_secs, bool) or timeout_secs <= 0:
            raise ValueError(f"timeout_secs must be a positive number, got {timeout_secs!r}")
        self.base_url = base_url.strip().rstrip("/")
        self.timeout_secs = timeout_secs
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.rate_limiter = rate_limiter
        # NOTE: no proxies, no multi-IP logic by design.

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        session: requests.Session | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> "ArcticShiftClient":
        """Build a client from a validated config dict (see config.py).

        Unless a limiter is passed explicitly, one is built from
        collection.throttle_qps / max_retries / backoff_* settings.
        """
        api_cfg = config.get("api", {}) if isinstance(config, Mapping) else {}
        base_url = api_cfg.get("base_url", DEFAULT_BASE_URL) if isinstance(api_cfg, Mapping) else DEFAULT_BASE_URL
        collection = config.get("collection", {}) if isinstance(config, Mapping) else {}
        timeout = collection.get("timeout_secs", 30) if isinstance(collection, Mapping) else 30
        limiter = rate_limiter if rate_limiter is not None else RateLimiter.from_config(config)
        return cls(base_url=base_url, timeout_secs=timeout, session=session, rate_limiter=limiter)

    @property
    def search_posts_url(self) -> str:
        return f"{self.base_url}{SEARCH_POSTS_PATH}"

    @property
    def comment_tree_url(self) -> str:
        return f"{self.base_url}{COMMENT_TREE_PATH}"

    def search_posts(
        self,
        *,
        subreddit: str | None = None,
        title: str | None = None,
        query: str | None = None,
        selftext: str | None = None,
        author: str | None = None,
        after: str | int | float | None = None,
        before: str | int | float | None = None,
        sort: str | None = None,
        limit: int = 25,
        fields: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """Search Reddit posts. Returns the parsed JSON response as a dict.

        With a `RateLimiter`, requests are spaced out and transient failures
        (HTTP 429, HTTP 5xx, connection errors/timeouts) are retried with
        exponential backoff, honouring any server-provided wait. Without one,
        a single attempt is made and 429/5xx raise immediately.

        Raises:
            ValueError: invalid limit/sort/fields arguments.
            ArcticShiftRateLimitError: HTTP 429, persistent after retries.
            ArcticShiftQueryTimeoutError: API reported 'Query timed out'.
            ArcticShiftAPIError: other non-2xx responses or bad payloads.
            ArcticShiftNetworkError: connection failures / request timeouts.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError(f"limit must be an integer 1-100 (Arctic Shift max), got {limit!r}")
        if sort is not None and sort not in _VALID_SORTS:
            raise ValueError(f"sort must be one of {sorted(_VALID_SORTS)}, got {sort!r}")

        if isinstance(fields, (list, tuple)):
            fields_value = ",".join(str(f).strip() for f in fields if str(f).strip())
            fields_param = fields_value or None
        elif fields is None:
            fields_param = None
        elif isinstance(fields, str):
            fields_param = fields.strip() or None
        else:
            raise ValueError(f"fields must be a comma-separated string or list, got {fields!r}")

        params: dict[str, str] = {"limit": str(limit)}
        optional = {
            "subreddit": _normalise_subreddit(subreddit) if subreddit is not None else None,
            "title": _to_api_value(title),
            "query": _to_api_value(query),
            "selftext": _to_api_value(selftext),
            "author": _to_api_value(author).removeprefix("u/") if author is not None and _to_api_value(author) else None,
            "after": _to_api_value(after),
            "before": _to_api_value(before),
            "sort": sort,
            "fields": fields_param,
        }
        for key, value in optional.items():
            if value is not None:
                params[key] = value

        return self._request_json(SEARCH_POSTS_PATH, params)

    def get_comment_tree(
        self,
        *,
        link_id: str,
        limit: int = 9999,
        parent_id: str | None = None,
        start_breadth: int | None = None,
        start_depth: int | None = None,
    ) -> dict[str, Any]:
        """Fetch the comment tree for one post. Returns the parsed JSON response.

        Uses GET /api/comments/tree (never /api/comments/search): a single
        request returns the complete nested discussion (top-level comments
        plus replies) as Reddit-style {"kind", "data"} nodes. Over-limit
        threads collapse excess into "kind": "more" nodes (left unresolved
        in Phase 1). Same spacing, retries, and error types as search_posts.

        Raises:
            ValueError: invalid link_id/limit/parent_id/breadth/depth arguments.
            ArcticShiftRateLimitError: HTTP 429, persistent after retries.
            ArcticShiftQueryTimeoutError: transient API timeout, persistent.
            ArcticShiftAPIError: other non-2xx responses or bad payloads.
            ArcticShiftNetworkError: connection failures / request timeouts.
        """
        post_id = _to_api_value(link_id)
        if post_id is None:
            raise ValueError("link_id (post ID) is required and must be non-empty")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 25000:
            raise ValueError(f"limit must be an integer 1-25000, got {limit!r}")
        for name, value in (("start_breadth", start_breadth), ("start_depth", start_depth)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name} must be an integer >= 0, got {value!r}")

        params: dict[str, str] = {"link_id": post_id, "limit": str(limit)}
        if parent_id is not None and _to_api_value(parent_id) is not None:
            params["parent_id"] = _to_api_value(parent_id)  # type: ignore[assignment]
        if start_breadth is not None:
            params["start_breadth"] = str(start_breadth)
        if start_depth is not None:
            params["start_depth"] = str(start_depth)
        return self._request_json(COMMENT_TREE_PATH, params)

    def _request_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """GET path with spacing, retries, and error mapping. Shared by all endpoints."""
        limiter = self.rate_limiter
        max_retries = limiter.max_retries if limiter is not None else 0

        attempt = 0
        while True:
            if limiter is not None:
                limiter.wait_for_turn()
            try:
                response = self.session.get(
                    f"{self.base_url}{path}", params=params, timeout=self.timeout_secs
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if limiter is not None and attempt < max_retries:
                    limiter.wait_before_retry(attempt, reason=f"network error: {exc}")
                    attempt += 1
                    continue
                raise ArcticShiftNetworkError(f"Network error calling {path}: {exc}") from exc
            except requests.RequestException as exc:
                raise ArcticShiftNetworkError(f"Network error calling {path}: {exc}") from exc

            if response.status_code == 429:
                retry_after = parse_retry_after_secs(response.headers)
                reset_at = response.headers.get("X-RateLimit-Reset-At")
                if limiter is not None and attempt < max_retries:
                    limiter.wait_before_retry(attempt, retry_after_secs=retry_after, reason="HTTP 429 rate limited")
                    attempt += 1
                    continue
                raise ArcticShiftRateLimitError(
                    "Arctic Shift rate limit hit (HTTP 429). "
                    + (
                        f"Retry after {retry_after:.0f}s."
                        if retry_after is not None
                        else "Back off and honour the server's reset headers."
                    ),
                    payload=_safe_body_snippet(response),
                    retry_after_secs=retry_after,
                    reset_at=reset_at,
                )

            if response.status_code == 422:
                # Heavy queries get e.g. {"data": null, "error": "Timeout. Maybe
                # slow down a bit"}. Transient: back off and retry like a 429,
                # but with pure exponential backoff — X-RateLimit-Reset rides
                # along on every response (even 200s), so it is informational
                # here, not a wait demand.
                _, timeout_message = _response_error_message(response)
                if timeout_message is not None:
                    if limiter is not None and attempt < max_retries:
                        limiter.wait_before_retry(
                            attempt, reason=f"transient API timeout: {timeout_message}"
                        )
                        attempt += 1
                        continue
                    raise ArcticShiftQueryTimeoutError(
                        f"Arctic Shift query timed out: {timeout_message} "
                        "(narrow the date range / add subreddit filter / lower limit and retry)",
                        status_code=response.status_code,
                        payload=_safe_body_snippet(response),
                    )
                raise ArcticShiftAPIError(
                    f"Arctic Shift API error: HTTP {response.status_code} for {path}",
                    status_code=response.status_code,
                    payload=_safe_body_snippet(response),
                )

            if 500 <= response.status_code < 600:
                if limiter is not None and attempt < max_retries:
                    limiter.wait_before_retry(attempt, reason=f"HTTP {response.status_code}")
                    attempt += 1
                    continue
                raise ArcticShiftAPIError(
                    f"Arctic Shift API error: HTTP {response.status_code} for {path}",
                    status_code=response.status_code,
                    payload=_safe_body_snippet(response),
                )

            if response.status_code < 200 or response.status_code >= 300:
                raise ArcticShiftAPIError(
                    f"Arctic Shift API error: HTTP {response.status_code} for {path}",
                    status_code=response.status_code,
                    payload=_safe_body_snippet(response),
                )
            break

        try:
            payload = response.json()
        except ValueError as exc:
            raise ArcticShiftAPIError(
                f"Arctic Shift returned non-JSON for {SEARCH_POSTS_PATH}: {exc}",
                status_code=response.status_code,
                payload=_safe_body_snippet(response),
            ) from exc

        if isinstance(payload, dict) and "error" in payload:
            message = str(payload.get("error") or payload.get("message") or "Unknown API error")
            if _transient_timeout_message(payload) is not None:
                raise ArcticShiftQueryTimeoutError(
                    f"Arctic Shift query timed out: {message} "
                    "(narrow the date range / add subreddit filter / lower limit and retry)",
                    status_code=response.status_code,
                    payload=payload,
                )
            raise ArcticShiftAPIError(
                f"Arctic Shift API error: {message}",
                status_code=response.status_code,
                payload=payload,
            )

        if not isinstance(payload, dict):
            raise ArcticShiftAPIError(
                f"Unexpected Arctic Shift response shape: expected JSON object, got {type(payload).__name__}",
                status_code=response.status_code,
                payload=payload,
            )
        return payload


def _safe_body_snippet(response: requests.Response, limit: int = 500) -> Any:
    try:
        text = response.text
    except Exception:
        return None
    if not isinstance(text, str):
        return None
    return text[:limit]


# Re-export for convenience so callers import from one place.
__all__ = [
    "COMMENT_TREE_PATH",
    "DEFAULT_BASE_URL",
    "SEARCH_POSTS_PATH",
    "ArcticShiftClient",
    "ArcticShiftError",
    "ArcticShiftNetworkError",
    "ArcticShiftAPIError",
    "ArcticShiftRateLimitError",
    "ArcticShiftQueryTimeoutError",
]
