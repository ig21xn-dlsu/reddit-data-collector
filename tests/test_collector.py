"""Tests for the collection loop. HTTP is mocked; files go to temp dirs."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from reddit_collector.checkpoint import load_checkpoint
from reddit_collector.collector import (
    CollectorError,
    collect_new,
    describe_status,
    resume_collection,
)
from reddit_collector.config import load_config


def _post(pid, ts):
    return {"id": pid, "created_utc": ts, "title": f"t-{pid}",
            "subreddit": "python", "author": "u", "score": 1,
            "num_comments": 0, "url": "http://x"}


def _client(pages):
    client = MagicMock()
    seq = [{"data": list(p)} for p in pages]
    client.search_posts.side_effect = lambda **kw: seq.pop(0) if seq else {"data": []}
    return client


def _write_config(tmp):
    cfg = Path(tmp) / "config.yaml"
    cfg.write_text(
        "subreddit: python\n"
        "limit: 2\n"
        "output:\n"
        f"  raw_dir: {tmp}/raw\n"
        f"  processed_dir: {tmp}/processed\n"
        f"  checkpoint_dir: {tmp}/checkpoints\n"
        "logging:\n"
        f"  file: {tmp}/run.log\n",
        encoding="utf-8",
    )
    return load_config(str(cfg))


class TestCollect(unittest.TestCase):
    def test_collect_stores_raw_processed_and_checkpoint(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            client = _client([[ _post("a", 1), _post("b", 2)], [_post("c", 3)]])
            summary = collect_new(config, client=client)

            self.assertEqual(summary["posts_this_run"], 3)
            self.assertEqual(summary["total_posts"], 3)
            run_id = summary["run_id"]
            raw = list((Path(tmp) / "raw" / run_id / "posts.jsonl").read_text().splitlines())
            self.assertEqual(len(raw), 3)
            store_check = load_checkpoint(f"{tmp}/checkpoints")
            assert store_check is not None
            self.assertEqual(store_check["total_fetched"], 3)
            self.assertTrue((Path(tmp) / "raw" / run_id / "manifest.json").is_file())

    def test_collect_refuses_when_checkpoint_exists(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            collect_new(config, client=_client([[ _post("a", 1)]]))
            with self.assertRaises(CollectorError) as ctx:
                collect_new(config, client=_client([]))
            self.assertIn("resume", str(ctx.exception))

    def test_collect_with_corrupt_checkpoint_raises_collector_error(self):
        from reddit_collector.checkpoint import CHECKPOINT_FILENAME

        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            Path(f"{tmp}/checkpoints").mkdir(parents=True, exist_ok=True)
            Path(f"{tmp}/checkpoints/{CHECKPOINT_FILENAME}").write_text("{broken", encoding="utf-8")
            with self.assertRaises(CollectorError) as ctx:
                collect_new(config, client=_client([]))
            self.assertIn("--fresh", str(ctx.exception))

    def test_fresh_starts_over(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            first = collect_new(config, client=_client([[ _post("a", 1)]]))
            second = collect_new(config, fresh=True, client=_client([[ _post("b", 2)]]))
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertEqual(second["posts_this_run"], 1)

    def test_resume_continues_same_run_without_dupes(self):
        with TemporaryDirectory() as tmp:
            config = dict(_write_config(tmp))
            config["max_posts"] = 2
            first = collect_new(config, client=_client([[ _post("a", 1), _post("b", 2)],
                                                         [_post("c", 3)]]))
            self.assertEqual(first["posts_this_run"], 2)

            config["max_posts"] = None
            second = resume_collection(config, client=_client([[ _post("c", 3), _post("d", 4)], []]))
            self.assertEqual(second["run_id"], first["run_id"])
            self.assertEqual(second["posts_this_run"], 2)  # c, d only
            self.assertEqual(second["total_posts"], 4)

    def test_resume_requires_checkpoint(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(CollectorError):
                resume_collection(_write_config(tmp), client=_client([]))

    def test_should_stop_pauses_after_finished_page(self):
        from reddit_collector.storage import RunStore

        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            summary = collect_new(
                config,
                client=_client([[ _post("a", 1), _post("b", 2)], [_post("c", 3), _post("d", 4)]]),
                should_stop=lambda: True,  # stop after the first finished page
            )
            self.assertTrue(summary["stopped"])
            self.assertEqual(summary["posts_this_run"], 2)
            # Page 1 fully stored AND checkpointed: resume replays nothing.
            run_id = summary["run_id"]
            stored = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", run_id)
            self.assertEqual([r["id"] for r in stored.iter_processed()], ["a", "b"])

            second = resume_collection(
                config, client=_client([[ _post("c", 3), _post("d", 4)], []])
            )
            self.assertEqual(second["run_id"], run_id)
            self.assertFalse(second["stopped"])  # resumed run completes normally
            self.assertEqual([r["id"] for r in stored.iter_processed()],
                             ["a", "b", "c", "d"])

    def test_crash_between_append_and_checkpoint_no_dupes(self):
        from reddit_collector.storage import RunStore

        with TemporaryDirectory() as tmp:
            config = dict(_write_config(tmp))
            config["max_posts"] = 2
            first = collect_new(
                config,
                client=_client([[ _post("a", 1), _post("b", 2)], [_post("c", 3), _post("d", 4)]]),
            )
            self.assertEqual(first["posts_this_run"], 2)

            # Simulate a crash: page [c] reached the files but no checkpoint.
            crashed = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", first["run_id"])
            crashed.append_page([_post("c", 3)])

            config["max_posts"] = None
            second = resume_collection(
                config, client=_client([[ _post("c", 3), _post("d", 4)], []])
            )
            self.assertEqual(second["run_id"], first["run_id"])
            self.assertEqual(second["posts_this_run"], 1)  # only d
            self.assertEqual(second["total_posts"], 4)
            stored = list(crashed.iter_processed())
            self.assertEqual([r["id"] for r in stored], ["a", "b", "c", "d"])

    def test_status_reports_checkpoint_and_runs(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            info = describe_status(config)
            self.assertIsNone(info["checkpoint"])
            self.assertEqual(info["runs"], [])

            summary = collect_new(config, client=_client([[ _post("a", 1)]]))
            info = describe_status(config)
            assert info["checkpoint"] is not None
            self.assertEqual(info["checkpoint"]["run_id"], summary["run_id"])
            self.assertEqual(info["checkpoint"]["total_fetched"], 1)
            self.assertEqual(len(info["runs"]), 1)
            self.assertEqual(info["runs"][0]["raw_posts"], 1)
            self.assertEqual(info["runs"][0]["processed_posts"], 1)


if __name__ == "__main__":
    unittest.main()
