"""Tests for the JSON checkpoint store and resume wiring. No real HTTP calls."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from reddit_collector.__main__ import main
from reddit_collector.checkpoint import (
    CHECKPOINT_FILENAME,
    CheckpointError,
    checkpoint_path,
    clear_checkpoint,
    load_checkpoint,
    save_checkpoint,
)
from reddit_collector.paginator import PostPaginator


def _post(pid, ts):
    return {"id": pid, "created_utc": ts, "title": f"title {pid}"}


def _client(pages):
    client = MagicMock()
    seq = [{"data": list(p)} for p in pages]
    client.search_posts.side_effect = lambda **kwargs: seq.pop(0) if seq else {"data": []}
    return client


def _paginator(client, **kwargs):
    kwargs.setdefault("subreddit", "python")
    kwargs.setdefault("sort", "asc")
    kwargs.setdefault("page_size", 2)
    return PostPaginator(client, **kwargs)


class TestSaveAndLoad(unittest.TestCase):
    def test_save_creates_valid_json_checkpoint(self):
        with TemporaryDirectory() as tmp:
            paginator = _paginator(_client([[_post("a", 1), _post("b", 2)], []]))
            list(paginator.iter_pages())
            path = save_checkpoint(tmp, paginator.state())

            self.assertEqual(path, Path(tmp) / CHECKPOINT_FILENAME)
            self.assertTrue(path.is_file())
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["version"], 1)
            self.assertIn("saved_at", doc)
            self.assertEqual(doc["paginator"]["total_fetched"], 2)
            self.assertEqual(doc["paginator"]["cursor"], 2)

    def test_save_leaves_no_temp_files(self):
        with TemporaryDirectory() as tmp:
            save_checkpoint(tmp, _paginator(_client([])).state())
            leftovers = [p for p in Path(tmp).iterdir() if p.name != CHECKPOINT_FILENAME]
            self.assertEqual(leftovers, [])

    def test_load_roundtrips_state(self):
        with TemporaryDirectory() as tmp:
            paginator = _paginator(_client([[_post("a", 1)]]))
            list(paginator.iter_pages())
            save_checkpoint(tmp, paginator.state())
            loaded = load_checkpoint(tmp)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded, paginator.state())

    def test_failed_write_keeps_previous_checkpoint(self):
        with TemporaryDirectory() as tmp:
            paginator = _paginator(_client([[_post("a", 1)]]))
            list(paginator.iter_pages())
            save_checkpoint(tmp, paginator.state())
            before = load_checkpoint(tmp)

            with patch("reddit_collector.checkpoint.json.dump", side_effect=OSError("disk full")):
                with self.assertRaises(CheckpointError):
                    save_checkpoint(tmp, paginator.state())

            self.assertEqual(load_checkpoint(tmp), before)
            leftovers = [p for p in Path(tmp).iterdir() if p.name != CHECKPOINT_FILENAME]
            self.assertEqual(leftovers, [])


class TestMissingCheckpoint(unittest.TestCase):
    def test_load_missing_returns_none(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(load_checkpoint(tmp))

    def test_clear_missing_returns_false(self):
        with TemporaryDirectory() as tmp:
            self.assertFalse(clear_checkpoint(tmp))

    def test_clear_existing_returns_true(self):
        with TemporaryDirectory() as tmp:
            save_checkpoint(tmp, _paginator(_client([])).state())
            self.assertTrue(clear_checkpoint(tmp))
            self.assertIsNone(load_checkpoint(tmp))
            self.assertFalse(Path(tmp, CHECKPOINT_FILENAME).exists())


class TestInvalidCheckpoint(unittest.TestCase):
    def _write(self, tmp, content):
        path = Path(tmp) / CHECKPOINT_FILENAME
        path.write_text(content, encoding="utf-8")
        return path

    def test_not_json(self):
        with TemporaryDirectory() as tmp:
            self._write(tmp, "{not json")
            with self.assertRaises(CheckpointError):
                load_checkpoint(tmp)

    def test_wrong_version(self):
        with TemporaryDirectory() as tmp:
            state = _paginator(_client([])).state()
            self._write(tmp, json.dumps({"version": 999, "paginator": state}))
            with self.assertRaises(CheckpointError):
                load_checkpoint(tmp)

    def test_missing_paginator(self):
        with TemporaryDirectory() as tmp:
            self._write(tmp, json.dumps({"version": 1}))
            with self.assertRaises(CheckpointError):
                load_checkpoint(tmp)

    def test_bad_types_rejected(self):
        with TemporaryDirectory() as tmp:
            state = _paginator(_client([])).state()
            state["seen_ids"] = "not-a-list"
            state["total_fetched"] = -1
            self._write(tmp, json.dumps({"version": 1, "paginator": state}))
            with self.assertRaises(CheckpointError):
                load_checkpoint(tmp)

    def test_tampered_filters_rejected_on_resume(self):
        with TemporaryDirectory() as tmp:
            paginator = _paginator(_client([[_post("a", 1)]]))
            list(paginator.iter_pages())
            save_checkpoint(tmp, paginator.state())

            loaded = load_checkpoint(tmp)
            assert loaded is not None
            loaded["filters"]["subreddit"] = "rust"  # tamper after loading
            with self.assertRaises(ValueError):
                _paginator(_client([]), resume=loaded)


class TestResumeCollection(unittest.TestCase):
    def test_resume_continues_without_duplicates(self):
        first = [_post("a", 1), _post("b", 2)]
        rest = [_post("c", 3), _post("d", 4)]
        with TemporaryDirectory() as tmp:
            # Interrupted run: one page, then "crash" (checkpoint saved per page).
            paginator = _paginator(_client([first, rest, []]))
            pages = paginator.iter_pages()
            collected = [p["id"] for p in next(pages)]
            save_checkpoint(tmp, paginator.state())
            del paginator

            # Next run resumes from disk.
            resumed_state = load_checkpoint(tmp)
            paginator2 = _paginator(_client([rest, []]), resume=resumed_state)
            collected += [p["id"] for p in paginator2.iter_posts()]
            save_checkpoint(tmp, paginator2.state())

            self.assertEqual(collected, ["a", "b", "c", "d"])
            final = load_checkpoint(tmp)
            assert final is not None
            self.assertEqual(final["total_fetched"], 4)


class TestCliWiring(unittest.TestCase):
    def _write_config(self, tmp):
        cfg = Path(tmp) / "config.yaml"
        cfg.write_text(
            "subreddit: python\n"
            f"output: {{checkpoint_dir: {tmp}/checkpoints}}\n"
            f"logging: {{level: INFO, file: {tmp}/run.log}}\n",
            encoding="utf-8",
        )
        return str(cfg)

    def test_collect_fresh_clears_checkpoint(self):
        from unittest.mock import patch

        with TemporaryDirectory() as tmp:
            cfg = self._write_config(tmp)
            save_checkpoint(f"{tmp}/checkpoints", _paginator(_client([])).state())
            summary = {"run_id": "r", "pages": 0, "posts_this_run": 0, "total_posts": 0}
            with patch("reddit_collector.__main__.collect_new", return_value=summary) as collect:
                self.assertEqual(main(["collect", "--config", cfg, "--fresh"]), 0)
            collect.assert_called_once()
            self.assertTrue(collect.call_args.kwargs.get("fresh"))

    def test_resume_delegates_and_succeeds(self):
        from unittest.mock import patch

        with TemporaryDirectory() as tmp:
            cfg = self._write_config(tmp)
            paginator = _paginator(_client([[_post("a", 1)]]))
            list(paginator.iter_pages())
            save_checkpoint(f"{tmp}/checkpoints", paginator.state())
            summary = {"run_id": "r", "pages": 1, "posts_this_run": 1, "total_posts": 1}
            with patch("reddit_collector.__main__.resume_collection", return_value=summary):
                self.assertEqual(main(["resume", "--config", cfg]), 0)

    def test_resume_with_corrupt_checkpoint_fails_safe(self):
        with TemporaryDirectory() as tmp:
            cfg = self._write_config(tmp)
            Path(f"{tmp}/checkpoints").mkdir(parents=True, exist_ok=True)
            Path(f"{tmp}/checkpoints/{CHECKPOINT_FILENAME}").write_text("{broken", encoding="utf-8")
            self.assertEqual(main(["resume", "--config", cfg]), 2)
            # Checkpoint left untouched for inspection, not silently wiped.
            self.assertTrue(checkpoint_path(f"{tmp}/checkpoints").exists())


if __name__ == "__main__":
    unittest.main()
