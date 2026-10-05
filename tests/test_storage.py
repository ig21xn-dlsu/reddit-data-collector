"""Tests for two-layer storage. Writes go to temp dirs; no real API calls."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from reddit_collector.storage import (
    PROCESSED_FIELDS,
    RunStore,
    StorageError,
    normalize_post,
)


def _raw(pid="abc123", **overrides):
    post = {
        "id": pid,
        "subreddit": "python",
        "title": "Hello world",
        "selftext": "body text",
        "author": "someuser",
        "author_fullname": "t2_xyz",
        "score": 42,
        "num_comments": 7,
        "created_utc": 1577836800,  # 2020-01-01T00:00:00Z
        "url": "https://example.com/x",
        "retrieved_on": 1577750500,
    }
    post.update(overrides)
    return post


class TestNormalize(unittest.TestCase):
    def test_schema_and_values(self):
        record = normalize_post(_raw())
        self.assertEqual(tuple(record.keys()), PROCESSED_FIELDS)
        self.assertEqual(record["id"], "abc123")
        self.assertEqual(record["subreddit"], "python")
        self.assertEqual(record["title"], "Hello world")
        self.assertEqual(record["selftext"], "body text")
        self.assertEqual(record["author"], "someuser")
        self.assertEqual(record["score"], 42)
        self.assertEqual(record["num_comments"], 7)
        self.assertEqual(record["created_utc"], 1577836800)
        self.assertEqual(record["created_iso"], "2020-01-01T00:00:00+00:00")
        self.assertEqual(record["url"], "https://example.com/x")
        self.assertEqual(
            record["permalink"], "https://www.reddit.com/r/python/comments/abc123/"
        )

    def test_id_preserved_verbatim(self):
        record = normalize_post(_raw(pid="t3_ABC_x9"))
        self.assertEqual(record["id"], "t3_ABC_x9")
        self.assertIn("t3_ABC_x9", record["permalink"])

    def test_missing_fields_become_null(self):
        record = normalize_post({"id": "x"})
        self.assertEqual(record["id"], "x")
        for key in ("subreddit", "title", "selftext", "author", "score",
                    "num_comments", "created_utc", "created_iso", "url", "permalink"):
            self.assertIsNone(record[key])

    def test_does_not_mutate_input(self):
        raw = _raw()
        snapshot = dict(raw)
        normalize_post(raw)
        self.assertEqual(raw, snapshot)


class TestRunStore(unittest.TestCase):
    def _store(self, tmp, **kwargs):
        return RunStore(f"{tmp}/raw", f"{tmp}/processed", **kwargs)

    def test_raw_stored_verbatim_and_appendable(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp, run_id="run1")
            page1 = [_raw("a"), _raw("b")]
            page2 = [_raw("c")]
            self.assertEqual(store.append_raw(page1), 2)
            self.assertEqual(store.append_raw(page2), 1)

            lines = (store.raw_file).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual([json.loads(line) for line in lines], page1 + page2)
            self.assertEqual(store.count_raw(), 3)

    def test_processed_written_per_page(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp, run_id="run1")
            store.append_page([_raw("a"), _raw("b")])
            store.append_page([_raw("c")])
            records = list(store.iter_processed())
            self.assertEqual([r["id"] for r in records], ["a", "b", "c"])
            self.assertTrue(all(tuple(r.keys()) == PROCESSED_FIELDS for r in records))
            self.assertEqual(store.count_processed(), 3)

    def test_runs_never_overwrite(self):
        with TemporaryDirectory() as tmp:
            first = self._store(tmp, run_id="run1", subreddit="python")
            first.append_page([_raw("a")])
            second = self._store(tmp, run_id="run1", subreddit="python")
            self.assertNotEqual(second.run_id, first.run_id)
            second.append_page([_raw("b")])
            self.assertEqual([r["id"] for r in first.iter_processed()], ["a"])
            self.assertEqual([r["id"] for r in second.iter_processed()], ["b"])

    def test_resume_reopens_same_files(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp, run_id="run1")
            store.append_page([_raw("a")])
            resumed = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "run1")
            self.assertEqual(resumed.run_id, "run1")
            resumed.append_page([_raw("b")])
            self.assertEqual([r["id"] for r in resumed.iter_processed()], ["a", "b"])
            self.assertEqual([r["id"] for r in store.iter_processed()], ["a", "b"])

    def test_resume_missing_run_raises(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(StorageError):
                RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "nope")

    def test_manifest_records_params(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp, run_id="run1")
            path = store.write_manifest({"subreddit": "python", "sort": "asc"})
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["run_id"], "run1")
            self.assertEqual(doc["params"]["subreddit"], "python")
            self.assertIn("started_at", doc)

    def test_streaming_read_skips_blanks(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp, run_id="run1")
            store.append_raw([_raw("a")])
            with open(store.raw_file, "a", encoding="utf-8") as fh:
                fh.write("\n   \n")
            store.append_raw([_raw("b")])
            self.assertEqual([r["id"] for r in store.iter_raw()], ["a", "b"])

    def test_corrupt_line_raises(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp, run_id="run1")
            store.append_raw([_raw("a")])
            with open(store.raw_file, "a", encoding="utf-8") as fh:
                fh.write("{broken\n")
            with self.assertRaises(StorageError):
                list(store.iter_raw())

    def test_empty_store_reads_zero(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp, run_id="run1")
            self.assertEqual(store.count_raw(), 0)
            self.assertEqual(store.count_processed(), 0)
            self.assertEqual(list(store.iter_processed()), [])

    def test_from_config_uses_output_dirs(self):
        with TemporaryDirectory() as tmp:
            config = {
                "subreddit": "python",
                "output": {"raw_dir": f"{tmp}/raw", "processed_dir": f"{tmp}/processed"},
            }
            store = RunStore.from_config(config, run_id="run1")
            store.append_page([_raw("a")])
            self.assertTrue(Path(tmp, "raw", "run1", "posts.jsonl").is_file())
            self.assertTrue(Path(tmp, "processed", "run1", "posts.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
