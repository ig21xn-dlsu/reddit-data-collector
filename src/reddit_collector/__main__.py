"""Thin command-line interface: collect, resume, validate, status.

All collection logic lives in collector.py (and client/paginator/storage/
checkpoint modules); this module only parses arguments, sets up logging,
prints safe summaries, and maps outcomes to exit codes:

    0  success
    1  collection runtime failure (API, storage)
    2  usage, configuration, or checkpoint problem (nothing was collected)

Logs and terminal output never include sensitive values: only a fixed set
of non-sensitive fields is ever shown, and anything resembling a credential
(api keys, tokens, secrets, passwords) is redacted.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any, Mapping

from .collector import (
    CollectorError,
    collect_comments,
    collect_new,
    describe_comment_status,
    describe_status,
    list_available_checkpoints,
    resume_collection,
)
from .config import ConfigError, load_config
from .logging_setup import setup_logging
from .client import ArcticShiftError
from .storage import StorageError

_SENSITIVE_KEY = re.compile(r"key|token|secret|password|credential|auth", re.IGNORECASE)


def redact(value: Any, key: str = "") -> Any:
    """Replace credential-looking values with '***REDACTED***' (recursive)."""
    if isinstance(value, Mapping):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if key and _SENSITIVE_KEY.search(key):
        return "***REDACTED***"
    return value


def summarize_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Fixed non-sensitive summary of the effective configuration."""
    collection = config.get("collection", {})
    output = config.get("output", {})
    return {
        "subreddit": config.get("subreddit"),
        "title": config.get("title"),
        "query": config.get("query"),
        "selftext": config.get("selftext"),
        "after": config.get("after"),
        "before": config.get("before"),
        "limit": config.get("limit"),
        "sort": config.get("sort"),
        "max_posts": config.get("max_posts"),
        "throttle_qps": collection.get("throttle_qps") if isinstance(collection, Mapping) else None,
        "chunk_size": output.get("chunk_size") if isinstance(output, Mapping) else None,
        "output": {
            "raw_dir": output.get("raw_dir"),
            "processed_dir": output.get("processed_dir"),
            "checkpoint_dir": output.get("checkpoint_dir"),
            "chunk_size": output.get("chunk_size"),
        } if isinstance(output, Mapping) else None,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reddit-collector",
        description="Collect public Reddit posts for academic research via the Arctic Shift API.",
    )
    p.add_argument("--log-level", default=None, help="Override logging.level (e.g. DEBUG, INFO).")
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    c = sub.add_parser("collect", help="Start a new collection (refuses if this collection has a checkpoint).")
    c.add_argument("-c", "--config", default="config.yaml", help="YAML config file (default: config.yaml).")
    c.add_argument("--fresh", action="store_true",
                   help="Reset only the matching collection checkpoint and start over.")
    c.add_argument("--checkpoint", default=None,
                   help="Explicit checkpoint name (<Subreddit>-<after>-<before>); must match the config.")

    r = sub.add_parser("resume", help="Continue this collection from its checkpoint.")
    r.add_argument("-c", "--config", default="config.yaml", help="YAML config file (default: config.yaml).")
    r.add_argument("--checkpoint", default=None,
                   help="Explicit checkpoint name to resume; must match the config.")

    v = sub.add_parser("validate", help="Validate the config and show a safe summary (collects nothing).")
    v.add_argument("-c", "--config", default="config.yaml", help="YAML config file (default: config.yaml).")

    s = sub.add_parser("status", help="Show checkpoint and stored-run status (collects nothing).")
    s.add_argument("-c", "--config", default="config.yaml", help="YAML config file (default: config.yaml).")

    g = sub.add_parser("gui", help="Open the desktop GUI (needs python3-tk).")
    g.add_argument("-c", "--config", default=None, help="Prefill the form from this YAML file.")

    cc = sub.add_parser("collect-comments",
                        help="Phase 2: fetch comment trees for a previous run's posts.")
    cc.add_argument("-c", "--config", default="config.yaml", help="YAML config file (default: config.yaml).")
    cc.add_argument("--run", default=None, help="Run id (default: the post checkpoint's run).")
    cc.add_argument("--checkpoint", default=None,
                    help="Explicit checkpoint name; must match the config.")
    cc.add_argument("--max-comment-posts", type=int, default=None,
                    help="Cap posts to fetch (default: config comments.max_comment_posts).")
    cc.add_argument("--smoke", action="store_true",
                    help="Smoke test: first 5 posts only.")

    lc = sub.add_parser("checkpoints", help="List saved per-collection checkpoints (collects nothing).")
    lc.add_argument("-c", "--config", default="config.yaml", help="YAML config file (default: config.yaml).")

    mc = sub.add_parser("migrate-checkpoint",
                        help="Copy the legacy shared checkpoint into this config's collection (originals kept).")
    mc.add_argument("-c", "--config", default="config.yaml", help="YAML config file (default: config.yaml).")
    return p


def _load_config_or_exit(path: str):
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return None


def cmd_validate(args) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 2
    print("Configuration OK.")
    for key, value in summarize_config(config).items():
        print(f"  {key}: {value!r}")
    # Dataset validation across all chunks (collective, not single-file).
    try:
        from .storage import RunStore
        from pathlib import Path as _Path
        raw_root = _Path(config["output"]["raw_dir"])
        if raw_root.is_dir():
            for run_dir in sorted(p for p in raw_root.iterdir() if p.is_dir()):
                try:
                    store = RunStore.existing(
                        config["output"]["raw_dir"], config["output"]["processed_dir"],
                        run_dir.name,
                        chunk_size=config["output"].get("chunk_size", 50000)
                        if config["output"].get("chunk_size") is not None else 50000,
                    )
                    report = store.validate_chunks()
                    totals = report["totals"]
                    print(f"  run {run_dir.name}: raw_posts={totals['raw_posts']} "
                          f"processed_posts={totals['processed_posts']} "
                          f"raw_comments={totals['raw_comments']} "
                          f"processed_comments={totals['processed_comments']} "
                          f"chunks_ok={report['ok']}")
                    if not report["ok"]:
                        if report["over_limit"]:
                            print(f"    over-limit chunks: {report['over_limit']}")
                        if not report["posts_agree"] or not report["comments_agree"]:
                            print("    WARNING: raw/processed layer counts disagree")
                except Exception:
                    continue
    except Exception:
        pass
    return 0


def cmd_status(args) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 2
    try:
        info = describe_status(config)
    except CollectorError as exc:
        print(f"Status error: {exc}", file=sys.stderr)
        return 2
    checkpoint = info["checkpoint"]
    print(f"Collection: {info.get('collection')}")
    if checkpoint is None:
        print("Checkpoint: none (no saved progress for this collection)")
    else:
        print(f"Checkpoint: {info['collection']}, run {checkpoint['run_id']}, "
              f"{checkpoint['total_fetched']} posts, cursor={checkpoint['cursor']!r}, "
              f"saved {checkpoint['saved_at']}")
    checkpoints = info.get("checkpoints") or []
    if checkpoints:
        print(f"Saved checkpoints: {len(checkpoints)}")
        for entry in checkpoints:
            print(f"  {format_checkpoint_entry(entry)}")
    else:
        print("Saved checkpoints: none")
    if info.get("legacy_note"):
        print(f"Note: {info['legacy_note']}")
    runs = info["runs"]
    if not runs:
        print("Stored runs: none")
    else:
        print(f"Stored runs: {len(runs)}")
        for run in runs:
            print(f"  {run['run_id']}: raw={run['raw_posts']} processed={run['processed_posts']}")
            comment = run.get("comment")
            if comment is None:
                print("    comments: status unavailable")
            elif comment["status"] == "not_started":
                print("    comments: not started")
            elif comment["status"] == "unknown":
                print(f"    comments: {comment['comments_collected']} stored "
                      f"(checkpoint missing — progress unknown, rerun collect-comments)")
            else:
                print(f"    comments: {comment['status']} — "
                      f"{comment['posts_completed']}/{comment['posts_considered']} posts, "
                      f"{comment['comments_collected']} collected")
    try:
        comments = describe_comment_status(config)
    except CollectorError as exc:
        print(f"Status error: {exc}", file=sys.stderr)
        return 2
    if comments is None:
        print("Comment checkpoint: none")
    else:
        print(f"Comment checkpoint: run {comments['run_id']}, "
              f"{comments['posts_completed']} posts done, "
              f"{comments['comments_collected']} comments, "
              f"stored raw={comments['raw_comments']} "
              f"processed={comments['processed_comments']}")
    return 0


def format_checkpoint_entry(entry: Mapping[str, Any]) -> str:
    """One-line summary for a discovered checkpoint (never raises)."""
    try:
        name = entry.get("name", "?")
        if entry.get("error"):
            return f"{name}: error — {entry['error']}"
        posts = entry.get("posts") or {}
        comments = entry.get("comments") or {}
        if entry.get("legacy"):
            prefix = "legacy (shared checkpoint, migrate explicitly)"
        else:
            prefix = str(name)
        if not entry.get("has_posts") and not entry.get("has_comments"):
            return f"{prefix}: empty"
        status = posts.get("status", "unknown") if isinstance(posts, Mapping) else "unknown"
        total = posts.get("total_fetched", "?") if isinstance(posts, Mapping) else "?"
        saved = entry.get("saved_at") or "unknown time"
        run = entry.get("run_id") or "no run"
        detail = f"{prefix}: {status}, {total} posts, run {run}, saved {saved}"
        if entry.get("has_comments") and isinstance(comments, Mapping):
            detail += f"; comments: {comments.get('comments_collected', '?')} collected"
        return detail
    except Exception as exc:  # never break status listing
        return f"{entry.get('name', '?')}: status unavailable ({exc})"


def cmd_checkpoints(args) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 2
    try:
        entries = list_available_checkpoints(config)
    except CollectorError as exc:
        print(f"Status error: {exc}", file=sys.stderr)
        return 2
    if not entries:
        print("Saved checkpoints: none")
    else:
        print(f"Saved checkpoints: {len(entries)}")
        for entry in entries:
            print(f"  {format_checkpoint_entry(entry)}")
    return 0


def cmd_migrate_checkpoint(args) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 2
    from .checkpoint import CheckpointError, migrate_legacy_checkpoint

    try:
        target = migrate_legacy_checkpoint(config["output"]["checkpoint_dir"], config)
    except CheckpointError as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return 2
    print(f"Migrated legacy checkpoint to {target} (originals kept).")
    return 0


def cmd_collect(args) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 2
    _setup_logging_or_exit(args, config)
    try:
        summary = collect_new(config, fresh=args.fresh, checkpoint=args.checkpoint)
    except CollectorError as exc:
        print(f"Collection error: {exc}", file=sys.stderr)
        return 2
    except (ArcticShiftError, StorageError) as exc:
        print(f"Collection failed: {exc}", file=sys.stderr)
        return 1
    print(f"Done: run {summary['run_id']}: "
          f"{summary['posts_this_run']} new posts ({summary['total_posts']} total).")
    return 0


def cmd_resume(args) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 2
    _setup_logging_or_exit(args, config)
    try:
        summary = resume_collection(config, checkpoint=args.checkpoint)
    except CollectorError as exc:
        print(f"Resume error: {exc}", file=sys.stderr)
        return 2
    except (ArcticShiftError, StorageError) as exc:
        print(f"Collection failed: {exc}", file=sys.stderr)
        return 1
    print(f"Done: run {summary['run_id']}: "
          f"{summary['posts_this_run']} new posts ({summary['total_posts']} total).")
    return 0


def cmd_collect_comments(args) -> int:
    config = _load_config_or_exit(args.config)
    if config is None:
        return 2
    _setup_logging_or_exit(args, config)
    cap = 5 if args.smoke else args.max_comment_posts
    if args.smoke:
        print("Smoke test mode: first 5 posts only.")
    try:
        summary = collect_comments(config, run_id=args.run, max_comment_posts=cap,
                                   checkpoint=args.checkpoint)
    except CollectorError as exc:
        print(f"Comment collection error: {exc}", file=sys.stderr)
        return 2
    except (ArcticShiftError, StorageError) as exc:
        print(f"Comment collection failed: {exc}", file=sys.stderr)
        return 1
    print(f"Done: run {summary['run_id']}: "
          f"{summary['comments_collected']} new comments from "
          f"{summary['posts_completed']} posts ({summary['total_comments']} total).")
    return 0


def _setup_logging_or_exit(args, config) -> None:
    level = args.log_level.upper() if args.log_level else config["logging"]["level"]
    try:
        logger = setup_logging(level=level, log_file=config["logging"]["file"])
    except ValueError as exc:
        print(f"Logging error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    logger.info("Loaded config from %s", args.config)


def cmd_gui(args) -> int:
    from . import gui as gui_module

    return gui_module.main(["--config", args.config] if args.config else [])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "collect":
        return cmd_collect(args)
    if args.command == "resume":
        return cmd_resume(args)
    if args.command == "collect-comments":
        return cmd_collect_comments(args)
    if args.command == "checkpoints":
        return cmd_checkpoints(args)
    if args.command == "migrate-checkpoint":
        return cmd_migrate_checkpoint(args)
    if args.command == "gui":
        return cmd_gui(args)
    raise AssertionError(f"unknown command: {args.command}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
