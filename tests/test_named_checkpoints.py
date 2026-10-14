"""Tests for persistent named per-collection checkpoints. No real HTTP calls."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from reddit_collector.checkpoint import (
    POSTS_CHECKPOINT_FILENAME,
    CheckpointError,
    checkpoint_dir_for_config,
    collection_name_for_config,
    has_legacy_checkpoint,
    list_collections,
    load_checkpoint_doc,
    load_comment_checkpoint,
    migrate_legacy_checkpoint,
    save_checkpoint,
)
from reddit_collector.collector import (
    CollectorError,
    collect_comments,
    collect_new,
    describe_status,
    list_available_checkpoints,
    resume_collection,
)
from reddit_collector.config import load_config
from reddit_collector.storage import RunStore


def _post(pid, ts, **extra):
    post = {"id": pid, "created_utc": ts, "title": f"t-{pid}",
            "subreddit": "python", "author": "u", "score": 1,
            "num_comments": 0, "url": "http://x"}
    post.update(extra)
    return post


def _client(pages):
    client = MagicMock()
    seq = [{"data": list(p)} for p in pages]
    client.search_posts.side_effect = lambda **kw: seq.pop(0) if seq else {"data": []}
    return client


def _comment_client(trees):
    client = MagicMock()
    seq = list(trees)
    client.get_comment_tree.side_effect = lambda **kw: seq.pop(0) if seq else {"data": []}
    return client


def _write_config(tmp, subreddit="Philippines", after="2026-01-01",
                  before="2026-06-30", extra=""):
    cfg = Path(tmp) / f"config-{subreddit}.yaml"
    cfg.write_text(
        f"subreddit: {subreddit}\n"
        f"after: '{after}'\n"
        f"before: '{before}'\n"
        "limit: 2\n"
        "output:\n"
        f"  raw_dir: {tmp}/raw\n"
        f"  processed_dir: {tmp}/processed\n"
        f"  checkpoint_dir: {tmp}/checkpoints\n"
        "logging:\n"
        f"  file: {tmp}/run.log\n" + extra,
        encoding="utf-8",
    )
    return load_config(str(cfg))


PH = ("Philippines", "2026-01-01", "2026-06-30")
TH = ("Thailand", "2026-01-01", "2026-06-30")
PH_NAME = "Philippines-2026-01-01-2026-06-30"
TH_NAME = "Thailand-2026-01-01-2026-06-30"


class TestNaming(unittest.TestCase):
    def test_example_names(self):
        with TemporaryDirectory() as tmp:
            ph = _write_config(tmp, *PH)
            th = _write_config(tmp, *TH)
            self.assertEqual(collection_name_for_config(ph), PH_NAME)
            self.assertEqual(collection_name_for_config(th), TH_NAME)

    def test_subreddit_normalized_consistently(self):
        with TemporaryDirectory() as tmp:
            base = _write_config(tmp, *PH)
            variant = dict(base)
            variant["subreddit"] = "r/Philippines"
            # Naming strips the r/ prefix consistently.
            self.assertEqual(collection_name_for_config(variant),
                             collection_name_for_config(base))
            # The paginator safeguard stays exact: resuming with a differently
            # spelled filter fails safely (no network, no mutation) rather
            # than silently mixing datasets.
            collect_new(base, client=_client([[_post("pa", 1)]]))
            client = _client([])
            with self.assertRaises(CollectorError) as ctx:
                resume_collection(variant, client=client)
            self.assertIn("Checkpoint mismatch", str(ctx.exception))
            self.assertEqual(client.search_posts.call_count, 0)
            names = [e["name"] for e in list_collections(f"{tmp}/checkpoints")]
            self.assertEqual(names, [PH_NAME])

    def test_sanitized_filename_safe(self):
        from reddit_collector.checkpoint import sanitize_name_part

        self.assertEqual(sanitize_name_part("a/b:c"), "a_b_c")
        self.assertNotIn("/", sanitize_name_part("r/Weird Name!"))


class TestSeparateCheckpoints(unittest.TestCase):
    def test_save_separate_philippines_and_thailand(self):
        with TemporaryDirectory() as tmp:
            ph = _write_config(tmp, *PH)
            th = _write_config(tmp, *TH)
            collect_new(ph, client=_client([[_post("pa", 1)]]))
            collect_new(th, client=_client([[_post("ta", 1)]]))
            ph_dir = checkpoint_dir_for_config(f"{tmp}/checkpoints", ph)
            th_dir = checkpoint_dir_for_config(f"{tmp}/checkpoints", th)
            self.assertTrue((ph_dir / POSTS_CHECKPOINT_FILENAME).is_file())
            self.assertTrue((th_dir / POSTS_CHECKPOINT_FILENAME).is_file())
            self.assertNotEqual(ph_dir, th_dir)

    def test_switching_does_not_delete_either(self):
        with TemporaryDirectory() as tmp:
            ph = _write_config(tmp, *PH)
            th = _write_config(tmp, *TH)
            collect_new(ph, client=_client([[_post("pa", 1)]]))
            before = (checkpoint_dir_for_config(f"{tmp}/checkpoints", ph)
                      / POSTS_CHECKPOINT_FILENAME).read_text(encoding="utf-8")
            collect_new(th, client=_client([[_post("ta", 1)]]))
            after = (checkpoint_dir_for_config(f"{tmp}/checkpoints", ph)
                     / POSTS_CHECKPOINT_FILENAME).read_text(encoding="utf-8")
            self.assertEqual(before, after)

    def test_resume_philippines_after_thailand(self):
        with TemporaryDirectory() as tmp:
            ph = dict(_write_config(tmp, *PH))
            ph["max_posts"] = 2
            th = _write_config(tmp, *TH)
            first = collect_new(ph, client=_client([[ _post("pa", 1), _post("pb", 2)],
                                                     [_post("pc", 3)]]))
            collect_new(th, client=_client([[_post("ta", 1)]]))
            ph["max_posts"] = None
            second = resume_collection(ph, client=_client([[ _post("pc", 3),
                                                             _post("pd", 4)], []]))
            self.assertEqual(second["run_id"], first["run_id"])
            self.assertEqual(second["total_posts"], 4)

    def test_resume_thailand_after_philippines(self):
        with TemporaryDirectory() as tmp:
            ph = _write_config(tmp, *PH)
            th = dict(_write_config(tmp, *TH))
            th["max_posts"] = 1
            collect_new(ph, client=_client([[_post("pa", 1)]]))
            first = collect_new(th, client=_client([[_post("ta", 1)],
                                                     [_post("tb", 2)]]))
            self.assertEqual(first["posts_this_run"], 1)
            th["max_posts"] = None
            second = resume_collection(th, client=_client([[_post("tb", 2)], []]))
            self.assertEqual(second["run_id"], first["run_id"])
            self.assertEqual(second["total_posts"], 2)


class TestMismatchValidation(unittest.TestCase):
    def _seed_thailand(self, tmp):
        th = _write_config(tmp, *TH)
        collect_new(th, client=_client([[_post("ta", 1)]]))
        return th

    def test_reject_thailand_checkpoint_with_philippines_config(self):
        with TemporaryDirectory() as tmp:
            self._seed_thailand(tmp)
            ph = _write_config(tmp, *PH)
            with self.assertRaises(CollectorError) as ctx:
                resume_collection(ph, checkpoint=TH_NAME, client=_client([]))
            message = str(ctx.exception)
            self.assertIn("Checkpoint mismatch", message)
            self.assertIn("r/Thailand", message)
            self.assertIn("r/Philippines", message)

    def test_reject_mismatched_start_date(self):
        with TemporaryDirectory() as tmp:
            self._seed_thailand(tmp)
            shifted = _write_config(tmp, "Thailand", "2026-02-01", "2026-06-30")
            with self.assertRaises(CollectorError) as ctx:
                resume_collection(shifted, checkpoint=TH_NAME, client=_client([]))
            self.assertIn("start date", str(ctx.exception))

    def test_reject_mismatched_end_date(self):
        with TemporaryDirectory() as tmp:
            self._seed_thailand(tmp)
            shifted = _write_config(tmp, "Thailand", "2026-01-01", "2026-12-31")
            with self.assertRaises(CollectorError) as ctx:
                resume_collection(shifted, checkpoint=TH_NAME, client=_client([]))
            self.assertIn("end date", str(ctx.exception))

    def test_reject_incompatible_filter(self):
        with TemporaryDirectory() as tmp:
            self._seed_thailand(tmp)
            filtered = _write_config(tmp, *TH, extra="query: election\n")
            with self.assertRaises(CollectorError) as ctx:
                resume_collection(filtered, checkpoint=TH_NAME, client=_client([]))
            self.assertIn("Checkpoint mismatch", str(ctx.exception))
            self.assertIn("query", str(ctx.exception))

    def test_validation_before_network_or_mutation(self):
        with TemporaryDirectory() as tmp:
            self._seed_thailand(tmp)
            ph = _write_config(tmp, *PH)
            th_dir = checkpoint_dir_for_config(f"{tmp}/checkpoints", _write_config(tmp, *TH))
            before = sorted(p.name for p in (th_dir).iterdir())
            raw_before = sorted(p.name for p in Path(tmp, "raw").iterdir())
            client = _client([[ _post("x", 1)]])
            with self.assertRaises(CollectorError):
                resume_collection(ph, checkpoint=TH_NAME, client=client)
            self.assertEqual(client.search_posts.call_count, 0)
            self.assertEqual(sorted(p.name for p in th_dir.iterdir()), before)
            self.assertEqual(sorted(p.name for p in Path(tmp, "raw").iterdir()), raw_before)

    def test_cli_checkpoint_flag_mismatch_exits_2(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from reddit_collector.__main__ import main

        with TemporaryDirectory() as tmp:
            self._seed_thailand(tmp)
            _write_config(tmp, *PH)
            cfg_path = Path(tmp) / "config-Philippines.yaml"
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["resume", "--config", str(cfg_path), "--checkpoint", TH_NAME])
            self.assertEqual(code, 2)
            self.assertIn("Checkpoint mismatch", err.getvalue())


class TestPostCommentConsistency(unittest.TestCase):
    def _tree(self, cid, parent):
        return {"data": [{"kind": "t1", "data": {
            "id": cid, "parent_id": parent, "link_id": "t3_p1", "author": "u",
            "body": "b", "score": 1, "created_utc": 1700000000,
            "subreddit": "Philippines", "permalink": "/", "replies": ""}}]}

    def test_post_and_comment_checkpoints_share_collection(self):
        with TemporaryDirectory() as tmp:
            ph = _write_config(tmp, *PH)
            post = _post("p1", 1, num_comments=1, subreddit="Philippines")
            run = collect_new(ph, client=_client([[post]]))
            collect_comments(ph, run_id=run["run_id"],
                             client=_comment_client([self._tree("c1", "t3_p1")]))
            cp_dir = checkpoint_dir_for_config(f"{tmp}/checkpoints", ph)
            posts_doc = load_checkpoint_doc(cp_dir)
            comments_doc = load_comment_checkpoint(cp_dir)
            assert posts_doc is not None and comments_doc is not None
            self.assertEqual(posts_doc["run_id"], comments_doc["run_id"])
            self.assertEqual(posts_doc["collection"]["name"], PH_NAME)
            self.assertEqual(comments_doc["collection"]["name"], PH_NAME)

    def test_comment_run_binding_rejects_foreign_run(self):
        from reddit_collector.checkpoint import save_comment_checkpoint

        with TemporaryDirectory() as tmp:
            ph = _write_config(tmp, *PH)
            run = collect_new(ph, client=_client([[_post("p1", 1)]]))
            cp_dir = str(checkpoint_dir_for_config(f"{tmp}/checkpoints", ph))
            save_comment_checkpoint(
                cp_dir,
                {"completed_post_ids": [], "posts_completed": 0,
                 "posts_skipped_empty": 0, "comments_collected": 0},
                "some-other-run",
            )
            with self.assertRaises(CollectorError):
                collect_comments(ph, run_id=run["run_id"], client=_client([]))


class TestFreshResumeChunked(unittest.TestCase):
    def test_fresh_keeps_other_collection_and_datasets(self):
        with TemporaryDirectory() as tmp:
            ph = _write_config(tmp, *PH)
            th = _write_config(tmp, *TH)
            first_ph = collect_new(ph, client=_client([[_post("pa", 1)]]))
            collect_new(th, client=_client([[_post("ta", 1)]]))
            th_dir = checkpoint_dir_for_config(f"{tmp}/checkpoints", th)
            th_snapshot = (th_dir / POSTS_CHECKPOINT_FILENAME).read_text(encoding="utf-8")
            second_ph = collect_new(ph, fresh=True, client=_client([[_post("pb", 2)]]))
            self.assertNotEqual(first_ph["run_id"], second_ph["run_id"])
            # Other collection untouched.
            self.assertEqual((th_dir / POSTS_CHECKPOINT_FILENAME).read_text(encoding="utf-8"),
                             th_snapshot)
            # Old dataset files kept.
            old_store = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", first_ph["run_id"])
            self.assertEqual([r["id"] for r in old_store.iter_processed()], ["pa"])
            new_store = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", second_ph["run_id"])
            self.assertEqual([r["id"] for r in new_store.iter_processed()], ["pb"])

    def test_fresh_refuses_without_flag_and_names_collection(self):
        with TemporaryDirectory() as tmp:
            ph = _write_config(tmp, *PH)
            collect_new(ph, client=_client([[_post("pa", 1)]]))
            with self.assertRaises(CollectorError) as ctx:
                collect_new(ph, client=_client([]))
            self.assertIn(PH_NAME, str(ctx.exception))
            self.assertIn("--fresh", str(ctx.exception))

    def test_resume_continues_chunks(self):
        with TemporaryDirectory() as tmp:
            ph = dict(_write_config(tmp, *PH))
            ph["output"] = dict(ph["output"])
            ph["output"]["chunk_size"] = 2
            ph["max_posts"] = 2
            first = collect_new(ph, client=_client([[ _post("pa", 1), _post("pb", 2)],
                                                     [_post("pc", 3)]]))
            ph["max_posts"] = None
            second = resume_collection(ph, client=_client([[ _post("pc", 3),
                                                             _post("pd", 4)], []]))
            self.assertEqual(second["run_id"], first["run_id"])
            store = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", first["run_id"],
                                      chunk_size=2)
            self.assertEqual([r["id"] for r in store.iter_processed()],
                             ["pa", "pb", "pc", "pd"])
            chunks = sorted((Path(tmp) / "processed" / first["run_id"] / "posts").glob("*.jsonl"))
            counts = [sum(1 for line in p.read_text(encoding="utf-8").splitlines()
                          if line.strip()) for p in chunks]
            self.assertEqual(counts, [2, 2])
            for count in counts:
                self.assertLessEqual(count, 2)


class TestGuiListing(unittest.TestCase):
    def test_empty_list(self):
        from reddit_collector.gui import list_checkpoint_options

        with TemporaryDirectory() as tmp:
            self.assertEqual(list_checkpoint_options(f"{tmp}/checkpoints"), [])

    def test_lists_both_with_status(self):
        from reddit_collector.gui import format_checkpoint_option, list_checkpoint_options

        with TemporaryDirectory() as tmp:
            collect_new(_write_config(tmp, *PH), client=_client([[_post("pa", 1)]]))
            collect_new(_write_config(tmp, *TH), client=_client([[_post("ta", 1)]]))
            options = list_checkpoint_options(f"{tmp}/checkpoints")
            self.assertEqual([o["name"] for o in options], [PH_NAME, TH_NAME])
            labels = [format_checkpoint_option(
                next(e for e in list_available_checkpoints(_write_config(tmp, *PH))
                     if e["name"] == o["name"])) for o in options]
            self.assertTrue(all(PH_NAME in labels[0] or "in_progress" in labels[0] for _ in [0]))
            for option in options:
                self.assertIn(option["name"], option["label"])
                self.assertIn(option["status"], ("in_progress", "complete"))

    def test_selection_mismatch_message(self):
        from reddit_collector.gui import describe_selection_mismatch

        with TemporaryDirectory() as tmp:
            collect_new(_write_config(tmp, *TH), client=_client([[_post("ta", 1)]]))
            ph = _write_config(tmp, *PH)
            error = describe_selection_mismatch(ph, TH_NAME, f"{tmp}/checkpoints")
            assert error is not None
            self.assertIn("Checkpoint mismatch", error)
            self.assertIn("r/Thailand", error)
            self.assertIn("r/Philippines", error)

    def test_selection_match_ok_and_populate(self):
        from reddit_collector.gui import checkpoint_form_values, describe_selection_mismatch

        with TemporaryDirectory() as tmp:
            collect_new(_write_config(tmp, *TH), client=_client([[_post("ta", 1)]]))
            th = _write_config(tmp, *TH)
            self.assertIsNone(describe_selection_mismatch(th, TH_NAME, f"{tmp}/checkpoints"))
            values = checkpoint_form_values(f"{tmp}/checkpoints", TH_NAME)
            assert values is not None
            self.assertEqual(values["subreddit"], "Thailand")
            self.assertEqual(values["after"], "2026-01-01")
            self.assertEqual(values["before"], "2026-06-30")

    def test_missing_selection_reports_clearly(self):
        from reddit_collector.gui import describe_selection_mismatch

        with TemporaryDirectory() as tmp:
            ph = _write_config(tmp, *PH)
            error = describe_selection_mismatch(ph, "Nope-2026-01-01-2026-06-30",
                                                f"{tmp}/checkpoints")
            assert error is not None
            self.assertIn("No checkpoint", error)


class TestLegacyAndCorrupt(unittest.TestCase):
    def test_legacy_preserved_and_listed(self):
        with TemporaryDirectory() as tmp:
            from reddit_collector.paginator import PostPaginator

            client = MagicMock()
            client.search_posts.return_value = {"data": [{"id": "a", "created_utc": 1}]}
            paginator = PostPaginator(client, subreddit="Philippines", page_size=2,
                                      after="2026-01-01", before="2026-06-30")
            list(paginator.iter_pages())
            save_checkpoint(f"{tmp}/checkpoints", paginator.state(), run_id="legacy-run")
            self.assertTrue(has_legacy_checkpoint(f"{tmp}/checkpoints"))
            names = [e["name"] for e in list_collections(f"{tmp}/checkpoints")]
            self.assertIn("legacy", names)

    def test_legacy_not_adopted_silently(self):
        with TemporaryDirectory() as tmp:
            from reddit_collector.paginator import PostPaginator

            client = MagicMock()
            client.search_posts.return_value = {"data": [{"id": "a", "created_utc": 1}]}
            paginator = PostPaginator(client, subreddit="Philippines", page_size=2,
                                      after="2026-01-01", before="2026-06-30")
            list(paginator.iter_pages())
            save_checkpoint(f"{tmp}/checkpoints", paginator.state(), run_id="legacy-run")
            ph = _write_config(tmp, *PH)
            with self.assertRaises(CollectorError) as ctx:
                resume_collection(ph, client=_client([]))
            self.assertIn("nothing to resume", str(ctx.exception))
            # Legacy files left in place.
            self.assertTrue(has_legacy_checkpoint(f"{tmp}/checkpoints"))

    def test_migrate_legacy_explicitly(self):
        with TemporaryDirectory() as tmp:
            from reddit_collector.paginator import PostPaginator

            client = MagicMock()
            client.search_posts.return_value = {"data": [{"id": "a", "created_utc": 1}]}
            paginator = PostPaginator(client, subreddit="Philippines", page_size=2,
                                      after="2026-01-01", before="2026-06-30")
            list(paginator.iter_pages())
            save_checkpoint(f"{tmp}/checkpoints", paginator.state(), run_id="legacy-run")
            ph = _write_config(tmp, *PH)
            target = migrate_legacy_checkpoint(f"{tmp}/checkpoints", ph)
            self.assertEqual(target.name, PH_NAME)
            self.assertTrue((target / POSTS_CHECKPOINT_FILENAME).is_file())
            # Originals kept.
            self.assertTrue(has_legacy_checkpoint(f"{tmp}/checkpoints"))
            doc = load_checkpoint_doc(target)
            assert doc is not None
            self.assertEqual(doc["run_id"], "legacy-run")

    def test_migrate_rejects_incompatible_config(self):
        with TemporaryDirectory() as tmp:
            from reddit_collector.paginator import PostPaginator

            client = MagicMock()
            client.search_posts.return_value = {"data": [{"id": "a", "created_utc": 1}]}
            paginator = PostPaginator(client, subreddit="Philippines", page_size=2,
                                      after="2026-01-01", before="2026-06-30")
            list(paginator.iter_pages())
            save_checkpoint(f"{tmp}/checkpoints", paginator.state(), run_id="legacy-run")
            th = _write_config(tmp, *TH)
            with self.assertRaises(CheckpointError):
                migrate_legacy_checkpoint(f"{tmp}/checkpoints", th)

    def test_malformed_checkpoint_listed_and_resume_fails_safe(self):
        with TemporaryDirectory() as tmp:
            ph = _write_config(tmp, *PH)
            bad_dir = checkpoint_dir_for_config(f"{tmp}/checkpoints", ph)
            bad_dir.mkdir(parents=True, exist_ok=True)
            (bad_dir / POSTS_CHECKPOINT_FILENAME).write_text("{broken", encoding="utf-8")
            entries = list_collections(f"{tmp}/checkpoints")
            self.assertEqual(len(entries), 1)
            self.assertIn("error", entries[0])
            with self.assertRaises(CollectorError):
                resume_collection(ph, client=_client([]))
            # File left for inspection.
            self.assertTrue((bad_dir / POSTS_CHECKPOINT_FILENAME).exists())

    def test_status_lists_all_collections(self):
        with TemporaryDirectory() as tmp:
            collect_new(_write_config(tmp, *PH), client=_client([[_post("pa", 1)]]))
            collect_new(_write_config(tmp, *TH), client=_client([[_post("ta", 1)]]))
            info = describe_status(_write_config(tmp, *PH))
            names = [e["name"] for e in info["checkpoints"]]
            self.assertIn(PH_NAME, names)
            self.assertIn(TH_NAME, names)
            assert info["checkpoint"] is not None
            self.assertEqual(info["collection"], PH_NAME)


if __name__ == "__main__":
    unittest.main()
