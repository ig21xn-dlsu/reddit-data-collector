"""Simple JSON checkpoint store for resumable collection.

The collector paginates with `PostPaginator`, whose `state()` snapshot holds
everything needed to resume safely (filters, sort, time cursor, total, seen
ids). Call `save_checkpoint()` periodically (e.g. after each page); on the
next run, `load_checkpoint()` returns the snapshot to pass as
`PostPaginator(resume=...)`, which re-validates filters and skips already
seen ids, so resuming neither duplicates nor skips posts.

Crash safety: checkpoints are written to a temp file in the same directory
and moved into place with `os.replace()`, so a crash can never leave a
half-written checkpoint behind.

Named collections: each collection configuration gets its own persistent
checkpoint directory ``<checkpoint_root>/<Subreddit>-<after>-<before>/``
holding ``posts-checkpoint.json`` + ``comments-checkpoint.json``. Switching
subreddits therefore never deletes or overwrites another collection's
checkpoint. The legacy single-file layout (``checkpoint.json`` /
``comments-checkpoint.json`` directly under the root) is still readable but
is never written to; use :func:`migrate_legacy_checkpoint` for an explicit,
validated migration.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

CHECKPOINT_VERSION = 1
CHECKPOINT_FILENAME = "checkpoint.json"  # legacy single-file layout (read-only)
COMMENT_CHECKPOINT_FILENAME = "comments-checkpoint.json"  # shared by both layouts
POSTS_CHECKPOINT_FILENAME = "posts-checkpoint.json"  # per-collection layout


class CheckpointError(Exception):
    """Raised when a checkpoint cannot be written or is invalid."""


def checkpoint_path(checkpoint_dir: str | Path) -> Path:
    return Path(checkpoint_dir) / CHECKPOINT_FILENAME


def posts_checkpoint_path(checkpoint_dir: str | Path) -> Path:
    """Preferred per-collection posts checkpoint location."""
    return Path(checkpoint_dir) / POSTS_CHECKPOINT_FILENAME


def _posts_doc_path(checkpoint_dir: str | Path) -> Path | None:
    """Existing posts checkpoint file in a concrete dir (new name, else legacy)."""
    new = posts_checkpoint_path(checkpoint_dir)
    if os.path.lexists(new):
        return new
    old = checkpoint_path(checkpoint_dir)
    if os.path.lexists(old):
        return old
    return None


def _write_json_atomic(path: Path, doc: dict[str, Any]) -> None:
    """Write doc to path via temp file + rename. Cleans up temp on failure."""
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(path.parent), prefix=".checkpoint-", suffix=".tmp",
            delete=False, encoding="utf-8",
        ) as tmp:
            tmp_name = tmp.name
            json.dump(doc, tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except (OSError, ValueError, TypeError) as exc:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise CheckpointError(f"Could not write checkpoint to {path}: {exc}") from exc


def _ensure_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CheckpointError(f"Could not create checkpoint dir {path.parent}: {exc}") from exc


def save_checkpoint(
    checkpoint_dir: str | Path,
    paginator_state: Mapping[str, Any],
    run_id: str | None = None,
    collection: Mapping[str, Any] | None = None,
) -> Path:
    """Persist a `PostPaginator.state()` snapshot atomically. Returns the path.

    When ``collection`` metadata (see :func:`build_collection_meta`) is given,
    the per-collection file ``posts-checkpoint.json`` is written; otherwise
    the legacy ``checkpoint.json`` name is used. Both are readable by
    :func:`load_checkpoint_doc`.
    """
    if not isinstance(paginator_state, Mapping):
        raise CheckpointError(
            f"paginator_state must be a mapping, got {type(paginator_state).__name__}"
        )
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise CheckpointError(f"run_id must be a non-empty string, got {run_id!r}")
    if collection is not None and not isinstance(collection, Mapping):
        raise CheckpointError(f"collection must be a mapping, got {type(collection).__name__}")
    doc: dict[str, Any] = {
        "version": CHECKPOINT_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "paginator": dict(paginator_state),
    }
    if collection is not None:
        doc["collection"] = dict(collection)
    path = posts_checkpoint_path(checkpoint_dir) if collection is not None else checkpoint_path(checkpoint_dir)
    _ensure_parent(path)
    _write_json_atomic(path, doc)

    total = paginator_state.get("total_fetched")
    cursor = paginator_state.get("cursor")
    logger.info("Checkpoint saved: %s posts collected, cursor=%r (%s)", total, cursor, path)
    return path


def load_checkpoint(checkpoint_dir: str | Path) -> dict[str, Any] | None:
    """Load the paginator snapshot, or None when no checkpoint exists.

    Raises CheckpointError if the file exists but is corrupt/incompatible.
    """
    doc = load_checkpoint_doc(checkpoint_dir)
    if doc is None:
        return None
    return dict(doc["paginator"])


def load_checkpoint_doc(checkpoint_dir: str | Path) -> dict[str, Any] | None:
    """Load the full checkpoint document (version, saved_at, run_id, paginator).

    Reads ``posts-checkpoint.json`` first, then the legacy ``checkpoint.json``
    in the same directory. Returns None when neither exists; raises
    CheckpointError if the file exists but is corrupt/incompatible.
    """
    path = _posts_doc_path(checkpoint_dir)
    if path is None:
        return None
    if not path.is_file():
        raise CheckpointError(f"Checkpoint path is not a file: {path} (use --fresh to start over)")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CheckpointError(f"Could not read checkpoint {path}: {exc}") from exc
    except ValueError as exc:
        raise CheckpointError(
            f"Checkpoint {path} is not valid JSON: {exc} (use --fresh to start over)"
        ) from exc

    errors = _validate_document(doc)
    if errors:
        raise CheckpointError(
            f"Invalid checkpoint in {path}:\n- " + "\n- ".join(errors) + " (use --fresh to start over)"
        )

    paginator_state = doc["paginator"]
    logger.info(
        "Loaded checkpoint saved at %s: %s posts collected, cursor=%r",
        doc.get("saved_at"), paginator_state.get("total_fetched"), paginator_state.get("cursor"),
    )
    loaded: dict[str, Any] = {"version": doc["version"], "saved_at": doc.get("saved_at"),
                              "run_id": doc.get("run_id"), "paginator": dict(paginator_state),
                              "path": str(path)}
    if isinstance(doc.get("collection"), dict):
        loaded["collection"] = dict(doc["collection"])
    return loaded


def comment_checkpoint_path(checkpoint_dir: str | Path) -> Path:
    return Path(checkpoint_dir) / COMMENT_CHECKPOINT_FILENAME


def save_comment_checkpoint(
    checkpoint_dir: str | Path,
    state: Mapping[str, Any],
    run_id: str | None,
    collection: Mapping[str, Any] | None = None,
) -> Path:
    """Persist comment-phase progress atomically. Independent of the post checkpoint."""
    if not isinstance(state, Mapping):
        raise CheckpointError(
            f"comment state must be a mapping, got {type(state).__name__}"
        )
    if not isinstance(run_id, str) or not run_id.strip():
        raise CheckpointError(f"run_id must be a non-empty string, got {run_id!r}")
    if collection is not None and not isinstance(collection, Mapping):
        raise CheckpointError(f"collection must be a mapping, got {type(collection).__name__}")
    completed = state.get("completed_post_ids", [])
    if not isinstance(completed, list) or any(not isinstance(i, str) for i in completed):
        raise CheckpointError("'completed_post_ids' must be a list of post id strings")
    doc: dict[str, Any] = {
        "version": CHECKPOINT_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "comments": {
            "completed_post_ids": list(completed),
            "posts_completed": state.get("posts_completed", len(completed)),
            "posts_skipped_empty": state.get("posts_skipped_empty", 0),
            "comments_collected": state.get("comments_collected", 0),
        },
    }
    if collection is not None:
        doc["collection"] = dict(collection)
    path = comment_checkpoint_path(checkpoint_dir)
    _ensure_parent(path)
    _write_json_atomic(path, doc)
    logger.info(
        "Comment checkpoint saved: %s posts done, %s comments (%s)",
        doc["comments"]["posts_completed"], doc["comments"]["comments_collected"], path,
    )
    return path


def load_comment_checkpoint(checkpoint_dir: str | Path) -> dict[str, Any] | None:
    """Load the comment-phase document, or None when absent.

    Raises CheckpointError if the file exists but is corrupt/incompatible.
    """
    path = comment_checkpoint_path(checkpoint_dir)
    if not os.path.lexists(path):
        return None
    if not path.is_file():
        raise CheckpointError(f"Comment checkpoint path is not a file: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CheckpointError(f"Could not read comment checkpoint {path}: {exc}") from exc
    except ValueError as exc:
        raise CheckpointError(
            f"Comment checkpoint {path} is not valid JSON: {exc}"
        ) from exc
    errors = _validate_comment_document(doc)
    if errors:
        raise CheckpointError(
            f"Invalid comment checkpoint in {path}:\n- " + "\n- ".join(errors)
        )
    state = doc["comments"]
    logger.info(
        "Loaded comment checkpoint: %s posts done, %s comments (run %s)",
        state.get("posts_completed"), state.get("comments_collected"), doc.get("run_id"),
    )
    loaded: dict[str, Any] = {"version": doc["version"], "saved_at": doc.get("saved_at"),
                              "run_id": doc.get("run_id"), "comments": dict(state),
                              "path": str(path)}
    if isinstance(doc.get("collection"), dict):
        loaded["collection"] = dict(doc["collection"])
    return loaded


def clear_checkpoint(checkpoint_dir: str | Path) -> bool:
    """Delete the posts checkpoint in a concrete directory. Returns True if one existed.

    Clears both the per-collection (``posts-checkpoint.json``) and legacy
    (``checkpoint.json``) file when present; other collections are untouched.
    """
    cleared = False
    for path in (posts_checkpoint_path(checkpoint_dir), checkpoint_path(checkpoint_dir)):
        if not os.path.lexists(path):
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                raise CheckpointError(
                    f"Checkpoint path is a directory: {path} (remove it manually to start over)"
                )
            path.unlink()
        except OSError as exc:
            raise CheckpointError(f"Could not delete checkpoint {path}: {exc}") from exc
        cleared = True
    if cleared:
        logger.info("Cleared checkpoint in %s for a fresh collection", checkpoint_dir)
    else:
        logger.info("No checkpoint to clear in %s", checkpoint_dir)
    return cleared


def clear_comment_checkpoint(checkpoint_dir: str | Path) -> bool:
    """Delete the comment checkpoint for a fresh start. Returns True if one existed."""
    path = comment_checkpoint_path(checkpoint_dir)
    if not os.path.lexists(path):
        logger.info("No comment checkpoint to clear in %s", checkpoint_dir)
        return False
    try:
        if path.is_dir() and not path.is_symlink():
            raise CheckpointError(
                f"Comment checkpoint path is a directory: {path} (remove it manually to start over)"
            )
        path.unlink()
    except OSError as exc:
        raise CheckpointError(f"Could not delete comment checkpoint {path}: {exc}") from exc
    logger.info("Cleared comment checkpoint in %s for a fresh collection", checkpoint_dir)
    return True


def _validate_comment_document(doc: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return [f"comment checkpoint root must be an object, got {type(doc).__name__}"]
    if doc.get("version") != CHECKPOINT_VERSION:
        errors.append(f"'version' must be {CHECKPOINT_VERSION}, got {doc.get('version')!r}")
    if not isinstance(doc.get("run_id"), str) or not doc.get("run_id").strip():
        errors.append(f"'run_id' must be a non-empty string, got {doc.get('run_id')!r}")
    state = doc.get("comments")
    if not isinstance(state, dict):
        errors.append("'comments' must be an object holding comment progress")
        return errors
    completed = state.get("completed_post_ids", [])
    if not isinstance(completed, list) or any(not isinstance(i, str) for i in completed):
        errors.append("'comments.completed_post_ids' must be a list of post id strings")
    for key in ("posts_completed", "posts_skipped_empty", "comments_collected"):
        value = state.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"'comments.{key}' must be an integer >= 0, got {value!r}")
    errors.extend(_validate_collection_block(doc))
    return errors


# -- named per-collection checkpoints -----------------------------------

OPEN_DATE_TOKEN = "open"


def normalize_subreddit_display(value: Any) -> str:
    """Strip an ``r/`` prefix and surrounding whitespace (preserves case)."""
    if not isinstance(value, str):
        raise CheckpointError(f"subreddit must be a non-empty string, got {value!r}")
    text = value.strip()
    if len(text) >= 2 and text[0].lower() == "r" and text[1] == "/":
        text = text[2:].strip()
    if not text:
        raise CheckpointError(f"subreddit must be a non-empty string, got {value!r}")
    return text


def normalize_subreddit_key(value: Any) -> str:
    """Case-insensitive identity key for a subreddit value."""
    return normalize_subreddit_display(value).lower()


def sanitize_name_part(text: str, max_len: int = 64) -> str:
    """Make a string safe for use in a checkpoint directory name."""
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "_", text.strip()).strip("_-")
    if not cleaned:
        return "collection"
    return cleaned[:max_len].strip("_-") or "collection"


def date_token(value: Any) -> str:
    """Stable short token for an after/before value (for naming + comparison)."""
    if value is None:
        return OPEN_DATE_TOKEN
    if isinstance(value, bool):
        return "invalid"
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return sanitize_name_part(f"epoch-{value}", 32)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return OPEN_DATE_TOKEN
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
        if m:
            return m.group(1)
        try:
            num = float(text)
        except ValueError:
            num = None
        if num is not None:
            return date_token(num)
        return sanitize_name_part(re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-"), 32)
    return sanitize_name_part(str(value), 32)


def collection_name_for_config(config: Mapping[str, Any]) -> str:
    """Build ``<Subreddit>-<after>-<before>`` for a loaded config."""
    if not isinstance(config, Mapping):
        raise CheckpointError(f"config must be a mapping, got {type(config).__name__}")
    sub = sanitize_name_part(normalize_subreddit_display(config.get("subreddit")))
    name = f"{sub}-{date_token(config.get('after'))}-{date_token(config.get('before'))}"
    return name[:128].strip("_-") or "collection"


def checkpoint_dir_for_config(checkpoint_root: str | Path, config: Mapping[str, Any]) -> Path:
    """Per-collection checkpoint directory for a config (creates nothing)."""
    return Path(checkpoint_root) / collection_name_for_config(config)


def checkpoint_dir_for_name(checkpoint_root: str | Path, name: str) -> Path:
    """Per-collection checkpoint directory for an explicit checkpoint name."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise CheckpointError("checkpoint name must be a non-empty string")
    return Path(checkpoint_root) / sanitize_name_part(cleaned, 128)


def find_collection_dir(checkpoint_root: str | Path, name: str) -> Path | None:
    """Locate an existing collection dir: exact name, else a unique match.

    Subreddit spelling may vary in case (``Philippines`` vs ``r/philippines``)
    while denoting the same collection, so when the exact directory does not
    exist, a single case-insensitive match is used. Multiple matches (or none)
    return None / the exact path respectively — never guess across collections.
    """
    root = Path(checkpoint_root)
    exact = checkpoint_dir_for_name(root, name)
    if exact.is_dir():
        return exact
    if not root.is_dir():
        return None
    lowered = sanitize_name_part(name, 128).lower()
    matches = [p for p in root.iterdir()
               if p.is_dir() and p.name.lower() == lowered]
    if len(matches) == 1:
        return matches[0]
    return None


def legacy_post_path(checkpoint_root: str | Path) -> Path:
    return Path(checkpoint_root) / CHECKPOINT_FILENAME


def legacy_comment_path(checkpoint_root: str | Path) -> Path:
    return Path(checkpoint_root) / COMMENT_CHECKPOINT_FILENAME


def has_legacy_checkpoint(checkpoint_root: str | Path) -> bool:
    root = Path(checkpoint_root)
    return (root / CHECKPOINT_FILENAME).exists() or (root / COMMENT_CHECKPOINT_FILENAME).exists()


def build_collection_meta(config: Mapping[str, Any], name: str | None = None) -> dict[str, Any]:
    """Snapshot of the dataset-defining configuration stored in checkpoints.

    Never guess: the stored values are compared verbatim (modulo subreddit
    case / ``r/`` prefix and equivalent date representations) before any
    resume or comment collection starts.
    """
    if not isinstance(config, Mapping):
        raise CheckpointError(f"config must be a mapping, got {type(config).__name__}")
    label = name or collection_name_for_config(config)
    display = normalize_subreddit_display(config.get("subreddit"))
    return {
        "name": label,
        "subreddit": display,
        "subreddit_norm": display.lower(),
        "after": config.get("after"),
        "before": config.get("before"),
        "after_token": date_token(config.get("after")),
        "before_token": date_token(config.get("before")),
        "title": config.get("title"),
        "query": config.get("query"),
        "selftext": config.get("selftext"),
        "author": config.get("author"),
        "sort": config.get("sort"),
        "limit": config.get("limit"),
    }


def _filters_from_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "subreddit": config.get("subreddit"),
        "title": config.get("title"),
        "query": config.get("query"),
        "selftext": config.get("selftext"),
        "author": config.get("author"),
        "after": config.get("after"),
        "before": config.get("before"),
    }


def validate_collection_compatible(
    doc: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    selected_name: str | None = None,
) -> None:
    """Reject a checkpoint whose dataset identity differs from ``config``.

    Compares subreddit (case-insensitive, ``r/``-insensitive), start/end
    dates (equivalent representations accepted), title/query/selftext/author
    filters, and sort — using the stored ``collection`` metadata when present,
    else the stored paginator filters (legacy checkpoints). Raises
    CheckpointError with a ``Checkpoint mismatch: ...`` message before any
    network request or checkpoint/dataset mutation.
    """
    if doc is None:
        raise CheckpointError("No checkpoint to validate: nothing stored for this collection.")
    label = selected_name or str(doc.get("collection", {}).get("name", "selected"))
    if isinstance(doc.get("collection"), dict):
        meta = doc["collection"]
        actual_sub = str(meta.get("subreddit", "?"))
        expected_sub = normalize_subreddit_display(config.get("subreddit"))
        if str(meta.get("subreddit_norm", actual_sub).lower()) != expected_sub.lower():
            raise CheckpointError(
                f"Checkpoint mismatch: selected checkpoint {label!r} belongs to "
                f"r/{actual_sub}, but the current configuration specifies "
                f"r/{expected_sub}. Select a compatible checkpoint or change the subreddit."
            )
        for field in ("after_token", "before_token"):
            cfg_tok = date_token(config.get(field.split("_")[0]))
            if str(meta.get(field, cfg_tok)) != cfg_tok:
                which = "start date" if field == "after_token" else "end date"
                raise CheckpointError(
                    f"Checkpoint mismatch: selected checkpoint {label!r} has {which} "
                    f"{meta.get(field)!r}, but the current configuration specifies "
                    f"{cfg_tok!r}. Select a compatible checkpoint or change the date range."
                )
        for field in ("title", "query", "selftext", "author", "sort"):
            if meta.get(field, config.get(field)) != config.get(field):
                raise CheckpointError(
                    f"Checkpoint mismatch: selected checkpoint {label!r} was collected "
                    f"with {field}={meta.get(field)!r}, but the current configuration "
                    f"specifies {field}={config.get(field)!r}. Select a compatible "
                    f"checkpoint or change the configuration."
                )
        return
    # Legacy checkpoint: no stored collection block — compare paginator state.
    paginator = doc.get("paginator")
    if not isinstance(paginator, Mapping):
        raise CheckpointError(
            f"Checkpoint mismatch: selected checkpoint {label!r} has no usable "
            f"collection metadata; migrate it explicitly rather than guessing."
        )
    saved_filters = paginator.get("filters")
    if not isinstance(saved_filters, Mapping):
        raise CheckpointError(
            f"Checkpoint mismatch: selected checkpoint {label!r} has no stored "
            f"filters; migrate it explicitly rather than guessing."
        )
    expected = _filters_from_config(config)
    try:
        actual_sub_key = normalize_subreddit_key(saved_filters.get("subreddit"))
        expected_sub_key = normalize_subreddit_key(expected.get("subreddit"))
    except CheckpointError:
        actual_sub_key, expected_sub_key = None, None
    if actual_sub_key != expected_sub_key:
        raise CheckpointError(
            f"Checkpoint mismatch: selected checkpoint {label!r} belongs to "
            f"r/{saved_filters.get('subreddit')}, but the current configuration specifies "
            f"r/{expected.get('subreddit')}. Select a compatible checkpoint or change the subreddit."
        )
    for field in ("after", "before"):
        if date_token(saved_filters.get(field)) != date_token(expected.get(field)):
            which = "start date" if field == "after" else "end date"
            raise CheckpointError(
                f"Checkpoint mismatch: selected checkpoint {label!r} has {which} "
                f"{saved_filters.get(field)!r}, but the current configuration specifies "
                f"{expected.get(field)!r}. Select a compatible checkpoint or change the date range."
            )
    for field in ("title", "query", "selftext", "author"):
        if saved_filters.get(field) != expected.get(field):
            raise CheckpointError(
                f"Checkpoint mismatch: selected checkpoint {label!r} was collected "
                f"with {field}={saved_filters.get(field)!r}, but the current configuration "
                f"specifies {field}={expected.get(field)!r}. Select a compatible "
                f"checkpoint or change the configuration."
            )
    if paginator.get("sort") != config.get("sort"):
        raise CheckpointError(
            f"Checkpoint mismatch: selected checkpoint {label!r} was collected "
            f"with sort={paginator.get('sort')!r}, but the current configuration "
            f"specifies sort={config.get('sort')!r}."
        )


def _summarize_posts_doc(doc: Mapping[str, Any] | None) -> dict[str, Any]:
    if doc is None:
        return {"present": False}
    paginator = doc.get("paginator", {}) if isinstance(doc.get("paginator"), dict) else {}
    max_posts = paginator.get("max_posts")
    total = paginator.get("total_fetched", 0)
    status = "in_progress"
    if isinstance(max_posts, int) and isinstance(total, int) and max_posts > 0 and total >= max_posts:
        status = "complete"
    collection = doc.get("collection") if isinstance(doc.get("collection"), dict) else {}
    filters = paginator.get("filters", {}) if isinstance(paginator.get("filters"), dict) else {}
    sub = collection.get("subreddit", filters.get("subreddit"))
    return {
        "present": True,
        "run_id": doc.get("run_id"),
        "saved_at": doc.get("saved_at"),
        "total_fetched": total,
        "cursor": paginator.get("cursor"),
        "max_posts": max_posts,
        "status": status,
        "subreddit": sub,
        "collection": dict(collection),
        "legacy": not isinstance(doc.get("collection"), dict),
    }


def _summarize_comment_doc(doc: Mapping[str, Any] | None) -> dict[str, Any]:
    if doc is None:
        return {"present": False}
    state = doc.get("comments", {}) if isinstance(doc.get("comments"), dict) else {}
    return {
        "present": True,
        "run_id": doc.get("run_id"),
        "saved_at": doc.get("saved_at"),
        "posts_completed": state.get("posts_completed", 0),
        "posts_skipped_empty": state.get("posts_skipped_empty", 0),
        "comments_collected": state.get("comments_collected", 0),
    }


def _safe_load_posts(directory: Path) -> dict[str, Any] | None | str:
    try:
        return load_checkpoint_doc(directory)
    except CheckpointError as exc:
        return f"error: {exc}"
    except Exception as exc:  # never let listing crash the GUI/CLI
        return f"error: {exc}"


def _safe_load_comments(directory: Path) -> dict[str, Any] | None | str:
    try:
        return load_comment_checkpoint(directory)
    except CheckpointError as exc:
        return f"error: {exc}"
    except Exception as exc:  # never let listing crash the GUI/CLI
        return f"error: {exc}"


def list_collections(checkpoint_root: str | Path) -> list[dict[str, Any]]:
    """Discover saved per-collection checkpoints (plus a legacy entry).

    Never raises for missing/malformed data: corrupt entries are reported
    with an ``error`` field so the GUI/CLI can display them safely.
    """
    root = Path(checkpoint_root)
    entries: list[dict[str, Any]] = []
    if root.is_dir():
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            posts_path = child / POSTS_CHECKPOINT_FILENAME
            legacy_path = child / CHECKPOINT_FILENAME
            comments_path = child / COMMENT_CHECKPOINT_FILENAME
            if not posts_path.exists() and not legacy_path.exists() and not comments_path.exists():
                continue
            posts = _safe_load_posts(child)
            comments = _safe_load_comments(child)
            if isinstance(posts, str):
                entry: dict[str, Any] = {"name": child.name, "dir": str(child),
                                         "error": posts, "has_posts": False,
                                         "has_comments": comments is not None and not isinstance(comments, str)}
            else:
                summary = _summarize_posts_doc(posts)
                entry = {"name": child.name, "dir": str(child),
                         "has_posts": posts is not None,
                         "has_comments": comments is not None and not isinstance(comments, str),
                         "posts": summary,
                         "comments": _summarize_comment_doc(
                             comments if not isinstance(comments, str) else None),
                         "saved_at": (posts or {}).get("saved_at") if isinstance(posts, dict) else None,
                         "run_id": (posts or {}).get("run_id") if isinstance(posts, dict) else None}
                if isinstance(comments, str):
                    entry["comments_error"] = comments
            entries.append(entry)
    if has_legacy_checkpoint(root):
        posts = _safe_load_posts(root)
        comments = _safe_load_comments(root)
        entries.append({
            "name": "legacy", "legacy": True, "dir": str(root),
            "has_posts": posts is not None and not isinstance(posts, str),
            "has_comments": comments is not None and not isinstance(comments, str),
            "posts": _summarize_posts_doc(posts if not isinstance(posts, str) else None),
            "comments": _summarize_comment_doc(comments if not isinstance(comments, str) else None),
            "saved_at": (posts or {}).get("saved_at") if isinstance(posts, dict) else None,
            "run_id": (posts or {}).get("run_id") if isinstance(posts, dict) else None,
            **({"error": posts} if isinstance(posts, str) else {}),
        })
    return entries


def migrate_legacy_checkpoint(
    checkpoint_root: str | Path, config: Mapping[str, Any]
) -> Path:
    """Copy legacy root checkpoints into the config's collection directory.

    Safe by design: validates compatibility first, refuses when the target
    already has checkpoints, and copies (the legacy files are left in place).
    Returns the target directory.
    """
    root = Path(checkpoint_root)
    if not has_legacy_checkpoint(root):
        raise CheckpointError(f"No legacy checkpoint found in {root}.")
    target = checkpoint_dir_for_config(root, config)
    if ((target / POSTS_CHECKPOINT_FILENAME).exists() or (target / CHECKPOINT_FILENAME).exists()
            or (target / COMMENT_CHECKPOINT_FILENAME).exists()):
        raise CheckpointError(
            f"Checkpoint {collection_name_for_config(config)!r} already exists in {target}: "
            f"migration would overwrite it. Resume it instead."
        )
    legacy_posts = load_checkpoint_doc(root)
    if legacy_posts is None and not (root / COMMENT_CHECKPOINT_FILENAME).exists():
        raise CheckpointError(f"No legacy checkpoint found in {root}.")
    if legacy_posts is not None:
        validate_collection_compatible(legacy_posts, config, selected_name="legacy")
    legacy_comments = load_comment_checkpoint(root)
    if legacy_comments is not None and legacy_posts is not None:
        if legacy_comments.get("run_id") != legacy_posts.get("run_id"):
            raise CheckpointError(
                "Legacy post and comment checkpoints belong to different runs "
                f"({legacy_posts.get('run_id')!r} vs {legacy_comments.get('run_id')!r}): "
                "migrate them explicitly rather than guessing."
            )
    meta = build_collection_meta(config)
    target.mkdir(parents=True, exist_ok=True)
    if legacy_posts is not None:
        save_checkpoint(target, legacy_posts["paginator"], run_id=legacy_posts.get("run_id"),
                        collection=meta)
    if legacy_comments is not None:
        save_comment_checkpoint(target, legacy_comments["comments"],
                                legacy_comments.get("run_id"), collection=meta)
    logger.info("Migrated legacy checkpoint from %s to %s (originals kept)", root, target)
    return target


def _validate_collection_block(doc: Any) -> list[str]:
    errors: list[str] = []
    block = doc.get("collection")
    if block is None:
        return errors
    if not isinstance(block, dict):
        return ["'collection' must be an object holding the collection identity"]
    if not isinstance(block.get("name"), str) or not block.get("name").strip():
        errors.append("'collection.name' must be a non-empty string")
    if not isinstance(block.get("subreddit"), str) or not block.get("subreddit").strip():
        errors.append("'collection.subreddit' must be a non-empty string")
    return errors


def _validate_document(doc: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return [f"checkpoint root must be an object, got {type(doc).__name__}"]
    if doc.get("version") != CHECKPOINT_VERSION:
        errors.append(f"'version' must be {CHECKPOINT_VERSION}, got {doc.get('version')!r}")
    run_id = doc.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        errors.append(f"'run_id' must be a non-empty string or null, got {run_id!r}")
    state = doc.get("paginator")
    if not isinstance(state, dict):
        errors.append("'paginator' must be an object holding the paginator snapshot")
        return errors
    if not isinstance(state.get("filters"), dict):
        errors.append("'paginator.filters' must be an object")
    if not isinstance(state.get("sort"), str):
        errors.append(f"'paginator.sort' must be a string, got {state.get('sort')!r}")
    cursor = state.get("cursor")
    if cursor is not None and (
        isinstance(cursor, bool) or not isinstance(cursor, (str, int, float))
    ):
        errors.append(f"'paginator.cursor' must be a string, number or null, got {cursor!r}")
    total = state.get("total_fetched")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        errors.append(f"'paginator.total_fetched' must be an integer >= 0, got {total!r}")
    seen = state.get("seen_ids", [])
    if not isinstance(seen, list) or any(not isinstance(i, str) for i in seen):
        errors.append("'paginator.seen_ids' must be a list of post id strings")
    errors.extend(_validate_collection_block(doc))
    return errors


__all__ = [
    "CHECKPOINT_FILENAME",
    "CHECKPOINT_VERSION",
    "COMMENT_CHECKPOINT_FILENAME",
    "OPEN_DATE_TOKEN",
    "POSTS_CHECKPOINT_FILENAME",
    "CheckpointError",
    "build_collection_meta",
    "checkpoint_dir_for_config",
    "checkpoint_dir_for_name",
    "checkpoint_path",
    "clear_checkpoint",
    "clear_comment_checkpoint",
    "collection_name_for_config",
    "comment_checkpoint_path",
    "date_token",
    "find_collection_dir",
    "has_legacy_checkpoint",
    "legacy_comment_path",
    "legacy_post_path",
    "list_collections",
    "load_checkpoint",
    "load_checkpoint_doc",
    "load_comment_checkpoint",
    "migrate_legacy_checkpoint",
    "normalize_subreddit_display",
    "normalize_subreddit_key",
    "posts_checkpoint_path",
    "sanitize_name_part",
    "save_checkpoint",
    "save_comment_checkpoint",
    "validate_collection_compatible",
]
