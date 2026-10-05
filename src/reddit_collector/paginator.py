"""Time-cursor pagination over Arctic Shift post search.

Arctic Shift's search endpoints document no page tokens. Pagination follows
the documented mechanism: results are sorted by `created_utc` (`sort=asc` or
`desc`), and the `after` / `before` parameters bound the time window. Each
page therefore advances a time cursor:

- `sort="asc"`: next request uses `after = max(created_utc)` of this page.
- `sort="desc"`: next request uses `before = min(created_utc)` of this page.

The paginator yields one page (a small list) at a time so callers never hold
the full dataset in memory. It deduplicates by post `id`, stops when a page
comes back short/empty or when `max_posts` is reached, and exposes `state()`
for the checkpoint system (`checkpoint.py`), which persists the cursor so a
later run can resume. Rate limiting is inherited: all requests go through
`ArcticShiftClient`, which applies its `RateLimiter`.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Iterator, Mapping

from .client import ArcticShiftClient

logger = logging.getLogger(__name__)

STATE_VERSION = 1


def _post_id(post: Mapping[str, Any]) -> str | None:
    value = post.get("id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _post_ts(post: Mapping[str, Any]) -> float | None:
    value = post.get("created_utc")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PostPaginator:
    """Paginate a post search. Yields pages of *new* posts oldest/newest first."""

    def __init__(
        self,
        client: ArcticShiftClient,
        *,
        subreddit: str | None = None,
        title: str | None = None,
        query: str | None = None,
        selftext: str | None = None,
        author: str | None = None,
        after: str | int | float | None = None,
        before: str | int | float | None = None,
        sort: str = "asc",
        page_size: int = 25,
        max_posts: int | None = None,
        fields: str | list[str] | None = None,
        resume: Mapping[str, Any] | None = None,
    ) -> None:
        if sort not in ("asc", "desc"):
            raise ValueError(f"sort must be 'asc' or 'desc', got {sort!r}")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 100:
            raise ValueError(f"page_size must be an integer 1-100 (Arctic Shift max), got {page_size!r}")
        if max_posts is not None and (
            not isinstance(max_posts, int) or isinstance(max_posts, bool) or max_posts < 1
        ):
            raise ValueError(f"max_posts must be a positive integer or None, got {max_posts!r}")
        self.client = client
        self.filters: dict[str, Any] = {
            "subreddit": subreddit,
            "title": title,
            "query": query,
            "selftext": selftext,
            "author": author,
            "after": after,
            "before": before,
        }
        self.sort = sort
        self.page_size = page_size
        self.max_posts = max_posts
        self.fields = fields

        self._seen_ids: set[str] = set()
        self._total_fetched = 0
        self._cursor: str | int | float | None = after if sort == "asc" else before

        if resume is not None:
            self._apply_resume(resume)

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any], client: ArcticShiftClient, resume: Mapping[str, Any] | None = None
    ) -> "PostPaginator":
        """Build from a validated config dict (search filters + limit + max_posts)."""
        return cls(
            client,
            subreddit=config.get("subreddit"),
            title=config.get("title"),
            query=config.get("query"),
            selftext=config.get("selftext"),
            author=config.get("author"),
            after=config.get("after"),
            before=config.get("before"),
            sort=config.get("sort", "asc"),
            page_size=config.get("limit", 25),
            max_posts=config.get("max_posts"),
            resume=resume,
        )

    # -- resume support -------------------------------------------------

    def _apply_resume(self, state: Mapping[str, Any]) -> None:
        if not isinstance(state, Mapping):
            raise ValueError(f"resume state must be a mapping, got {type(state).__name__}")
        if state.get("version") != STATE_VERSION:
            raise ValueError(f"unsupported resume state version: {state.get('version')!r}")
        if state.get("sort") != self.sort:
            raise ValueError(
                f"resume sort {state.get('sort')!r} does not match paginator sort {self.sort!r}"
            )
        saved_filters = state.get("filters")
        if not isinstance(saved_filters, Mapping) or dict(saved_filters) != self.filters:
            raise ValueError("resume filters do not match this paginator's search filters")
        seen = state.get("seen_ids", [])
        if not isinstance(seen, (list, tuple)) or any(not isinstance(i, str) for i in seen):
            raise ValueError("resume seen_ids must be a list of post id strings")
        total = state.get("total_fetched", 0)
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ValueError(f"resume total_fetched must be an integer >= 0, got {total!r}")
        self._seen_ids = set(seen)
        self._total_fetched = total
        self._cursor = state.get("cursor")

    def state(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot the checkpoint system can store."""
        return {
            "version": STATE_VERSION,
            "filters": dict(self.filters),
            "sort": self.sort,
            "page_size": self.page_size,
            "max_posts": self.max_posts,
            "cursor": self._cursor,
            "total_fetched": self._total_fetched,
            "seen_ids": sorted(self._seen_ids),
        }

    @property
    def total_fetched(self) -> int:
        return self._total_fetched

    def reconcile_stored(self, ids: Iterable[Any]) -> int:
        """Union ids already on disk into seen state; returns newly-seen count.

        Repairs the crash window where a page was appended to storage but the
        checkpoint was never saved: without this, resume would re-yield that
        page and duplicate it in the files. `total_fetched` is bumped for the
        reconciled posts since they are already collected on disk (this also
        keeps `max_posts` accounting correct). Id-less posts cannot be
        reconciled and may still duplicate.
        """
        added = 0
        for value in ids:
            if value is None:
                continue
            pid = str(value).strip()
            if not pid or pid in self._seen_ids:
                continue
            self._seen_ids.add(pid)
            added += 1
        if added:
            self._total_fetched += added
            logger.info(
                "Reconciled %d already-stored posts missing from checkpoint (%d total)",
                added, self._total_fetched,
            )
        return added

    # -- iteration ------------------------------------------------------

    def iter_pages(self) -> Iterator[list[dict[str, Any]]]:
        """Yield successive pages (lists) of new posts. Stops on exhaustion or max."""
        cursor = self._cursor
        page_num = 0
        while True:
            if self.max_posts is not None and self._total_fetched >= self.max_posts:
                logger.info("Reached max_posts=%d, stopping", self.max_posts)
                return
            page_num += 1
            params = self._page_params(cursor)
            payload = self.client.search_posts(**params, sort=self.sort, limit=self.page_size, fields=self.fields)
            items = payload.get("data", [])
            if not isinstance(items, list):
                raise ValueError(f"Unexpected API response: 'data' is {type(items).__name__}, not a list")
            if not items:
                logger.info("Page %d: no more results (%d posts total)", page_num, self._total_fetched)
                return

            new_posts: list[dict[str, Any]] = []
            for post in items:
                if not isinstance(post, Mapping):
                    continue
                pid = _post_id(post)
                if pid is None:
                    new_posts.append(dict(post))  # no id: cannot dedupe, keep it
                elif pid not in self._seen_ids:
                    self._seen_ids.add(pid)
                    new_posts.append(dict(post))

            remaining: int | None = None
            if self.max_posts is not None:
                remaining = self.max_posts - self._total_fetched
                new_posts = new_posts[:remaining]
            self._total_fetched += len(new_posts)

            next_cursor = self._advance_cursor(cursor, items, new_posts)
            self._cursor = next_cursor  # persist before yield so state() is accurate mid-iteration

            cap = f"/{self.max_posts}" if self.max_posts is not None else ""
            logger.info(
                "Page %d: %d new posts (%d%s total)", page_num, len(new_posts), self._total_fetched, cap
            )
            if new_posts:
                yield new_posts

            if self.max_posts is not None and self._total_fetched >= self.max_posts:
                logger.info("Reached max_posts=%d, stopping", self.max_posts)
                return
            if len(items) < self.page_size:
                logger.info("Last page had %d/%d items, stopping", len(items), self.page_size)
                return
            if next_cursor == cursor:
                # Refetching would repeat this exact request forever.
                logger.warning(
                    "Page %d: cursor did not advance and page was full; "
                    "stopping to avoid an infinite loop (some results may be missed)",
                    page_num,
                )
                return
            cursor = next_cursor

    def iter_posts(self) -> Iterator[dict[str, Any]]:
        """Yield new posts one by one (same pagination, minimal memory)."""
        for page in self.iter_pages():
            yield from page

    # -- internals ------------------------------------------------------

    def _page_params(self, cursor: str | int | float | None) -> dict[str, Any]:
        params = dict(self.filters)
        if self.sort == "asc":
            params["after"] = cursor
        else:
            params["before"] = cursor
        return params

    def _advance_cursor(
        self,
        cursor: str | int | float | None,
        items: list[Any],
        new_posts: list[dict[str, Any]],
    ) -> str | int | float | None:
        """Compute the next time cursor, forcing progress on full-duplicate pages."""
        stamps = [_post_ts(p) for p in items if isinstance(p, Mapping)]
        stamps = [s for s in stamps if s is not None]
        if not stamps:
            return cursor
        if new_posts:
            extreme = max(stamps) if self.sort == "asc" else min(stamps)
            return _clean_ts(extreme)
        # Full page of duplicates: the plain extreme would refetch the same
        # page forever, so step past it. (A same-second post beyond the page
        # limit could be skipped here; logged by the caller.)
        extreme = max(stamps) if self.sort == "asc" else min(stamps)
        step = 1 if self.sort == "asc" else -1
        logger.warning(
            "Page returned only already-seen posts; stepping cursor past %s", _clean_ts(extreme)
        )
        return _clean_ts(extreme) + step

def _clean_ts(value: float) -> int | float:
    return int(value) if value.is_integer() else value


__all__ = ["PostPaginator"]
