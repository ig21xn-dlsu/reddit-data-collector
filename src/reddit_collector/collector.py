"""Collection loop: paginator -> storage -> checkpoints.

This module owns the actual collection logic so the CLI (`__main__.py`)
stays thin (argument parsing, exit codes, output formatting only).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Mapping

from .checkpoint import (
    CheckpointError,
    clear_checkpoint,
    clear_comment_checkpoint,
    load_checkpoint_doc,
    load_comment_checkpoint,
    save_checkpoint,
    save_comment_checkpoint,
)
from .client import ArcticShiftClient, ArcticShiftError
from .comments import CommentError, flatten_comment_tree, normalize_comment
from .paginator import PostPaginator
from .storage import RunStore, StorageError

logger = logging.getLogger(__name__)


class CollectorError(Exception):
    """Raised for collection workflow problems (stale checkpoint, missing run...)."""


def collect_new(
    config: Mapping[str, Any],
    fresh: bool = False,
    client: ArcticShiftClient | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Start a collection. Refuses when a checkpoint exists unless fresh=True.

    should_stop is an optional cooperative-stop hook (e.g. GUI Stop button):
    when it returns True, the current page is finished, checkpointed, and the
    run pauses cleanly. None (default) preserves the old run-to-completion.
    """
    checkpoint_dir = config["output"]["checkpoint_dir"]
    try:
        existing = load_checkpoint_doc(checkpoint_dir)
    except CheckpointError as exc:
        raise CollectorError(str(exc)) from exc
    if existing is not None and not fresh:
        raise CollectorError(
            f"A checkpoint already exists in {checkpoint_dir}: "
            "use 'resume' to continue it or 'collect --fresh' to start over."
        )
    if fresh:
        # Fresh means fresh for BOTH phases: a stale comment checkpoint from
        # another run must never carry over into the new run.
        clear_checkpoint(checkpoint_dir)
        clear_comment_checkpoint(checkpoint_dir)
        logger.info("Starting a completely new collection (--fresh)")
    else:
        logger.info("No checkpoint found, starting a new collection")
    return _run(config, resume_state=None, run_id=None, client=client, should_stop=should_stop)


def resume_collection(
    config: Mapping[str, Any],
    client: ArcticShiftClient | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Continue a previous collection from its checkpoint. Requires one."""
    checkpoint_dir = config["output"]["checkpoint_dir"]
    try:
        doc = load_checkpoint_doc(checkpoint_dir)
    except CheckpointError as exc:
        raise CollectorError(str(exc)) from exc
    if doc is None:
        raise CollectorError(
            f"No checkpoint found in {checkpoint_dir}: nothing to resume "
            "(use 'collect' to start a new collection)."
        )
    state = doc["paginator"]
    logger.info(
        "Resuming collection from checkpoint: %s posts collected, cursor=%r",
        state.get("total_fetched"), state.get("cursor"),
    )
    return _run(config, resume_state=state, run_id=doc.get("run_id"), client=client,
                should_stop=should_stop)


def _run(
    config: Mapping[str, Any],
    resume_state: Mapping[str, Any] | None,
    run_id: str | None,
    client: ArcticShiftClient | None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    client = client if client is not None else ArcticShiftClient.from_config(config)
    paginator = PostPaginator.from_config(config, client, resume=resume_state)
    output = config["output"]
    checkpoint_dir = output["checkpoint_dir"]
    if resume_state is None:
        store = RunStore.from_config(config)
        store.write_manifest({
            "subreddit": config.get("subreddit"),
            "title": config.get("title"),
            "query": config.get("query"),
            "selftext": config.get("selftext"),
            "after": config.get("after"),
            "before": config.get("before"),
            "sort": config.get("sort"),
            "limit": config.get("limit"),
            "max_posts": config.get("max_posts"),
        })
        logger.info("Run %s: storing raw data in %s", store.run_id, store.raw_file.parent)
    else:
        if not run_id:
            raise CollectorError("Checkpoint has no run_id; cannot resume into the same run.")
        try:
            store = RunStore.existing(output["raw_dir"], output["processed_dir"], run_id)
        except StorageError as exc:
            raise CollectorError(str(exc)) from exc
        # Crash-window repair: a page may have been appended without its
        # checkpoint being saved. Union stored ids into seen state (streamed,
        # one pass) so replayed pages are skipped instead of duplicated.
        reconciled = paginator.reconcile_stored(
            record.get("id") for record in store.iter_processed()
        )
        if reconciled:
            save_checkpoint(checkpoint_dir, paginator.state(), run_id=store.run_id)

    pages = 0
    collected = 0
    stopped = False
    for page in paginator.iter_pages():
        store.append_page(page)
        pages += 1
        collected += len(page)
        save_checkpoint(checkpoint_dir, paginator.state(), run_id=store.run_id)
        if should_stop is not None and should_stop():
            # Stop only between finished, checkpointed pages: nothing is lost
            # (current page stored) and nothing duplicates on resume.
            stopped = True
            logger.info(
                "Stop requested: pausing after page %d (%d new posts this run, %d total)",
                pages, collected, paginator.total_fetched,
            )
            break

    summary = {
        "run_id": store.run_id,
        "pages": pages,
        "posts_this_run": collected,
        "total_posts": paginator.total_fetched,
        "stopped": stopped,
    }
    if stopped:
        logger.info(
            "Collection stopped by user: run %s, %d total (resume to continue)",
            store.run_id, paginator.total_fetched,
        )
    else:
        logger.info(
            "Collection finished: run %s, %d new posts this run (%d total)",
            store.run_id, collected, paginator.total_fetched,
        )
    return summary


def collect_comments(
    config: Mapping[str, Any],
    run_id: str | None = None,
    max_comment_posts: int | None = None,
    skip_empty_posts: bool | None = None,
    client: ArcticShiftClient | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Phase 2: fetch comment trees for a previous run's posts.

    Reads post IDs from the run's own processed posts (never re-searches).
    Progress lives in an independent comment checkpoint, so post and comment
    collection resume independently. Completed posts are never re-requested;
    already-stored comments are reconciled before starting (crash repair).
    Explicit arguments override config["comments"]; None means "use config".
    """
    comments_cfg = config.get("comments", {})
    if not isinstance(comments_cfg, Mapping):
        raise CollectorError("'comments' must be a mapping")
    tree_limit = comments_cfg.get("tree_limit", 9999)
    if max_comment_posts is None:
        max_comment_posts = comments_cfg.get("max_comment_posts")
    if skip_empty_posts is None:
        skip_empty_posts = comments_cfg.get("skip_empty_posts", True)
    if max_comment_posts is not None and (
        not isinstance(max_comment_posts, int) or isinstance(max_comment_posts, bool)
        or max_comment_posts < 1
    ):
        raise CollectorError(f"max_comment_posts must be a positive integer, got {max_comment_posts!r}")

    output = config["output"]
    checkpoint_dir = output["checkpoint_dir"]
    rid = (run_id or "").strip() if run_id else ""
    if not rid:
        try:
            doc = load_checkpoint_doc(checkpoint_dir)
        except CheckpointError as exc:
            raise CollectorError(str(exc)) from exc
        if doc is None or not doc.get("run_id"):
            raise CollectorError(
                "No post checkpoint found: collect posts first, or pass an explicit run id."
            )
        rid = doc["run_id"]
    try:
        store = RunStore.existing(output["raw_dir"], output["processed_dir"], rid)
    except StorageError as exc:
        raise CollectorError(str(exc)) from exc

    try:
        cdoc = load_comment_checkpoint(checkpoint_dir)
    except CheckpointError as exc:
        raise CollectorError(str(exc)) from exc
    completed: set[str] = set()
    prev_comments = 0
    fetched_base = 0
    skipped_base = 0
    if cdoc is not None:
        if cdoc.get("run_id") != store.run_id:
            raise CollectorError(
                f"Comment checkpoint belongs to run {cdoc.get('run_id')!r}, "
                f"not {store.run_id!r}."
            )
        completed = set(cdoc["comments"].get("completed_post_ids", []))
        prev_comments = cdoc["comments"].get("comments_collected", 0)
        fetched_base = cdoc["comments"].get("posts_completed", 0)
        skipped_base = cdoc["comments"].get("posts_skipped_empty", 0)
    # Crash-window repair: a post's comments may have been appended without
    # its completion being checkpointed. Union stored post ids (streamed,
    # one pass) so replayed posts are skipped instead of duplicated.
    repaired_before = len(completed)
    for record in store.iter_processed_comments():
        pid = record.get("post_id")
        if isinstance(pid, str) and pid.strip():
            completed.add(pid.strip())
    repaired = len(completed) - repaired_before

    client = client if client is not None else ArcticShiftClient.from_config(config)
    attempted = 0
    completed_new = 0
    already = 0
    skipped_empty = 0
    comments = 0
    stopped = False
    posts_seen = 0
    posts_usable = 0

    def _checkpoint() -> None:
        _save_comment_progress(
            checkpoint_dir, store.run_id, completed,
            fetched_base + completed_new, skipped_base + skipped_empty,
            prev_comments + comments,
        )
    if repaired:
        logger.info("Crash repair: %d already-stored post(s) marked complete", repaired)
        _checkpoint()
    for post in store.iter_processed():
        pid = post.get("id")
        pid = str(pid).strip() if pid is not None and str(pid).strip() else None
        posts_seen += 1
        if pid is None:
            logger.warning("Skipping processed record without a usable post id")
            continue
        posts_usable += 1
        if pid in completed:
            already += 1
            continue
        if max_comment_posts is not None and attempted >= max_comment_posts:
            logger.info("Reached max_comment_posts=%d, stopping", max_comment_posts)
            break
        num_comments = post.get("num_comments")
        if skip_empty_posts and num_comments == 0:
            skipped_empty += 1
            completed.add(pid)
            _checkpoint()
            continue
        payload = client.get_comment_tree(link_id=pid, limit=tree_limit)
        try:
            flat = flatten_comment_tree(payload)
        except CommentError as exc:
            raise CollectorError(f"Post {pid}: unusable comment tree: {exc}") from exc
        records = [normalize_comment(node, pid, depth, path) for node, depth, path in flat]
        store.append_raw_comments([node for node, _, _ in flat])
        store.append_processed_comments(records)
        attempted += 1
        completed_new += 1
        completed.add(pid)
        comments += len(records)
        logger.info("Post %s: %d comments (%d posts done)", pid, len(records), len(completed))
        _checkpoint()
        if should_stop is not None and should_stop():
            stopped = True
            logger.info("Stop requested: pausing comment collection after %d posts", attempted)
            break

    if posts_seen == 0:
        raise CollectorError(
            f"Run {store.run_id!r} has no processed posts to collect comments for: "
            "collect posts first, or check --run."
        )
    if posts_usable == 0:
        raise CollectorError(
            f"Run {store.run_id!r} has {posts_seen} processed record(s) but none "
            "contain a usable post id: cannot fetch comment trees."
        )

    summary = {
        "run_id": store.run_id,
        "posts_attempted": attempted,
        "posts_completed": completed_new,
        "posts_already_complete": already,
        "posts_skipped_empty": skipped_empty,
        "comments_collected": comments,
        "total_comments": prev_comments + comments,
        "stopped": stopped,
    }
    logger.info(
        "Comment collection %s: run %s, %d new comments (%d total)",
        "stopped by user" if stopped else "finished",
        store.run_id, comments, prev_comments + comments,
    )
    return summary


def _save_comment_progress(checkpoint_dir: str, run_id: str, completed: set[str],
                           posts_done: int, skipped_empty: int, comments: int) -> None:
    save_comment_checkpoint(checkpoint_dir, {
        "completed_post_ids": sorted(completed),
        "posts_completed": posts_done,
        "posts_skipped_empty": skipped_empty,
        "comments_collected": comments,
    }, run_id)


def describe_comment_status(config: Mapping[str, Any]) -> dict[str, Any] | None:
    """Comment-phase checkpoint + stored counts, or None when never started."""
    output = config["output"]
    try:
        doc = load_comment_checkpoint(output["checkpoint_dir"])
    except CheckpointError as exc:
        raise CollectorError(str(exc)) from exc
    if doc is None:
        return None
    state = doc["comments"]
    counts: dict[str, Any] = {"raw_comments": None, "processed_comments": None}
    try:
        store = RunStore.existing(output["raw_dir"], output["processed_dir"], doc["run_id"])
        counts = {"raw_comments": store.count_raw_comments(),
                  "processed_comments": store.count_processed_comments()}
    except (StorageError, OSError):
        pass
    return {"saved_at": doc.get("saved_at"), "run_id": doc.get("run_id"),
            **dict(state), **counts}


def describe_status(config: Mapping[str, Any]) -> dict[str, Any]:
    """Gather checkpoint + stored-run info for the status command."""
    output = config["output"]
    try:
        doc = load_checkpoint_doc(output["checkpoint_dir"])
    except CheckpointError as exc:
        raise CollectorError(str(exc)) from exc
    checkpoint = None
    if doc is not None:
        state = doc["paginator"]
        checkpoint = {
            "saved_at": doc.get("saved_at"),
            "run_id": doc.get("run_id"),
            "total_fetched": state.get("total_fetched"),
            "cursor": state.get("cursor"),
            "subreddit": (state.get("filters") or {}).get("subreddit"),
        }

    runs: list[dict[str, Any]] = []
    try:
        comment_doc = load_comment_checkpoint(output["checkpoint_dir"])
    except CheckpointError as exc:
        raise CollectorError(str(exc)) from exc
    raw_root = Path(output["raw_dir"])
    if raw_root.is_dir():
        for run_dir in sorted(p for p in raw_root.iterdir() if p.is_dir()):
            try:
                store = RunStore.existing(output["raw_dir"], output["processed_dir"], run_dir.name)
                entry = {
                    "run_id": run_dir.name,
                    "raw_posts": store.count_raw(),
                    "processed_posts": store.count_processed(),
                    "raw_comments": store.count_raw_comments(),
                    "processed_comments": store.count_processed_comments(),
                    "comment": _comment_progress_for_run(store, comment_doc),
                }
                runs.append(entry)
            except (StorageError, OSError):
                runs.append({"run_id": run_dir.name, "raw_posts": None, "processed_posts": None,
                             "raw_comments": None, "processed_comments": None, "comment": None})
    return {"checkpoint": checkpoint, "runs": runs}


def _comment_progress_for_run(store: "RunStore", comment_doc: dict[str, Any] | None) -> dict[str, Any]:
    """Per-run comment progress. Checkpoint is authoritative when it matches the run."""
    if comment_doc is not None and comment_doc.get("run_id") == store.run_id:
        state = comment_doc["comments"]
        done = state.get("posts_completed", 0) + state.get("posts_skipped_empty", 0)
        total = store.count_processed()
        return {
            "status": "complete" if total > 0 and done >= total else "in_progress",
            "posts_completed": state.get("posts_completed", 0),
            "posts_skipped_empty": state.get("posts_skipped_empty", 0),
            "comments_collected": state.get("comments_collected", 0),
            "posts_considered": total,
        }
    if store.count_processed_comments() > 0:
        # Comment data exists but no checkpoint covers this run: report what
        # is on disk and mark progress unknown rather than pretending none.
        post_ids = {r.get("post_id") for r in store.iter_processed_comments()}
        post_ids.discard(None)
        return {
            "status": "unknown",
            "posts_completed": len(post_ids),
            "posts_skipped_empty": 0,
            "comments_collected": store.count_processed_comments(),
            "posts_considered": store.count_processed(),
        }
    return {
        "status": "not_started",
        "posts_completed": 0,
        "posts_skipped_empty": 0,
        "comments_collected": 0,
        "posts_considered": store.count_processed(),
    }


__all__ = ["CollectorError", "collect_comments", "collect_new", "describe_comment_status",
         "describe_status", "resume_collection"]
