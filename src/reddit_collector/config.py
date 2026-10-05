"""Load and validate YAML configuration. No search parameters are hardcoded."""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


DEFAULTS: dict[str, Any] = {
    "api": {
        "base_url": "https://arctic-shift.photon-reddit.com",
    },
    "title": None,
    "query": None,
    "selftext": None,
    "after": None,
    "before": None,
    "limit": 25,
    "sort": "asc",
    "max_posts": None,
    "output": {
        "raw_dir": "data/raw",
        "processed_dir": "data/processed",
        "checkpoint_dir": "data/checkpoints",
    },
    "collection": {
        "throttle_qps": 1.0,
        "max_retries": 5,
        "timeout_secs": 30,
        "backoff_base_secs": 1.0,
        "backoff_max_secs": 60.0,
    },
    "comments": {
        "tree_limit": 9999,
        "max_comment_posts": None,
        "skip_empty_posts": True,
    },
    "logging": {
        "level": "INFO",
        "file": "logs/collector.log",
    },
}

VALID_SORT = {"asc", "desc"}
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _expand_env(value: Any) -> Any:
    """Expand ${VAR} / $VAR in strings using os.environ."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge override onto base (nested dicts only)."""
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def load_config(path: str | Path) -> dict[str, Any]:
    """Load YAML config, apply defaults + env expansion, and validate.

    Raises ConfigError with a clear message on any problem.
    """
    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise ConfigError(f"Config file not found: {cfg_path} (copy config.example.yaml to get started)")

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {cfg_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {cfg_path}: {exc}") from exc

    if raw is None:
        raise ConfigError(f"Config file is empty: {cfg_path}")
    if not isinstance(raw, dict):
        raise ConfigError(f"Config root must be a mapping, got {type(raw).__name__}")

    config = _merge(DEFAULTS, _expand_env(raw))
    for key in ("after", "before"):
        config[key] = _coerce_date_value(config.get(key))
    _validate(config, cfg_path)
    return config


def _coerce_date_value(value: Any) -> Any:
    """Coerce unquoted YAML dates to the ISO strings the API expects.

    YAML auto-parses `after: 2019-12-30` into a datetime.date; without this,
    users would be forced to quote every date. datetime is checked first
    since it subclasses date.
    """
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


def _validate(config: dict[str, Any], cfg_path: Path) -> None:
    errors: list[str] = []

    api = config.get("api")
    if not isinstance(api, dict):
        errors.append("'api' must be a mapping with base_url")
    else:
        base_url = api.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            errors.append("'api.base_url' must be a non-empty string")
        elif not base_url.lower().startswith(("http://", "https://")):
            errors.append(f"'api.base_url' must start with http(s)://, got {base_url!r}")

    subreddit = config.get("subreddit")
    if not isinstance(subreddit, str) or not subreddit.strip():
        errors.append("'subreddit' is required and must be a non-empty string")

    limit = config.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        errors.append(f"'limit' must be an integer 1-100, got {limit!r}")

    sort = config.get("sort")
    if sort not in VALID_SORT:
        errors.append(f"'sort' must be one of {sorted(VALID_SORT)}, got {sort!r}")

    max_posts = config.get("max_posts")
    if max_posts is not None and (
        not isinstance(max_posts, int) or isinstance(max_posts, bool) or max_posts < 1
    ):
        errors.append(f"'max_posts' must be a positive integer or null, got {max_posts!r}")

    for key in ("title", "query", "selftext"):
        val = config.get(key)
        if val is None:
            continue
        if not isinstance(val, str):
            errors.append(f"'{key}' must be a string or null, got {type(val).__name__}")
        elif not val.strip():
            errors.append(f"'{key}' must be a non-empty string or null")

    for key in ("after", "before"):
        val = config.get(key)
        if val is None:
            continue
        if isinstance(val, bool):
            errors.append(f"'{key}' must be a date string, epoch number or null, got {val!r}")
        elif isinstance(val, str):
            if not val.strip():
                errors.append(f"'{key}' must be a non-empty string or null")
        elif not isinstance(val, (int, float)):
            errors.append(
                f"'{key}' must be a date string, epoch number or null, got {type(val).__name__}"
            )

    output = config.get("output")
    if not isinstance(output, dict):
        errors.append("'output' must be a mapping with raw_dir/processed_dir/checkpoint_dir")
    else:
        for key in ("raw_dir", "processed_dir", "checkpoint_dir"):
            if not isinstance(output.get(key), str) or not output.get(key).strip():
                errors.append(f"'output.{key}' must be a non-empty string")

    collection = config.get("collection")
    if not isinstance(collection, dict):
        errors.append("'collection' must be a mapping")
    else:
        qps = collection.get("throttle_qps")
        if not isinstance(qps, (int, float)) or isinstance(qps, bool) or qps <= 0:
            errors.append(f"'collection.throttle_qps' must be a positive number, got {qps!r}")
        retries = collection.get("max_retries")
        if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
            errors.append(f"'collection.max_retries' must be an integer >= 0, got {retries!r}")
        timeout = collection.get("timeout_secs")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            errors.append(f"'collection.timeout_secs' must be a positive number, got {timeout!r}")
        for key in ("backoff_base_secs", "backoff_max_secs"):
            val = collection.get(key)
            if not isinstance(val, (int, float)) or isinstance(val, bool) or val <= 0:
                errors.append(f"'collection.{key}' must be a positive number, got {val!r}")

    comments = config.get("comments")
    if not isinstance(comments, dict):
        errors.append("'comments' must be a mapping")
    else:
        tree_limit = comments.get("tree_limit")
        if not isinstance(tree_limit, int) or isinstance(tree_limit, bool) or not 1 <= tree_limit <= 25000:
            errors.append(f"'comments.tree_limit' must be an integer 1-25000, got {tree_limit!r}")
        max_cp = comments.get("max_comment_posts")
        if max_cp is not None and (
            not isinstance(max_cp, int) or isinstance(max_cp, bool) or max_cp < 1
        ):
            errors.append(f"'comments.max_comment_posts' must be a positive integer or null, got {max_cp!r}")
        skip_empty = comments.get("skip_empty_posts")
        if not isinstance(skip_empty, bool):
            errors.append(f"'comments.skip_empty_posts' must be true or false, got {skip_empty!r}")

    logging_cfg = config.get("logging")
    if not isinstance(logging_cfg, dict):
        errors.append("'logging' must be a mapping with level/file")
    else:
        level = str(logging_cfg.get("level", "")).upper()
        if level not in VALID_LOG_LEVELS:
            errors.append(f"'logging.level' must be one of {sorted(VALID_LOG_LEVELS)}, got {logging_cfg.get('level')!r}")
        else:
            logging_cfg["level"] = level  # normalise e.g. "info" -> "INFO"
        if not isinstance(logging_cfg.get("file"), str) or not logging_cfg.get("file").strip():
            errors.append("'logging.file' must be a non-empty string")

    if errors:
        raise ConfigError(f"Invalid config in {cfg_path}:\n- " + "\n- ".join(errors))
