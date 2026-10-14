"""Two-layer data storage: raw API records + normalized processed records.

Directory layout (one directory per collection run; runs are never overwritten):

    data/raw/<run_id>/manifest.json                    # run parameters + chunk inventory
    data/raw/<run_id>/posts/posts_00001.jsonl          # verbatim post objects, chunked
    data/raw/<run_id>/comments/comments_00001.jsonl    # verbatim comment nodes, chunked
    data/processed/<run_id>/posts/posts_00001.jsonl    # normalized posts, chunked
    data/processed/<run_id>/comments/comments_00001.jsonl

Format: JSON Lines. Each page is appended as it arrives, and readers stream
line by line, so a multi-million-post collection never needs the full
dataset in memory. A resumed run reuses its `run_id` and keeps appending
(never overwriting): the writer scans existing chunks on open and continues
in the first non-full chunk. Legacy single-file runs
(`posts.jsonl` / `comments.jsonl` directly under the run dir) are still
read (legacy content first, then chunks) but new records always go to
chunks; legacy files are never modified, renamed, or deleted.

Chunking: at most `chunk_size` records per file (default 50,000,
configurable via `output.chunk_size`). Rotation is incremental: records
are streamed to the current chunk and a new numbered file is started only
when the limit is reached.

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

RAW_FILENAME = "posts.jsonl"  # legacy single-file layout (read-only compat)
COMMENTS_FILENAME = "comments.jsonl"  # legacy single-file layout (read-only compat)
MANIFEST_FILENAME = "manifest.json"

POSTS_SUBDIR = "posts"
COMMENTS_SUBDIR = "comments"
POSTS_PREFIX = "posts"
COMMENTS_PREFIX = "comments"
DEFAULT_CHUNK_SIZE = 50000

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
    cleaned = re.sub(r"[^a-z0-9-]+", "_", text.strip().lower()).strip("_-")
    return cleaned or "run"


def new_run_id(subreddit: str | None = None) -> str:
    """Build a filesystem-safe, time-ordered run id like 20261005T173000Z_python."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if subreddit and subreddit.strip():
        return f"{stamp}_{_sanitise_run_part(subreddit)}"
    return stamp


def validate_chunk_size(value: Any) -> int:
    """Validate a chunk_size value, returning it as int. Raises StorageError."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StorageError(f"chunk_size must be a positive integer, got {value!r}")
    return value


def chunk_size_from_config(config: Mapping[str, Any]) -> int:
    """Read output.chunk_size from a loaded config (default 50,000)."""
    output = config.get("output", {})
    if not isinstance(output, Mapping):
        return DEFAULT_CHUNK_SIZE
    value = output.get("chunk_size", DEFAULT_CHUNK_SIZE)
    if value is None:
        return DEFAULT_CHUNK_SIZE
    return validate_chunk_size(value)


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


def _chunk_filename(prefix: str, index: int) -> str:
    return f"{prefix}_{index:05d}.jsonl"


_CHUNK_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _chunk_pattern(prefix: str) -> re.Pattern[str]:
    pat = _CHUNK_RE_CACHE.get(prefix)
    if pat is None:
        pat = re.compile(r"^" + re.escape(prefix) + r"_(\d+)\.jsonl$")
        _CHUNK_RE_CACHE[prefix] = pat
    return pat


def _list_chunk_files(chunk_dir: Path, prefix: str) -> list[Path]:
    """Sorted chunk files for a stream. Empty list when the dir is absent."""
    if not chunk_dir.is_dir():
        return []
    pat = _chunk_pattern(prefix)
    found: list[tuple[int, Path]] = []
    for child in chunk_dir.iterdir():
        if not child.is_file():
            continue
        m = pat.match(child.name)
        if not m:
            continue
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        if idx < 1:
            continue
        found.append((idx, child))
    found.sort(key=lambda t: t[0])
    return [p for _, p in found]


class _StreamState:
    """In-memory write cursor for one chunked stream."""

    def __init__(self, legacy_file: Path, chunk_dir: Path, prefix: str, chunk_size: int) -> None:
        self.legacy_file = legacy_file
        self.chunk_dir = chunk_dir
        self.prefix = prefix
        self.chunk_size = chunk_size
        self.legacy_count = 0
        self.chunks: list[tuple[Path, int]] = []  # (path, record count)
        self.total = 0
        self.cur_index = 1
        self.cur_fill = 0
        self._scan()

    def _scan(self) -> None:
        if self.legacy_file.is_file():
            self.legacy_count = _count_records_file(self.legacy_file)
        else:
            self.legacy_count = 0
        self.chunks = []
        for path in _list_chunk_files(self.chunk_dir, self.prefix):
            self.chunks.append((path, _count_records_file(path)))
        self.total = self.legacy_count + sum(n for _, n in self.chunks)
        if not self.chunks:
            self.cur_index = 1
            self.cur_fill = 0
        else:
            last_idx = _chunk_index(self.chunks[-1][0], self.prefix) or len(self.chunks)
            last_fill = self.chunks[-1][1]
            if last_fill >= self.chunk_size:
                self.cur_index = last_idx + 1
                self.cur_fill = 0
            else:
                self.cur_index = last_idx
                self.cur_fill = last_fill

    def set_chunk_size(self, chunk_size: int) -> None:
        """Adopt a new chunk_size (e.g. config changed between resume)."""
        self.chunk_size = chunk_size
        if not self.chunks:
            self.cur_index = 1
            self.cur_fill = 0
            return
        last_idx = _chunk_index(self.chunks[-1][0], self.prefix) or len(self.chunks)
        last_fill = self.chunks[-1][1]
        if last_fill >= chunk_size:
            self.cur_index = last_idx + 1
            self.cur_fill = 0
        else:
            self.cur_index = last_idx
            self.cur_fill = last_fill

    def append(self, records: list[Mapping[str, Any]]) -> int:
        if not records:
            return 0
        pos = 0
        total = len(records)
        while pos < total:
            if self.cur_fill >= self.chunk_size:
                self.cur_index += 1
                self.cur_fill = 0
            remaining = self.chunk_size - self.cur_fill
            batch = records[pos:pos + remaining]
            if self.cur_fill == 0 and not self._current_path().exists():
                # New chunk file: ensure the directory exists first.
                self.chunk_dir.mkdir(parents=True, exist_ok=True)
            _append_lines_to_file(self._current_path(), batch)
            self.cur_fill += len(batch)
            self.total += len(batch)
            self._record_chunk_count(self._current_path(), self.cur_fill)
            pos += len(batch)
        return total

    def _current_path(self) -> Path:
        return self.chunk_dir / _chunk_filename(self.prefix, self.cur_index)

    def _record_chunk_count(self, path: Path, fill: int) -> None:
        for i, (p, _) in enumerate(self.chunks):
            if p == path:
                self.chunks[i] = (p, fill)
                return
        self.chunks.append((path, fill))

    def inventory(self, run_dir: Path) -> list[dict[str, Any]]:
        """Chunk entries for the manifest (legacy entry first when present)."""
        entries: list[dict[str, Any]] = []
        if self.legacy_file.is_file():
            entries.append({
                "file": self.legacy_file.name,
                "records": self.legacy_count,
                "legacy": True,
            })
        for path, count in self.chunks:
            try:
                rel = str(path.relative_to(run_dir))
            except ValueError:
                rel = path.name
            entries.append({"file": rel, "records": count})
        return entries


def _chunk_index(path: Path, prefix: str) -> int | None:
    m = _chunk_pattern(prefix).match(path.name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _count_records_file(path: Path) -> int:
    n = 0
    for _ in RunStore._iter_lines(path):
        n += 1
    return n


def _append_lines_to_file(path: Path, records: list[Mapping[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for record in records:
                # No sort_keys: processed files keep field order.
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
    except (OSError, ValueError, TypeError) as exc:
        raise StorageError(f"Could not append {len(records)} records to {path}: {exc}") from exc


class RunStore:
    """Appendable per-run chunked file store for raw + processed posts/comments."""

    def __init__(
        self,
        raw_root: str | Path,
        processed_root: str | Path,
        run_id: str | None = None,
        subreddit: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.chunk_size = validate_chunk_size(chunk_size)
        self.raw_root = Path(raw_root)
        self.processed_root = Path(processed_root)
        requested = (run_id or "").strip() if run_id else ""
        base = _sanitise_run_part(requested) if requested else new_run_id(subreddit)
        self.run_id = self._unique_run_id(base)
        self._init_paths()
        self.raw_run_dir.mkdir(parents=True, exist_ok=True)
        self.processed_run_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_params: dict[str, Any] | None = None
        self._manifest_started_at: str | None = None
        self._init_streams()
        logger.info("Run store ready: run_id=%s raw=%s chunk_size=%d",
                    self.run_id, self.raw_run_dir, self.chunk_size)

    def _init_paths(self) -> None:
        self.raw_run_dir = self.raw_root / self.run_id
        self.processed_run_dir = self.processed_root / self.run_id
        # Legacy single-file locations (read-only compatibility).
        self.raw_file = self.raw_run_dir / RAW_FILENAME
        self.processed_file = self.processed_run_dir / RAW_FILENAME
        self.raw_comments_file = self.raw_run_dir / COMMENTS_FILENAME
        self.processed_comments_file = self.processed_run_dir / COMMENTS_FILENAME
        # Chunked locations for new writes.
        self.raw_posts_dir = self.raw_run_dir / POSTS_SUBDIR
        self.raw_comments_dir = self.raw_run_dir / COMMENTS_SUBDIR
        self.processed_posts_dir = self.processed_run_dir / POSTS_SUBDIR
        self.processed_comments_dir = self.processed_run_dir / COMMENTS_SUBDIR

    def _init_streams(self) -> None:
        self._streams: dict[str, _StreamState] = {
            "raw_posts": _StreamState(self.raw_file, self.raw_posts_dir,
                                      POSTS_PREFIX, self.chunk_size),
            "processed_posts": _StreamState(self.processed_file, self.processed_posts_dir,
                                            POSTS_PREFIX, self.chunk_size),
            "raw_comments": _StreamState(self.raw_comments_file, self.raw_comments_dir,
                                         COMMENTS_PREFIX, self.chunk_size),
            "processed_comments": _StreamState(self.processed_comments_file,
                                               self.processed_comments_dir,
                                               COMMENTS_PREFIX, self.chunk_size),
        }
        # If a manifest already exists (resume without explicit load), adopt
        # its params/started_at so refreshes preserve them.
        self._load_manifest_state_if_present()

    def _load_manifest_state_if_present(self) -> None:
        manifest = self.raw_run_dir / MANIFEST_FILENAME
        if not manifest.is_file():
            return
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(doc, dict):
            return
        params = doc.get("params")
        if isinstance(params, dict):
            self._manifest_params = dict(params)
        started = doc.get("started_at")
        if isinstance(started, str):
            self._manifest_started_at = started

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
            chunk_size=output.get("chunk_size", DEFAULT_CHUNK_SIZE)
            if output.get("chunk_size") is not None else DEFAULT_CHUNK_SIZE,
        )

    @classmethod
    def existing(cls, raw_root: str | Path, processed_root: str | Path, run_id: str,
                 chunk_size: int | None = None) -> "RunStore":
        """Reopen a previous run to keep appending (resume). Never bumps the id.

        Existing chunk files are scanned and the writer continues in the
        first non-full chunk, so resume never overwrites, duplicates, or
        skips records. Legacy single files are read but never written to.
        """
        store = cls.__new__(cls)
        store.raw_root = Path(raw_root)
        store.processed_root = Path(processed_root)
        cleaned = (run_id or "").strip()
        if not cleaned:
            raise StorageError("run_id is required to resume a previous run")
        store.run_id = _sanitise_run_part(cleaned)
        # Resolve chunk_size: explicit arg wins, else manifest, else default.
        resolved: Any = chunk_size
        if resolved is None:
            manifest = store.raw_root / store.run_id / MANIFEST_FILENAME
            if manifest.is_file():
                try:
                    doc = json.loads(manifest.read_text(encoding="utf-8"))
                    if isinstance(doc, dict) and doc.get("chunk_size") is not None:
                        resolved = doc.get("chunk_size")
                except (OSError, ValueError):
                    resolved = None
        if resolved is None:
            resolved = DEFAULT_CHUNK_SIZE
        store.chunk_size = validate_chunk_size(resolved)
        store._init_paths()
        if not store.raw_run_dir.is_dir() or not store.processed_run_dir.is_dir():
            raise StorageError(f"No previous run {store.run_id!r} under {raw_root} / {processed_root}")
        store._manifest_params = None
        store._manifest_started_at = None
        store._init_streams()
        # If the caller passed a different chunk_size than the manifest used,
        # keep writing with the caller's size without touching old chunks.
        if chunk_size is not None and chunk_size != store._manifest_chunk_size_guess():
            for state in store._streams.values():
                state.set_chunk_size(store.chunk_size)
            logger.info("Resume chunk_size=%d differs from stored manifest; "
                        "continuing existing chunks with the new size", store.chunk_size)
        logger.info("Run store resumed: run_id=%s chunk_size=%d", store.run_id, store.chunk_size)
        return store

    def _manifest_chunk_size_guess(self) -> int | None:
        manifest = self.raw_run_dir / MANIFEST_FILENAME
        if not manifest.is_file():
            return None
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(doc, dict) and isinstance(doc.get("chunk_size"), int):
            return doc["chunk_size"]
        return None

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
        n = self._streams["raw_posts"].append(list(posts))
        self._refresh_manifest()
        return n

    def append_processed(self, posts: list[Mapping[str, Any]]) -> int:
        """Normalize and append processed records. Returns the number stored."""
        n = self._streams["processed_posts"].append([normalize_post(p) for p in posts])
        self._refresh_manifest()
        return n

    def append_page(self, posts: list[Mapping[str, Any]]) -> int:
        """Append one fetched page to both layers. Returns the number stored."""
        if not posts:
            return 0
        raw_count = self._streams["raw_posts"].append(list(posts))
        self._streams["processed_posts"].append([normalize_post(p) for p in posts])
        self._refresh_manifest()
        return raw_count

    def append_raw_comments(self, nodes: list[Mapping[str, Any]]) -> int:
        """Append verbatim comment tree nodes. Returns the number stored."""
        n = self._streams["raw_comments"].append(list(nodes))
        self._refresh_manifest()
        return n

    def append_processed_comments(self, records: list[Mapping[str, Any]]) -> int:
        """Append normalized comment records. Returns the number stored."""
        n = self._streams["processed_comments"].append(list(records))
        self._refresh_manifest()
        return n

    def write_manifest(self, params: Mapping[str, Any]) -> Path:
        """Record the run parameters next to the raw data."""
        self._manifest_params = dict(params)
        self._manifest_started_at = datetime.now(timezone.utc).isoformat()
        return self._write_manifest_doc()

    def _refresh_manifest(self) -> None:
        if self._manifest_params is None:
            return
        try:
            self._write_manifest_doc()
        except StorageError as exc:
            logger.warning("Could not refresh manifest for run %s: %s", self.run_id, exc)

    def _write_manifest_doc(self) -> Path:
        manifest = self.raw_run_dir / MANIFEST_FILENAME
        if self._manifest_started_at is None:
            self._manifest_started_at = datetime.now(timezone.utc).isoformat()
        doc = {
            "run_id": self.run_id,
            "started_at": self._manifest_started_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "chunk_size": self.chunk_size,
            "params": dict(self._manifest_params or {}),
            "chunks": {
                "raw_posts": self._streams["raw_posts"].inventory(self.raw_run_dir),
                "processed_posts": self._streams["processed_posts"].inventory(
                    self.processed_run_dir),
                "raw_comments": self._streams["raw_comments"].inventory(self.raw_run_dir),
                "processed_comments": self._streams["processed_comments"].inventory(
                    self.processed_run_dir),
            },
            "totals": {
                "raw_posts": self._streams["raw_posts"].total,
                "processed_posts": self._streams["processed_posts"].total,
                "raw_comments": self._streams["raw_comments"].total,
                "processed_comments": self._streams["processed_comments"].total,
            },
        }
        try:
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError(f"Could not write manifest {manifest}: {exc}") from exc
        return manifest

    def chunk_inventory(self) -> dict[str, Any]:
        """Current chunk files and totals for all four streams."""
        return {
            "chunk_size": self.chunk_size,
            "raw_posts": self._streams["raw_posts"].inventory(self.raw_run_dir),
            "processed_posts": self._streams["processed_posts"].inventory(
                self.processed_run_dir),
            "raw_comments": self._streams["raw_comments"].inventory(self.raw_run_dir),
            "processed_comments": self._streams["processed_comments"].inventory(
                self.processed_run_dir),
            "totals": {
                "raw_posts": self._streams["raw_posts"].total,
                "processed_posts": self._streams["processed_posts"].total,
                "raw_comments": self._streams["raw_comments"].total,
                "processed_comments": self._streams["processed_comments"].total,
            },
        }

    def validate_chunks(self) -> dict[str, Any]:
        """Check all chunks collectively: sizes, limits, layer agreement.

        Returns a dict with per-stream file counts, totals, any files over
        the limit (legacy files are reported separately as exempt), and
        whether raw/processed layers agree.
        """
        over_limit: list[str] = []
        legacy_over: list[str] = []
        for key in ("raw_posts", "processed_posts", "raw_comments", "processed_comments"):
            state = self._streams[key]
            for entry in state.inventory(
                    self.raw_run_dir if key.startswith("raw_") else self.processed_run_dir):
                if entry.get("legacy"):
                    if entry["records"] > self.chunk_size:
                        legacy_over.append(str(entry["file"]))
                elif entry["records"] > self.chunk_size:
                    over_limit.append(str(entry["file"]))
        totals = {
            "raw_posts": self.count_raw(),
            "processed_posts": self.count_processed(),
            "raw_comments": self.count_raw_comments(),
            "processed_comments": self.count_processed_comments(),
        }
        return {
            "run_id": self.run_id,
            "chunk_size": self.chunk_size,
            "totals": totals,
            "posts_agree": totals["raw_posts"] == totals["processed_posts"],
            "comments_agree": totals["raw_comments"] == totals["processed_comments"],
            "over_limit": over_limit,
            "legacy_over_limit": legacy_over,
            "ok": not over_limit and totals["raw_posts"] == totals["processed_posts"]
            and totals["raw_comments"] == totals["processed_comments"],
        }

    # -- reads (streaming) --------------------------------------------

    def _iter_stream(self, legacy: Path, chunk_dir: Path, prefix: str) -> Iterator[dict[str, Any]]:
        yield from self._iter_lines(legacy)
        for path in _list_chunk_files(chunk_dir, prefix):
            yield from self._iter_lines(path)

    def iter_raw(self) -> Iterator[dict[str, Any]]:
        return self._iter_stream(self.raw_file, self.raw_posts_dir, POSTS_PREFIX)

    def iter_processed(self) -> Iterator[dict[str, Any]]:
        return self._iter_stream(self.processed_file, self.processed_posts_dir, POSTS_PREFIX)

    def iter_raw_comments(self) -> Iterator[dict[str, Any]]:
        return self._iter_stream(self.raw_comments_file, self.raw_comments_dir, COMMENTS_PREFIX)

    def iter_processed_comments(self) -> Iterator[dict[str, Any]]:
        return self._iter_stream(self.processed_comments_file,
                                 self.processed_comments_dir, COMMENTS_PREFIX)

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
        """Legacy helper kept for tests: append directly to one file."""
        if not records:
            return 0
        _append_lines_to_file(path, records)
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
    "COMMENTS_PREFIX",
    "COMMENTS_SUBDIR",
    "DEFAULT_CHUNK_SIZE",
    "MANIFEST_FILENAME",
    "POSTS_PREFIX",
    "POSTS_SUBDIR",
    "PROCESSED_FIELDS",
    "RAW_FILENAME",
    "RunStore",
    "StorageError",
    "chunk_size_from_config",
    "new_run_id",
    "normalize_post",
    "validate_chunk_size",
]
