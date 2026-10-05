"""Tests for Phase-2 comment collection. HTTP is mocked; files go to temp dirs."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from reddit_collector.checkpoint import (
    CheckpointError,
    load_comment_checkpoint,
    save_comment_checkpoint,
)
from reddit_collector.collector import CollectorError, collect_comments
from reddit_collector.config import load_config
from reddit_collector.storage import RunStore


def _t1(cid, parent, body="text"):
    return {"kind": "t1", "data": {
        "id": cid, "parent_id": parent, "link_id": "t3_p1", "author": "u",
        "body": body, "score": 1, "created_utc": 1700000000,
        "subreddit": "python", "permalink": f"/r/python/comments/p1/x/{cid}/",
        "replies": ""}}


def _tree(*nodes):
    return {"data": list(nodes)}


def _client(trees):
    """Mock client: get_comment_tree returns the payload per link_id in order."""
    client = MagicMock()
    seq = list(trees)
    client.get_comment_tree.side_effect = lambda **kw: seq.pop(0) if seq else {"data": []}
    return client


def _write_config(tmp, extra=""):
    cfg = Path(tmp) / "config.yaml"
    cfg.write_text(
        "subreddit: python\n"
        "limit: 10\n"
        "output:\n"
        f"  raw_dir: {tmp}/raw\n"
        f"  processed_dir: {tmp}/processed\n"
        f"  checkpoint_dir: {tmp}/checkpoints\n"
        "logging:\n"
        f"  file: {tmp}/run.log\n" + extra,
        encoding="utf-8",
    )
    return load_config(str(cfg))


def _seed_posts(tmp, run_id, posts):
    """Create a finished post run directly (no HTTP)."""
    store = RunStore(f"{tmp}/raw", f"{tmp}/processed", run_id=run_id)
    raws = []
    for pid, n_comments in posts:
        raws.append({"id": pid, "subreddit": "python", "title": "t", "selftext": "",
                     "author": "u", "score": 1, "num_comments": n_comments,
                     "created_utc": 1700000000, "url": "http://x"})
    store.append_page(raws)
    return store


class TestCommentCheckpoint(unittest.TestCase):
    def test_roundtrip(self):
        with TemporaryDirectory() as tmp:
            state = {"completed_post_ids": ["a", "b"], "posts_completed": 2,
                     "posts_skipped_empty": 1, "comments_collected": 7}
            save_comment_checkpoint(tmp, state, "run1")
            loaded = load_comment_checkpoint(tmp)
            assert loaded is not None
            self.assertEqual(loaded["run_id"], "run1")
            self.assertEqual(loaded["comments"]["completed_post_ids"], ["a", "b"])
            self.assertEqual(loaded["comments"]["comments_collected"], 7)

    def test_missing_returns_none(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(load_comment_checkpoint(tmp))

    def test_invalid_rejected(self):
        with TemporaryDirectory() as tmp:
            Path(tmp, "comments-checkpoint.json").write_text('{"version": 1}', encoding="utf-8")
            with self.assertRaises(CheckpointError):
                load_comment_checkpoint(tmp)


class TestCollectComments(unittest.TestCase):
    def test_collects_skips_empty_and_checkpoints(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p1", 2), ("p2", 0), ("p3", 1)])
            client = _client([_tree(_t1("c1", "t3_p1"), _t1("c2", "t1_c1")),
                              _tree(_t1("c3", "t3_p3"))])
            summary = collect_comments(config, run_id="run1", client=client)

            self.assertEqual(summary["run_id"], "run1")
            self.assertEqual(summary["posts_completed"], 2)
            self.assertEqual(summary["posts_skipped_empty"], 1)
            self.assertEqual(summary["comments_collected"], 3)
            self.assertEqual(client.get_comment_tree.call_count, 2)
            store = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "run1")
            self.assertEqual(store.count_processed_comments(), 3)
            self.assertEqual([r["comment_id"] for r in store.iter_processed_comments()],
                             ["c1", "c2", "c3"])
            loaded = load_comment_checkpoint(f"{tmp}/checkpoints")
            assert loaded is not None
            self.assertEqual(sorted(loaded["comments"]["completed_post_ids"]),
                             ["p1", "p2", "p3"])

    def test_resume_skips_completed_without_rerequest(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p1", 1), ("p2", 1)])
            first = collect_comments(
                config, run_id="run1", max_comment_posts=1,
                client=_client([_tree(_t1("c1", "t3_p1"))]))
            self.assertEqual(first["posts_completed"], 1)

            client2 = _client([_tree(_t1("c2", "t3_p2"))])
            second = collect_comments(config, run_id="run1", client=client2)
            self.assertEqual(client2.get_comment_tree.call_count, 1)  # only p2
            self.assertEqual(second["comments_collected"], 1)
            self.assertEqual(second["total_comments"], 2)
            store = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "run1")
            self.assertEqual([r["comment_id"] for r in store.iter_processed_comments()],
                             ["c1", "c2"])

    def test_crash_repair_no_duplicates(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            store = _seed_posts(tmp, "run1", [("p1", 1)])
            # Simulate crash: comments appended, completion never checkpointed.
            store.append_raw_comments([{"kind": "t1", "data": {"id": "c1"}}])
            from reddit_collector.comments import normalize_comment
            store.append_processed_comments(
                [normalize_comment({"kind": "t1", "data": {"id": "c1"}}, "p1", 0, [])])

            client = _client([_tree(_t1("c1", "t3_p1"))])
            summary = collect_comments(config, run_id="run1", client=client)
            self.assertEqual(client.get_comment_tree.call_count, 0)  # repaired, not refetched
            self.assertEqual(summary["comments_collected"], 0)
            self.assertEqual([r["comment_id"] for r in store.iter_processed_comments()], ["c1"])

    def test_missing_run_and_posts(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            with self.assertRaises(CollectorError):
                collect_comments(config, run_id="nope", client=_client([]))
            with self.assertRaises(CollectorError):
                collect_comments(config, client=_client([]))  # no post checkpoint either

    def test_skip_empty_configurable_off(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p1", 0)])
            client = _client([{"data": []}])
            summary = collect_comments(config, run_id="run1", client=client,
                                       skip_empty_posts=False)
            self.assertEqual(client.get_comment_tree.call_count, 1)
            self.assertEqual(summary["posts_completed"], 1)

    def test_post_ids_come_from_stored_run(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p9", 1)])
            client = _client([_tree(_t1("c9", "t3_p9"))])
            collect_comments(config, run_id="run1", client=client)
            _, kwargs = client.get_comment_tree.call_args
            self.assertEqual(kwargs["link_id"], "p9")

    def test_empty_posts_file_is_an_error_not_silent_success(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            RunStore(f"{tmp}/raw", f"{tmp}/processed", run_id="run1")  # dirs only, no posts
            with self.assertRaises(CollectorError) as ctx:
                collect_comments(config, run_id="run1", client=_client([]))
            self.assertIn("no processed posts", str(ctx.exception))

    def test_records_without_usable_ids_are_an_error(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            store = RunStore(f"{tmp}/raw", f"{tmp}/processed", run_id="run1")
            store.append_page([{"noid": 1}, {"id": None, "title": "x"}])
            with self.assertRaises(CollectorError) as ctx:
                collect_comments(config, run_id="run1", client=_client([]))
            self.assertIn("usable post id", str(ctx.exception))

    def test_all_empty_posts_is_still_success(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p1", 0), ("p2", 0)])
            client = _client([])
            summary = collect_comments(config, run_id="run1", client=client)
            self.assertEqual(summary["posts_skipped_empty"], 2)
            self.assertEqual(summary["comments_collected"], 0)
            self.assertEqual(client.get_comment_tree.call_count, 0)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.now += secs


def _http_response(status=200, json_data=None, headers=None, text="",
                   side_effect=None):
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = text
    if isinstance(json_data, Exception):
        resp.json.side_effect = json_data
    else:
        resp.json.return_value = json_data
    if side_effect is not None:
        resp.get_effect = side_effect
    return resp


def _live_client(responses, clock, **limiter_kwargs):
    """Real ArcticShiftClient + real RateLimiter over a mocked session."""
    from unittest.mock import MagicMock

    from reddit_collector.client import ArcticShiftClient
    from reddit_collector.rate_limit import RateLimiter

    session = MagicMock()
    session.get.side_effect = list(responses)
    limiter_kwargs.setdefault("min_interval_secs", 0.001)
    limiter_kwargs.setdefault("sleep_fn", clock.sleep)
    limiter_kwargs.setdefault("clock_fn", clock.monotonic)
    limiter = RateLimiter(**limiter_kwargs)
    return ArcticShiftClient(session=session, rate_limiter=limiter), session, clock


class TestCommentRateLimitHandling(unittest.TestCase):
    def test_429_then_success(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p1", 1)])
            clock = FakeClock()
            client, session, _ = _live_client(
                [_http_response(429, {"error": "slow"}, text="slow"),
                 _http_response(200, {"data": [_t1("c1", "t3_p1")]})],
                clock, max_retries=3, backoff_base_secs=1.0)
            summary = collect_comments(config, run_id="run1", client=client)
            self.assertEqual(session.get.call_count, 2)
            self.assertEqual(clock.sleeps, [1.0])  # backoff, no server wait given
            self.assertEqual(summary["comments_collected"], 1)
            resumed = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "run1")
            self.assertEqual([r["comment_id"] for r in resumed.iter_processed_comments()],
                             ["c1"])

    def test_retry_after_respected(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p1", 1)])
            clock = FakeClock()
            client, session, _ = _live_client(
                [_http_response(429, {"error": "slow"}, text="slow",
                                 headers={"Retry-After": "5"}),
                 _http_response(200, {"data": [_t1("c1", "t3_p1")]})],
                clock, max_retries=3, backoff_base_secs=1.0)
            collect_comments(config, run_id="run1", client=client)
            self.assertEqual(session.get.call_count, 2)
            self.assertEqual(clock.sleeps, [5.0])

    def test_422_timeout_then_success(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p1", 1)])
            clock = FakeClock()
            client, session, _ = _live_client(
                [_http_response(422, {"data": None, "error": "Timeout. Maybe slow down a bit"},
                                 text="timeout"),
                 _http_response(200, {"data": [_t1("c1", "t3_p1")]})],
                clock, max_retries=3, backoff_base_secs=1.0)
            summary = collect_comments(config, run_id="run1", client=client)
            self.assertEqual(session.get.call_count, 2)
            self.assertEqual(clock.sleeps, [1.0])
            self.assertEqual(summary["comments_collected"], 1)

    def test_500s_then_success_with_backoff(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p1", 1)])
            clock = FakeClock()
            client, session, _ = _live_client(
                [_http_response(500, text="boom"),
                 _http_response(503, text="boom"),
                 _http_response(200, {"data": [_t1("c1", "t3_p1")]})],
                clock, max_retries=3, backoff_base_secs=1.0, backoff_max_secs=60.0)
            collect_comments(config, run_id="run1", client=client)
            self.assertEqual(session.get.call_count, 3)
            self.assertEqual(clock.sleeps, [1.0, 2.0])

    def test_persistent_failure_marks_nothing_complete(self):
        from reddit_collector.checkpoint import load_comment_checkpoint
        from reddit_collector.client import ArcticShiftAPIError

        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p1", 2)])
            clock = FakeClock()
            client, session, _ = _live_client(
                [_http_response(500, text="boom")] * 5, clock, max_retries=1)
            with self.assertRaises(ArcticShiftAPIError):
                collect_comments(config, run_id="run1", client=client)
            self.assertEqual(session.get.call_count, 2)  # initial + 1 retry
            # Failed post NOT marked complete; no partial comments stored.
            doc = load_comment_checkpoint(f"{tmp}/checkpoints")
            self.assertTrue(doc is None or doc["comments"]["completed_post_ids"] == [])
            resumed = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "run1")
            self.assertEqual(list(resumed.iter_processed_comments()), [])
            # A later healthy run recovers the same post fully.
            clock2 = FakeClock()
            client2, _, _ = _live_client(
                [_http_response(200, {"data": [_t1("c1", "t3_p1"), _t1("c2", "t1_c1")]})],
                clock2)
            summary = collect_comments(config, run_id="run1", client=client2)
            self.assertEqual(summary["comments_collected"], 2)


class TestDuplicateProtection(unittest.TestCase):
    def _ids(self, tmp):
        store = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "run1")
        recs = list(store.iter_processed_comments())
        return recs, [r["comment_id"] for r in recs]

    def test_same_collection_twice_no_duplicates(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p1", 2), ("p2", 1)])
            trees = [_tree(_t1("c1", "t3_p1"), _t1("c2", "t1_c1")),
                     _tree(_t1("c3", "t3_p2"))]
            first = collect_comments(config, run_id="run1", client=_client(trees))
            self.assertEqual(first["comments_collected"], 3)
            # Second identical run: nothing re-requested, nothing rewritten.
            client2 = _client([_tree(_t1("c1", "t3_p1"))])
            second = collect_comments(config, run_id="run1", client=client2)
            self.assertEqual(client2.get_comment_tree.call_count, 0)
            self.assertEqual(second["comments_collected"], 0)
            recs, ids = self._ids(tmp)
            self.assertEqual(len(recs), 3)
            self.assertEqual(len(set(ids)), 3)

    def test_crash_between_append_and_checkpoint_repaired(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("p1", 1), ("p2", 1)])
            collect_comments(config, run_id="run1", max_comment_posts=1,
                             client=_client([_tree(_t1("c1", "t3_p1"))]))
            # Simulate crash: p2's comments reach the files, checkpoint never updates.
            from reddit_collector.comments import normalize_comment

            store = RunStore.existing(f"{tmp}/raw", f"{tmp}/processed", "run1")
            store.append_raw_comments([{"kind": "t1", "data": {"id": "c2"}}])
            store.append_processed_comments(
                [normalize_comment({"kind": "t1", "data": {"id": "c2"}}, "p2", 0, [])])
            # Resume must NOT refetch p2 and must NOT duplicate c2.
            client2 = _client([_tree(_t1("c2", "t3_p2"))])
            collect_comments(config, run_id="run1", client=client2)
            self.assertEqual(client2.get_comment_tree.call_count, 0)
            _, ids = self._ids(tmp)
            self.assertEqual(sorted(ids), ["c1", "c2"])

    def test_comments_never_cross_posts(self):
        with TemporaryDirectory() as tmp:
            config = _write_config(tmp)
            _seed_posts(tmp, "run1", [("pa", 2), ("pb", 2)])
            trees = [_tree(_t1("ca1", "t3_pa"), _t1("ca2", "t1_ca1")),
                     _tree(_t1("cb1", "t3_pb"), _t1("cb2", "t1_cb1"))]
            collect_comments(config, run_id="run1", client=_client(trees))
            # Lose the checkpoint but keep the files: reconcile must suppress
            # all refetches purely from stored post ids.
            Path(tmp, "checkpoints", "comments-checkpoint.json").unlink()
            client2 = _client([_tree(_t1("xx", "t3_pa"))])
            collect_comments(config, run_id="run1", client=client2)
            self.assertEqual(client2.get_comment_tree.call_count, 0)
            recs, ids = self._ids(tmp)
            by_post: dict[str, list[str]] = {}
            for r in recs:
                by_post.setdefault(r["post_id"], []).append(r["comment_id"])
            self.assertEqual(by_post, {"pa": ["ca1", "ca2"], "pb": ["cb1", "cb2"]})
            self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
