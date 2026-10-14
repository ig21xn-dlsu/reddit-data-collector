"""Tests for chunked JSONL output (default 50,000 records per file)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from reddit_collector.collector import collect_new, collect_comments, resume_collection
from reddit_collector.config import load_config
from reddit_collector.storage import DEFAULT_CHUNK_SIZE, RunStore


def _raw(pid, n_comments=0):
    return {"id": pid, "subreddit": "python", "title": f"t-{pid}", "selftext": "",
            "author": "u", "score": 1, "num_comments": n_comments,
            "created_utc": 1700000000, "url": "http://x"}


def _store(tmp, run_id="run1", chunk_size=DEFAULT_CHUNK_SIZE):
    return RunStore(f"{tmp}/raw", f"{tmp}/processed", run_id=run_id, chunk_size=chunk_size)


def _chunk_counts(tmp, run_id, layer="raw", kind="posts"):
    base = Path(tmp) / layer / run_id / kind
    if not base.is_dir():
        return []
    files = sorted(base.glob(f"{kind}_*.jsonl"))
    return [sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
            for p in files]


def _write_config(tmp, chunk_size=None, extra=""):
    cfg = Path(tmp) / "config.yaml"
    chunk_line = f"  chunk_size: {chunk_size}\n" if chunk_size is not None else ""
    cfg.write_text(
        "subreddit: python\n"
        "limit: 2\n"
        "output:\n"
        f"  raw_dir: {tmp}/raw\n"
        f"  processed_dir: {tmp}/processed\n"
        f"  checkpoint_dir: {tmp}/checkpoints\n"
        + chunk_line +
        "logging:\n"
        f"  file: {tmp}/run.log\n" + extra,
        encoding="utf-8",
    )
    return load_config(str(cfg))


class TestChunkBoundaries(unittest.TestCase):
    def test_fewer_than_limit_single_chunk(self):
        with TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.write_manifest({"subreddit": "python"})
            store.append_page([_raw(f"p{i}") for i in range(3)])
            self.assertEqual(_chunk_counts(tmp, "run1", "raw", "posts"), [3])
            self.assertEqual(_chunk_counts(tmp, "run1", "processed", "posts"), [3])
            self.assertEqual(store.count_raw(), 3)
            self.assertEqual(store.count_processed(), 3)

    def test_exactly_50000_single_full_chunk(self):
        with TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.write_manifest({})
            batch = 5000
            for start in range(0, DEFAULT_CHUNK_SIZE, batch):
                store.append_page([_raw(f"p{start + i}") for i in range(batch)])
            self.assertEqual(_chunk_counts(tmp, "run1", "raw", "posts"), [50000])
            self.assertEqual(_chunk_counts(tmp, "run1", "processed", "posts"), [50000])
            self.assertEqual(store.count_raw(), 50000)
            report = store.validate_chunks()
            self.assertTrue(report["ok"])
            self.assertEqual(report["over_limit"], [])

    def test_50001_rotates_to_second_chunk(self):
        with TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.write_manifest({})
            batch = 5000
            total = DEFAULT_CHUNK_SIZE + 1
            for start in range(0, total, batch):
                n = min(batch, total - start)
                store.append_page([_raw(f"p{start + i}") for i in range(n)])
            self.assertEqual(_chunk_counts(tmp, "run1", "raw", "posts"), [50000, 1])
            self.assertEqual(_chunk_counts(tmp, "run1", "processed", "posts"), [50000, 1])
            self.assertEqual(store.count_raw(), 50001)

    def test_120k_acceptance_three_chunks(self):
        with TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.write_manifest({})
            total = 120000
            batch = 5000
            for start in range(0, total, batch):
                store.append_page([_raw(f"p{start + i}") for i in range(batch)])
            counts = _chunk_counts(tmp, "run1", "raw", "posts")
            self.assertEqual(counts, [50000, 50000, 20000])
            for c in counts:
                self.assertLessEqual(c, DEFAULT_CHUNK_SIZE)
            self.assertEqual(store.count_raw(), 120000)
            self.assertEqual(store.count_processed(), 120000)

    def test_small_chunk_size_multiple_chunks(self):
        with TemporaryDirectory() as tmp:
            store = _store(tmp, chunk_size=5)
            store.write_manifest({})
            store.append_page([_raw("a"), _raw("b"), _raw("c")])
            store.append_page([_raw("d"), _raw("e"), _raw("f"), _raw("g")])
            self.assertEqual(_chunk_counts(tmp, "run1", "raw", "posts"), [5, 2])
            self.assertEqual(list(r["id"] for r in store.iter_raw()),
                             ["a", "b", "c", "d", "e", "f", "g"])

    def test_incremental_no_full_dataset_in_memory(self):
        # Appends are page-sized; iteration streams (check via generator type
        # and per-page appends rather than one giant list).
        import types

        with TemporaryDirectory() as tmp:
            store = _store(tmp, chunk_size=4)
            store.write_manifest({})
            for i in range(0, 10, 2):
                store.append_page([_raw(f"p{i}"), _raw(f"p{i + 1}")])
            self.assertEqual(_chunk_counts(tmp, "run1", "raw", "posts"), [4, 4, 2])
            it = store.iter_processed()
            self.assertTrue(isinstance(it, types.GeneratorType) or hasattr(it, "__next__"))
            self.assertEqual(sum(1 for _ in it), 10)


class TestRawProcessedAndComments(unittest.TestCase):
    def test_raw_and_processed_separate_but_equal(self):
        with TemporaryDirectory() as tmp:
            store = _store(tmp, chunk_size=3)
            store.write_manifest({})
            store.append_page([_raw("a"), _raw("b")])
            store.append_page([_raw("c"), _raw("d")])
            raw_files = sorted((Path(tmp) / "raw" / "run1" / "posts").glob("*.jsonl"))
            proc_files = sorted((Path(tmp) / "processed" / "run1" / "posts").glob("*.jsonl"))
            self.assertEqual(len(raw_files), 2)
            self.assertEqual(len(proc_files), 2)
            # Raw keeps verbatim objects; processed keeps normalized schema.
            first_raw = json.loads(raw_files[0].read_text().splitlines()[0])
            self.assertIn("created_utc", first_raw)
            first_proc = json.loads(proc_files[0].read_text().splitlines()[0])
            self.assertIn("created_iso", first_proc)
            self.assertEqual(store.count_raw(), store.count_processed())

    def test_comments_chunked(self):
        with TemporaryDirectory() as tmp:
            store = _store(tmp, chunk_size=2)
            store.write_manifest({})
            nodes = [{"kind": "t1", "data": {"id": f"c{i}"}} for i in range(5)]
            recs = [{"comment_id": f"c{i}", "post_id": "p1"} for i in range(5)]
            store.append_raw_comments(nodes)
            store.append_processed_comments(recs)
            self.assertEqual(_chunk_counts(tmp, "run1", "raw", "comments"), [2, 2, 1])
            self.assertEqual(_chunk_counts(tmp, "run1", "processed", "comments"), [2, 2, 1])
            self.assertEqual(store.count_raw_comments(), 5)
            self.assertEqual([r["comment_id"] for r in store.iter_processed_comments()],
                             [f"c{i}" for i in range(5)])


class TestManifestAndValidation(unittest.TestCase):
    def test_manifest_lists_chunks_and_counts(self):
        with TemporaryDirectory() as tmp:
            store = _store(tmp, chunk_size=2)
            path = store.write_manifest({"subreddit": "python"})
            store.append_page([_raw("a"), _raw("b"), _raw("c")])
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["chunk_size"], 2)
            self.assertIn("chunks", doc)
            self.assertIn("totals", doc)
            raw_posts = doc["chunks"]["raw_posts"]
            self.assertEqual(sum(e["records"] for e in raw_posts), 3)
            self.assertEqual(doc["totals"]["raw_posts"], 3)
            self.assertTrue(any("posts_00001" in e["file"] for e in raw_posts))

    def test_configurable_chunk_size(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp, chunk_size=7)
            self.assertEqual(config["output"]["chunk_size"], 7)
            store = RunStore.from_config(config, run_id="run1")
            self.assertEqual(store.chunk_size, 7)
            store.write_manifest({})
            store.append_page([_raw(f"p{i}") for i in range(10)])
            self.assertEqual(_chunk_counts(tmp, "run1", "raw", "posts"), [7, 3])

    def test_default_chunk_size_is_50000(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            self.assertEqual(config["output"]["chunk_size"], DEFAULT_CHUNK_SIZE)
            self.assertEqual(DEFAULT_CHUNK_SIZE, 50000)

    def test_validator_checks_all_chunks_collectively(self):
        with TemporaryDirectory() as tmp:
            store = _store(tmp, chunk_size=2)
            store.write_manifest({})
            store.append_page([_raw("a"), _raw("b"), _raw("c")])
            report = store.validate_chunks()
            self.assertTrue(report["ok"])
            self.assertEqual(report["totals"]["raw_posts"], 3)
            # Corrupt any chunk (not just the first): collective reads must fail.
            second = Path(tmp, "raw", "run1", "posts", "posts_00002.jsonl")
            with open(second, "a", encoding="utf-8") as fh:
                fh.write("{broken\n")
            with self.assertRaises(Exception):
                store.count_raw()
            # A fresh handle also detects the corruption when scanning.
            with self.assertRaises(Exception):
                RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "run1",
                                  chunk_size=2)

    def test_bad_chunk_size_rejected(self):
        from reddit_collector.config import ConfigError

        with TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "bad.yaml"
            cfg.write_text("subreddit: python\noutput:\n  chunk_size: 0\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(str(cfg))


class TestResumeAndFresh(unittest.TestCase):
    def _client(self, pages):
        client = MagicMock()
        seq = [{"data": list(p)} for p in pages]
        client.search_posts.side_effect = lambda **kw: seq.pop(0) if seq else {"data": []}
        return client

    def _post(self, pid, ts):
        return {"id": pid, "created_utc": ts, "title": f"t-{pid}",
                "subreddit": "python", "author": "u", "score": 1,
                "num_comments": 0, "url": "http://x"}

    def test_interrupted_resume_continues_chunks(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp, chunk_size=3)
            pages = [[self._post("a", 1), self._post("b", 2)],
                     [self._post("c", 3), self._post("d", 4)]]
            first = collect_new(config, client=self._client(pages),
                                should_stop=lambda: True)
            self.assertTrue(first["stopped"])
            run_id = first["run_id"]
            mid = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", run_id, chunk_size=3)
            self.assertEqual(mid.count_raw(), 2)
            second = resume_collection(
                config, client=self._client([[self._post("c", 3), self._post("d", 4)], []]))
            self.assertEqual(second["run_id"], run_id)
            final = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", run_id, chunk_size=3)
            self.assertEqual([r["id"] for r in final.iter_processed()], ["a", "b", "c", "d"])
            self.assertEqual(_chunk_counts(tmp, run_id, "raw", "posts"), [3, 1])
            self.assertEqual(second["total_posts"], 4)

    def test_crash_between_append_and_checkpoint_no_dupes_chunked(self):
        with TemporaryDirectory() as tmp:
            config = dict(_write_config(tmp, chunk_size=2))
            config["max_posts"] = 2
            first = collect_new(
                config, client=self._client([[self._post("a", 1), self._post("b", 2)],
                                             [self._post("c", 3), self._post("d", 4)]]))
            crashed = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed",
                                        first["run_id"], chunk_size=2)
            crashed.append_page([self._post("c", 3)])
            config["max_posts"] = None
            second = resume_collection(
                config, client=self._client([[self._post("c", 3), self._post("d", 4)], []]))
            self.assertEqual(second["posts_this_run"], 1)
            stored = list(crashed.iter_processed())
            # Re-read via a fresh handle to include resume appends.
            fresh = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed",
                                      first["run_id"], chunk_size=2)
            self.assertEqual([r["id"] for r in fresh.iter_processed()], ["a", "b", "c", "d"])

    def test_fresh_collection_new_run_keeps_old_chunks(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp, chunk_size=2)
            first = collect_new(config, client=self._client([[self._post("a", 1)]]))
            second = collect_new(config, fresh=True,
                                 client=self._client([[self._post("b", 2)]]))
            self.assertNotEqual(first["run_id"], second["run_id"])
            old = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed",
                                    first["run_id"], chunk_size=2)
            new = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed",
                                    second["run_id"], chunk_size=2)
            self.assertEqual([r["id"] for r in old.iter_processed()], ["a"])
            self.assertEqual([r["id"] for r in new.iter_processed()], ["b"])

    def test_comment_resume_chunked(self):
        def _t1(cid, parent):
            return {"kind": "t1", "data": {
                "id": cid, "parent_id": parent, "link_id": "t3_p1", "author": "u",
                "body": "b", "score": 1, "created_utc": 1700000000,
                "subreddit": "python", "permalink": "/", "replies": ""}}

        with TemporaryDirectory() as tmp:
            config = _write_config(tmp, chunk_size=2)
            store = RunStore(f"{tmp}/raw", f"{tmp}/processed", run_id="run1", chunk_size=2)
            store.append_page([_raw("p1", 1), _raw("p2", 1)])
            c1 = MagicMock()
            c1.get_comment_tree.side_effect = [{"data": [_t1("c1", "t3_p1")]}]
            first = collect_comments(config, run_id="run1", max_comment_posts=1, client=c1)
            self.assertEqual(first["posts_completed"], 1)
            c2 = MagicMock()
            c2.get_comment_tree.side_effect = [{"data": [_t1("c2", "t3_p2")]}]
            second = collect_comments(config, run_id="run1", client=c2)
            self.assertEqual(second["total_comments"], 2)
            final = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "run1", chunk_size=2)
            self.assertEqual(final.count_processed_comments(), 2)


class TestLegacySingleFile(unittest.TestCase):
    def test_legacy_reads_combined_with_chunks(self):
        with TemporaryDirectory() as tmp:
            store = _store(tmp, chunk_size=2)
            store.write_manifest({})
            # Simulate a pre-upgrade run: legacy single file with 2 records.
            legacy = Path(tmp, "raw", "run1", "posts.jsonl")
            legacy_proc = Path(tmp, "processed", "run1", "posts.jsonl")
            legacy.write_text(
                json.dumps(_raw("old1")) + "\n" + json.dumps(_raw("old2")) + "\n",
                encoding="utf-8")
            legacy_proc.write_text(
                json.dumps({"id": "old1"}) + "\n" + json.dumps({"id": "old2"}) + "\n",
                encoding="utf-8")
            reopened = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "run1",
                                         chunk_size=2)
            self.assertEqual(reopened.count_raw(), 2)
            # New writes go to chunks; legacy files untouched.
            before = legacy.read_text(encoding="utf-8")
            reopened.append_page([_raw("new1")])
            self.assertEqual(legacy.read_text(encoding="utf-8"), before)
            self.assertEqual(reopened.count_raw(), 3)
            self.assertEqual([r["id"] for r in reopened.iter_raw()],
                             ["old1", "old2", "new1"])
            self.assertTrue((Path(tmp, "raw", "run1", "posts",
                                   "posts_00001.jsonl")).is_file())

    def test_legacy_not_deleted_or_renamed(self):
        with TemporaryDirectory() as tmp:
            store = _store(tmp, chunk_size=10)
            store.write_manifest({})
            store.append_page([_raw("a")])
            # Manually add a legacy file alongside chunks (mixed state).
            legacy = Path(tmp, "raw", "run1", "posts.jsonl")
            legacy.write_text(json.dumps(_raw("legacy")) + "\n", encoding="utf-8")
            reopened = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "run1",
                                         chunk_size=10)
            self.assertTrue(legacy.is_file())
            self.assertEqual(reopened.count_raw(), 2)  # 1 chunk + 1 legacy


if __name__ == "__main__":
    unittest.main()
