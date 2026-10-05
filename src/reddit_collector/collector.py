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
    load_checkpoint_doc,
    save_checkpoint,
)
from .client import ArcticShiftClient, ArcticShiftError
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
        clear_checkpoint(checkpoint_dir)
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
    raw_root = Path(output["raw_dir"])
    if raw_root.is_dir():
        for run_dir in sorted(p for p in raw_root.iterdir() if p.is_dir()):
            try:
                store = RunStore.existing(output["raw_dir"], output["processed_dir"], run_dir.name)
                runs.append({
                    "run_id": run_dir.name,
                    "raw_posts": store.count_raw(),
                    "processed_posts": store.count_processed(),
                })
            except (StorageError, OSError):
                runs.append({"run_id": run_dir.name, "raw_posts": None, "processed_posts": None})
    return {"checkpoint": checkpoint, "runs": runs}


__all__ = ["CollectorError", "collect_new", "resume_collection", "describe_status"]
