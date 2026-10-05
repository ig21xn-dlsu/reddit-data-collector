"""Tests for GUI helpers and the cooperative stop hook. No tkinter needed."""

from __future__ import annotations

import logging
import unittest
import unittest.mock
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

    def test_format_summary_with_comments(self):
        text = format_summary(
            {"run_id": "r1", "pages": 1, "posts_this_run": 2, "total_posts": 2,
             "stopped": False},
            30, {"pages": 1, "retry_waits": 0, "errors": 0}, "data/processed",
            comments={"run_id": "r1", "posts_completed": 2, "total_comments": 7,
                      "stopped": False})
        self.assertIn("7", text)
        self.assertIn("Comments:", text)

    def test_stats_track_comment_phase(self):
        stats = StatsTracker()
        stats.update(_record("reddit_collector.collector", logging.INFO,
                             "Post %s: %d comments (%d posts done)", ("p1", 3, 1)))
        stats.update(_record("reddit_collector.checkpoint", logging.INFO,
                             "Comment checkpoint saved: %d posts done, %d comments (%s)",
                             (1, 3, "path")))
        snap = stats.snapshot()
        self.assertTrue(snap["in_comment_phase"])
        self.assertEqual(snap["comment_posts_done"], 1)
        self.assertEqual(snap["comments_collected"], 3)


def _gui_post(pid, n_comments=1):
    return {"id": pid, "created_utc": 1700000000, "subreddit": "python",
            "title": "t", "author": "u", "score": 1,
            "num_comments": n_comments, "url": "http://x"}


def _gui_tree(*pairs):
    return {"data": [{"kind": "t1", "data": {
        "id": cid, "parent_id": f"t3_{pid}", "link_id": f"t3_{pid}", "author": "u",
        "body": "b", "score": 1, "created_utc": 1700000000,
        "subreddit": "python", "permalink": "/", "replies": ""}}
        for cid, pid in pairs]}


class TestRunCollectionFlow(unittest.TestCase):
    def _config(self, tmp):
        from reddit_collector.config import load_config

        cfg = Path(tmp) / "config.yaml"
        cfg.write_text(
            "subreddit: python\nlimit: 10\nmax_posts: 10\noutput:\n"
            f"  raw_dir: {tmp}/raw\n  processed_dir: {tmp}/processed\n"
            f"  checkpoint_dir: {tmp}/checkpoints\nlogging:\n  file: {tmp}/run.log\n",
            encoding="utf-8")
        return load_config(str(cfg))

    def test_flow_runs_posts_then_comments(self):
        from reddit_collector.gui import run_collection_flow
        from reddit_collector.storage import RunStore

        with TemporaryDirectory() as tmp:
            config = self._config(tmp)
            post_client = MagicMock()
            post_client.search_posts.side_effect = [
                {"data": [_gui_post("p1", 1), _gui_post("p2", 0)]}, {"data": []}]
            # Drive the flow with injected clients via collector functions.
            import reddit_collector.gui as gui_module
            from reddit_collector import collector as collector_module

            real_collect_new = collector_module.collect_new
            real_collect_comments = collector_module.collect_comments

            def fake_collect_new(cfg, fresh=False, client=None, should_stop=None):
                return real_collect_new(cfg, fresh=fresh, client=post_client,
                                        should_stop=should_stop)

            tree_client = MagicMock()
            tree_client.get_comment_tree.side_effect = [
                {"data": [{"kind": "t1", "data": {
                    "id": "c1", "parent_id": "t3_p1", "link_id": "t3_p1",
                    "author": "u", "body": "b", "score": 1,
                    "created_utc": 1700000000, "subreddit": "python",
                    "permalink": "/", "replies": ""}}]}]

            def fake_collect_comments(cfg, run_id=None, max_comment_posts=None,
                                      skip_empty_posts=None, client=None,
                                      should_stop=None):
                return real_collect_comments(cfg, run_id=run_id, client=tree_client,
                                             should_stop=should_stop)

            with unittest.mock.patch.object(gui_module, "collect_new",
                                            side_effect=fake_collect_new), \
                 unittest.mock.patch.object(gui_module, "collect_comments",
                                            side_effect=fake_collect_comments):
                result = gui_module.run_collection_flow(config, fresh=True,
                                                        with_comments=True)
            self.assertEqual(result["post"]["posts_this_run"], 2)
            self.assertEqual(result["comments"]["comments_collected"], 1)
            self.assertEqual(result["comments"]["posts_skipped_empty"], 1)
            store = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed",
                                      result["post"]["run_id"])
            self.assertTrue((store.raw_comments_file).is_file())
            self.assertTrue((store.processed_comments_file).is_file())
            self.assertEqual(store.count_processed_comments(), 1)

    def test_flow_without_comments_skips_phase_two(self):
        from reddit_collector import gui as gui_module

        with TemporaryDirectory() as tmp:
            config = self._config(tmp)
            post_client = MagicMock()
            post_client.search_posts.side_effect = [
                {"data": [_gui_post("p1", 1)]}, {"data": []}]

            from reddit_collector import collector as collector_module
            real_collect_new = collector_module.collect_new

            def fake_collect_new(cfg, fresh=False, client=None, should_stop=None):
                return real_collect_new(cfg, fresh=fresh, client=post_client,
                                        should_stop=should_stop)

            with unittest.mock.patch.object(gui_module, "collect_new",
                                            side_effect=fake_collect_new), \
                 unittest.mock.patch.object(gui_module, "collect_comments") as comments_fn:
                result = gui_module.run_collection_flow(config, fresh=True,
                                                        with_comments=False)
            comments_fn.assert_not_called()
            self.assertIsNone(result["comments"])
            self.assertEqual(result["post"]["posts_this_run"], 1)

    def test_flow_stopped_during_posts_skips_comments(self):
        from reddit_collector import gui as gui_module

        with TemporaryDirectory() as tmp:
            config = self._config(tmp)
            post_client = MagicMock()
            post_client.search_posts.side_effect = [
                {"data": [_gui_post("p1", 1), _gui_post("p2", 1)]},
                {"data": [_gui_post("p3", 1)]}]

            from reddit_collector import collector as collector_module
            real_collect_new = collector_module.collect_new

            def fake_collect_new(cfg, fresh=False, client=None, should_stop=None):
                return real_collect_new(cfg, fresh=fresh, client=post_client,
                                        should_stop=should_stop)

            with unittest.mock.patch.object(gui_module, "collect_new",
                                            side_effect=fake_collect_new), \
                 unittest.mock.patch.object(gui_module, "collect_comments") as comments_fn:
                result = gui_module.run_collection_flow(
                    config, fresh=True, with_comments=True,
                    should_stop=lambda: True)
            comments_fn.assert_not_called()
            self.assertTrue(result["post"]["stopped"])


if __name__ == "__main__":
    unittest.main()
