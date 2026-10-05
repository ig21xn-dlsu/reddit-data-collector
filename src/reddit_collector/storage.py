"""Two-layer data storage: raw API records + normalized processed records.

Directory layout (one directory per collection run; runs are never overwritten):

    data/raw/<run_id>/posts.jsonl        # verbatim post objects, one JSON per line
    data/raw/<run_id>/manifest.json      # run parameters (filters, sort, limits)
    data/processed/<run_id>/posts.jsonl  # normalized records, one JSON per line

Format: JSON Lines. Each page is appended as it arrives, and readers stream
line by line, so a multi-million-post collection never needs the full
dataset in memory. A resumed run reuses its `run_id` and keeps appending to
the same files (deduplication itself is the paginator's job via seen ids).

Processed record schema (fixed keys, missing values are null):

    id            original Reddit post id, verbatim (never modified)
    subreddit     subreddit display name
    title         post title
    selftext      post body text (empty for link posts)
    author        author username
    score         upvote score (integer)
    num_comments  comment count (integer)
    created_utc   creation time, verbatim from the API (usually epoch seconds)
    created_iso   creation time as UTC ISO 8601, derived when possible
    url           post url, verbatim from the API
    permalink     full reddit.com permalink (from API, else built from id)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

logger = logging.getLogger(__name__)

RAW_FILENAME = "posts.jsonl"
COMMENTS_FILENAME = "comments.jsonl"
MANIFEST_FILENAME = "manifest.json"

PROCESSED_FIELDS = (
    "id",
    "subreddit",
    "title",
    "selftext",
    "author",
    "score",
    "num_comments",
    "created_utc",
    "created_iso",
    "url",
    "permalink",
)


class StorageError(Exception):
    """Raised when stored data cannot be written or read back."""


def _sanitise_run_part(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return cleaned or "run"


def new_run_id(subreddit: str | None = None) -> str:
    """Build a filesystem-safe, time-ordered run id like 20261005T173000Z_python."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if subreddit and subreddit.strip():
        return f"{stamp}_{_sanitise_run_part(subreddit)}"
    return stamp


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _created_iso(created_utc: Any) -> str | None:
    if isinstance(created_utc, bool):
        return None
    if not isinstance(created_utc, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalize_post(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Map one raw API post object to the fixed processed schema (no mutation)."""
    if not isinstance(raw, Mapping):
        raise StorageError(f"Cannot normalize post: expected a mapping, got {type(raw).__name__}")
    post_id = raw.get("id")  # preserved verbatim
    subreddit = _as_str(raw.get("subreddit"))
    permalink_raw = raw.get("permalink")
    if isinstance(permalink_raw, str) and permalink_raw.strip():
        permalink_raw = permalink_raw.strip()
        if permalink_raw.startswith("http"):
            permalink: str | None = permalink_raw
        else:
            permalink = "https://www.reddit.com" + (
                permalink_raw if permalink_raw.startswith("/") else "/" + permalink_raw
            )
    elif subreddit and post_id is not None:
        permalink = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"
    else:
        permalink = None
    created_utc = raw.get("created_utc")
    return {
        "id": post_id,
        "subreddit": subreddit,
        "title": _as_str(raw.get("title")),
        "selftext": _as_str(raw.get("selftext")),
        "author": _as_str(raw.get("author")),
        "score": _as_int(raw.get("score")),
        "num_comments": _as_int(raw.get("num_comments")),
        "created_utc": created_utc,
        "created_iso": _created_iso(created_utc),
        "url": _as_str(raw.get("url")),
        "permalink": permalink,
    }


class RunStore:
    """Appendable per-run file store for raw + processed posts."""

    def __init__(
        self,
        raw_root: str | Path,
        processed_root: str | Path,
        run_id: str | None = None,
        subreddit: str | None = None,
    ) -> None:
        self.raw_root = Path(raw_root)
        self.processed_root = Path(processed_root)
        requested = (run_id or "").strip() if run_id else ""
        base = _sanitise_run_part(requested) if requested else new_run_id(subreddit)
        self.run_id = self._unique_run_id(base)
        self.raw_file = self.raw_root / self.run_id / RAW_FILENAME
        self.processed_file = self.processed_root / self.run_id / RAW_FILENAME
        self.raw_comments_file = self.raw_root / self.run_id / COMMENTS_FILENAME
        self.processed_comments_file = self.processed_root / self.run_id / COMMENTS_FILENAME
        self.raw_file.parent.mkdir(parents=True, exist_ok=True)
        self.processed_file.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Run store ready: run_id=%s raw=%s", self.run_id, self.raw_file)

    @classmethod
    def from_config(cls, config: Mapping[str, Any], run_id: str | None = None) -> "RunStore":
        output = config.get("output", {})
        if not isinstance(output, Mapping):
            raise StorageError("'output' must be a mapping with raw_dir/processed_dir")
        return cls(
            output.get("raw_dir", "data/raw"),
            output.get("processed_dir", "data/processed"),
            run_id=run_id,
            subreddit=config.get("subreddit"),
        )

    @classmethod
    def existing(cls, raw_root: str | Path, processed_root: str | Path, run_id: str) -> "RunStore":
        """Reopen a previous run to keep appending (resume). Never bumps the id."""
        store = cls.__new__(cls)
        store.raw_root = Path(raw_root)
        store.processed_root = Path(processed_root)
        cleaned = (run_id or "").strip()
        if not cleaned:
            raise StorageError("run_id is required to resume a previous run")
        store.run_id = _sanitise_run_part(cleaned)
        store.raw_file = store.raw_root / store.run_id / RAW_FILENAME
        store.processed_file = store.processed_root / store.run_id / RAW_FILENAME
        store.raw_comments_file = store.raw_root / store.run_id / COMMENTS_FILENAME
        store.processed_comments_file = store.processed_root / store.run_id / COMMENTS_FILENAME
        if not store.raw_file.parent.is_dir() or not store.processed_file.parent.is_dir():
            raise StorageError(f"No previous run {store.run_id!r} under {raw_root} / {processed_root}")
        logger.info("Run store resumed: run_id=%s", store.run_id)
        return store

    def _unique_run_id(self, base: str) -> str:
        """Bump the id (run-2, run-3, ...) while either run dir already exists."""
        base = _sanitise_run_part(base)
        candidate = base
        suffix = 1
        while (self.raw_root / candidate).exists() or (self.processed_root / candidate).exists():
            suffix += 1
            candidate = f"{base}-{suffix}"
        if candidate != base:
            logger.info("Run id %r taken, using %r instead", base, candidate)
        return candidate

    # -- writes -------------------------------------------------------

    def append_raw(self, posts: list[Mapping[str, Any]]) -> int:
        """Append verbatim API post objects. Returns the number stored."""
        return self._append_lines(self.raw_file, posts)

    def append_processed(self, posts: list[Mapping[str, Any]]) -> int:
        """Normalize and append processed records. Returns the number stored."""
        return self._append_lines(self.processed_file, [normalize_post(p) for p in posts])

    def append_page(self, posts: list[Mapping[str, Any]]) -> int:
        """Append one fetched page to both layers. Returns the number stored."""
        raw_count = self.append_raw(posts)
        self.append_processed(posts)
        return raw_count

    def append_raw_comments(self, nodes: list[Mapping[str, Any]]) -> int:
        """Append verbatim comment tree nodes. Returns the number stored."""
        return self._append_lines(self.raw_comments_file, nodes)

    def append_processed_comments(self, records: list[Mapping[str, Any]]) -> int:
        """Append normalized comment records. Returns the number stored."""
        return self._append_lines(self.processed_comments_file, records)

    def write_manifest(self, params: Mapping[str, Any]) -> Path:
        """Record the run parameters next to the raw data."""
        manifest = self.raw_file.parent / MANIFEST_FILENAME
        doc = {
            "run_id": self.run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "params": dict(params),
        }
        try:
            manifest.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError(f"Could not write manifest {manifest}: {exc}") from exc
        return manifest

    # -- reads (streaming) --------------------------------------------

    def iter_raw(self) -> Iterator[dict[str, Any]]:
        return self._iter_lines(self.raw_file)

    def iter_processed(self) -> Iterator[dict[str, Any]]:
        return self._iter_lines(self.processed_file)

    def iter_raw_comments(self) -> Iterator[dict[str, Any]]:
        return self._iter_lines(self.raw_comments_file)

    def iter_processed_comments(self) -> Iterator[dict[str, Any]]:
        return self._iter_lines(self.processed_comments_file)

    def count_raw(self) -> int:
        return sum(1 for _ in self.iter_raw())

    def count_processed(self) -> int:
        return sum(1 for _ in self.iter_processed())

    def count_raw_comments(self) -> int:
        return sum(1 for _ in self.iter_raw_comments())

    def count_processed_comments(self) -> int:
        return sum(1 for _ in self.iter_processed_comments())

    # -- internals ----------------------------------------------------

    @staticmethod
    def _append_lines(path: Path, records: list[Mapping[str, Any]]) -> int:
        if not records:
            return 0
        try:
            with open(path, "a", encoding="utf-8") as fh:
                for record in records:
                    # No sort_keys: processed files keep PROCESSED_FIELDS order.
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError(f"Could not append {len(records)} records to {path}: {exc}") from exc
        return len(records)

    @staticmethod
    def _iter_lines(path: Path) -> Iterator[dict[str, Any]]:
        if not path.exists():
            return
        try:
            fh = open(path, encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Could not read {path}: {exc}") from exc
        with fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError as exc:
                    raise StorageError(f"Corrupt line {lineno} in {path}: {exc}") from exc
                if not isinstance(record, dict):
                    raise StorageError(f"Corrupt line {lineno} in {path}: expected an object")
                yield record


__all__ = [
    "COMMENTS_FILENAME",
    "MANIFEST_FILENAME",
    "PROCESSED_FIELDS",
    "RAW_FILENAME",
    "RunStore",
    "StorageError",
    "new_run_id",
    "normalize_post",
]
