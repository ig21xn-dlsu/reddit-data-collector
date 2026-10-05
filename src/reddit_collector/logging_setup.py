"""Central logging setup: console + file, configured from config.yaml."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(level: str = "INFO", log_file: str | Path | None = None) -> logging.Logger:
    """Configure the root logger once and return the app logger.

    Safe to call multiple times: handlers are only added once.
    """
    numeric = getattr(logging, str(level).upper(), None)
    if not isinstance(numeric, int):
        raise ValueError(f"Invalid log level: {level!r}")

    root = logging.getLogger()
    root.setLevel(numeric)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not root.handlers:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(numeric)
        console.setFormatter(formatter)
        root.addHandler(console)

        if log_file:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setLevel(numeric)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)

    return logging.getLogger("reddit_collector")
