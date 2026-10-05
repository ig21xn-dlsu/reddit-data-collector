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
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

CHECKPOINT_VERSION = 1
CHECKPOINT_FILENAME = "checkpoint.json"
COMMENT_CHECKPOINT_FILENAME = "comments-checkpoint.json"


class CheckpointError(Exception):
    """Raised when a checkpoint cannot be written or is invalid."""


def checkpoint_path(checkpoint_dir: str | Path) -> Path:
    return Path(checkpoint_dir) / CHECKPOINT_FILENAME


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
) -> Path:
    """Persist a `PostPaginator.state()` snapshot atomically. Returns the path."""
    if not isinstance(paginator_state, Mapping):
        raise CheckpointError(
            f"paginator_state must be a mapping, got {type(paginator_state).__name__}"
        )
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise CheckpointError(f"run_id must be a non-empty string, got {run_id!r}")
    doc = {
        "version": CHECKPOINT_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "paginator": dict(paginator_state),
    }
    path = checkpoint_path(checkpoint_dir)
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

    Returns None when no checkpoint exists; raises CheckpointError if the
    file exists but is corrupt/incompatible.
    """
    path = checkpoint_path(checkpoint_dir)
    if not os.path.lexists(path):
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
    return {"version": doc["version"], "saved_at": doc.get("saved_at"),
            "run_id": doc.get("run_id"), "paginator": dict(paginator_state)}


def comment_checkpoint_path(checkpoint_dir: str | Path) -> Path:
    return Path(checkpoint_dir) / COMMENT_CHECKPOINT_FILENAME


def save_comment_checkpoint(
    checkpoint_dir: str | Path,
    state: Mapping[str, Any],
    run_id: str | None,
) -> Path:
    """Persist comment-phase progress atomically. Independent of the post checkpoint."""
    if not isinstance(state, Mapping):
        raise CheckpointError(
            f"comment state must be a mapping, got {type(state).__name__}"
        )
    if not isinstance(run_id, str) or not run_id.strip():
        raise CheckpointError(f"run_id must be a non-empty string, got {run_id!r}")
    completed = state.get("completed_post_ids", [])
    if not isinstance(completed, list) or any(not isinstance(i, str) for i in completed):
        raise CheckpointError("'completed_post_ids' must be a list of post id strings")
    doc = {
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
    return {"version": doc["version"], "saved_at": doc.get("saved_at"),
            "run_id": doc.get("run_id"), "comments": dict(state)}


def clear_checkpoint(checkpoint_dir: str | Path) -> bool:
    """Delete the checkpoint for a fresh start. Returns True if one existed."""
    path = checkpoint_path(checkpoint_dir)
    if not os.path.lexists(path):
        logger.info("No checkpoint to clear in %s", checkpoint_dir)
        return False
    try:
        if path.is_dir() and not path.is_symlink():
            raise CheckpointError(
                f"Checkpoint path is a directory: {path} (remove it manually to start over)"
            )
        path.unlink()
    except OSError as exc:
        raise CheckpointError(f"Could not delete checkpoint {path}: {exc}") from exc
    logger.info("Cleared checkpoint in %s for a fresh collection", checkpoint_dir)
    return True


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
    return errors


__all__ = [
    "CHECKPOINT_FILENAME",
    "CHECKPOINT_VERSION",
    "COMMENT_CHECKPOINT_FILENAME",
    "CheckpointError",
    "checkpoint_path",
    "clear_checkpoint",
    "clear_comment_checkpoint",
    "comment_checkpoint_path",
    "load_checkpoint",
    "load_checkpoint_doc",
    "load_comment_checkpoint",
    "save_checkpoint",
    "save_comment_checkpoint",
]
