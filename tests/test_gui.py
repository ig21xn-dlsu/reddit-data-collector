"""Tests for GUI helpers and the cooperative stop hook. No tkinter needed."""

from __future__ import annotations

import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from reddit_collector.gui import (
    StatsTracker,
    build_config_dict,
    format_summary,
    prefill_inputs,
    write_gui_config,
)


def _record(name, level, msg, args=()):
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)


class TestBuildConfigDict(unittest.TestCase):
    def test_maps_fields_and_advanced_row(self):
        config = build_config_dict({
            "subreddit": "Philippines", "keywords": "typhoon",
            "title_kw": "election", "selftext_kw": "",
            "after": "2024-01-01", "before": "", "max_posts": "10,000",
            "raw_dir": "data/raw", "processed_dir": "data/processed",
            "checkpoint_dir": "data/checkpoints", "log_file": "",
        })
        self.assertEqual(config["subreddit"], "Philippines")
        self.assertEqual(config["query"], "typhoon")
        self.assertEqual(config["title"], "election")
        self.assertIsNone(config["selftext"])
        self.assertIsNone(config["before"])
        self.assertEqual(config["max_posts"], 10000)
        self.assertEqual(config["limit"], 100)
        self.assertEqual(config["sort"], "asc")
        self.assertEqual(config["output"]["raw_dir"], "data/raw")
        self.assertEqual(config["logging"]["file"], "logs/gui.log")

    def test_bad_target_rejected_with_clear_message(self):
        with self.assertRaises(ValueError) as ctx:
            build_config_dict({"max_posts": "ten"})
        self.assertIn("Target posts", str(ctx.exception))
        with self.assertRaises(ValueError):
            build_config_dict({"max_posts": "0"})

    def test_empty_target_means_unlimited(self):
        self.assertIsNone(build_config_dict({"max_posts": ""})["max_posts"])

    def test_written_config_passes_project_validation(self):
        with TemporaryDirectory() as tmp:
            path = write_gui_config({"subreddit": "python", "max_posts": "10"},
                                    f"{tmp}/last-run.yaml")
            self.assertTrue(Path(path).is_file())

    def test_invalid_inputs_fail_before_any_collection(self):
        from reddit_collector.config import ConfigError

        with TemporaryDirectory() as tmp:
            with self.assertRaises((ValueError, ConfigError)):
                write_gui_config({"subreddit": "", "max_posts": "10"}, f"{tmp}/bad.yaml")

    def test_prefill_roundtrip(self):
        config = build_config_dict({"subreddit": "python", "keywords": "x",
                                    "max_posts": "25", "after": "2024-01-01"})
        fields = prefill_inputs(config)
        self.assertEqual(fields["subreddit"], "python")
        self.assertEqual(fields["keywords"], "x")
        self.assertEqual(fields["max_posts"], "25")
        self.assertEqual(fields["after"], "2024-01-01")
        self.assertEqual(fields["title_kw"], "")


class TestStatsTracker(unittest.TestCase):
    def test_counts_pages_retries_and_errors(self):
        stats = StatsTracker(target_posts=10)
        stats.update(_record("reddit_collector.paginator", logging.INFO,
                             "Page %d: %d new posts (%d/%d total)", (1, 5, 5, 10)))
        stats.update(_record("reddit_collector.rate_limit", logging.WARNING,
                             "Waiting %.2fs before retry %d/%d%s", (2.0, 1, 5, " (x)")))
        stats.update(_record("reddit_collector.client", logging.ERROR, "boom"))
        snap = stats.snapshot()
        self.assertEqual((snap["pages"], snap["posts"]), (1, 5))
        self.assertEqual(snap["retry_waits"], 1)
        self.assertEqual(snap["errors"], 1)
        self.assertTrue(snap["waiting_on_rate_limit"])

    def test_next_page_clears_rate_limit_flag(self):
        stats = StatsTracker()
        stats.update(_record("r.rate_limit", logging.WARNING, "Waiting 1.00s before retry 1/5"))
        self.assertTrue(stats.snapshot()["waiting_on_rate_limit"])
        stats.update(_record("reddit_collector.paginator", logging.INFO,
                             "Page 2: 5 new posts (10 total)"))
        snap = stats.snapshot()
        self.assertFalse(snap["waiting_on_rate_limit"])
        self.assertEqual((snap["pages"], snap["posts"]), (2, 10))

    def test_format_summary(self):
        text = format_summary(
            {"run_id": "r1", "pages": 2, "posts_this_run": 10, "total_posts": 10,
             "stopped": False},
            65, {"pages": 2, "retry_waits": 1, "errors": 0}, "data/processed")
        self.assertIn("10", text)
        self.assertIn("1m 05s", text)
        self.assertIn("data/processed/r1/posts.jsonl", text)
        stopped = format_summary(
            {"run_id": "r1", "pages": 1, "posts_this_run": 5, "total_posts": 5,
             "stopped": True},
            5, {"pages": 1, "retry_waits": 0, "errors": 0}, "data/processed")
        self.assertIn("resume", stopped)


if __name__ == "__main__":
    unittest.main()
