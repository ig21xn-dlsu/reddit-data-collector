"""Tests for PostPaginator. The API client is mocked: no real HTTP calls."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from reddit_collector.paginator import PostPaginator


def _post(pid, ts):
    return {"id": pid, "created_utc": ts, "title": f"title {pid}"}


def _client(pages):
    """Mock client whose search_posts returns each payload in order, then empty."""
    client = MagicMock()
    seq = [{"data": list(p)} for p in pages]
    client.search_posts.side_effect = lambda **kwargs: seq.pop(0) if seq else {"data": []}
    return client


def _paginator(client, **kwargs):
    kwargs.setdefault("subreddit", "python")
    kwargs.setdefault("sort", "asc")
    kwargs.setdefault("page_size", 2)
    return PostPaginator(client, **kwargs)


class TestMultiPageCollection(unittest.TestCase):
    def test_collects_until_empty_page(self):
        client = _client([[_post("a", 1), _post("b", 2)], [_post("c", 3), _post("d", 4)], []])
        posts = list(_paginator(client).iter_posts())
        self.assertEqual([p["id"] for p in posts], ["a", "b", "c", "d"])
        self.assertEqual(client.search_posts.call_count, 3)

    def test_cursor_advances_with_max_timestamp(self):
        client = _client([[_post("a", 1), _post("b", 2)], []])
        list(_paginator(client, after="2019-12-30").iter_pages())
        afters = [c.kwargs["after"] for c in client.search_posts.call_args_list]
        self.assertEqual(afters, ["2019-12-30", 2])

    def test_short_final_page_stops(self):
        client = _client([[_post("a", 1), _post("b", 2)], [_post("c", 3)]])
        posts = list(_paginator(client).iter_posts())
        self.assertEqual([p["id"] for p in posts], ["a", "b", "c"])
        self.assertEqual(client.search_posts.call_count, 2)

    def test_empty_first_page_yields_nothing(self):
        client = _client([[]])
        self.assertEqual(list(_paginator(client).iter_posts()), [])
        self.assertEqual(client.search_posts.call_count, 1)

    def test_stops_at_max_posts(self):
        client = _client([[_post("a", 1), _post("b", 2)], [_post("c", 3), _post("d", 4)]])
        posts = list(_paginator(client, max_posts=3).iter_posts())
        self.assertEqual([p["id"] for p in posts], ["a", "b", "c"])
        self.assertEqual(client.search_posts.call_count, 2)

    def test_desc_direction_uses_before_cursor(self):
        client = _client([[_post("d", 4), _post("c", 3)], [_post("b", 2), _post("a", 1)], []])
        posts = list(_paginator(client, sort="desc").iter_posts())
        self.assertEqual([p["id"] for p in posts], ["d", "c", "b", "a"])
        befores = [c.kwargs["before"] for c in client.search_posts.call_args_list]
        self.assertEqual(befores, [None, 3, 1])


class TestDedup(unittest.TestCase):
    def test_overlapping_ids_returned_once(self):
        client = _client([
            [_post("a", 1), _post("b", 2)],
            [_post("b", 2), _post("c", 3)],
            [],
        ])
        posts = list(_paginator(client).iter_posts())
        self.assertEqual([p["id"] for p in posts], ["a", "b", "c"])

    def test_full_duplicate_page_steps_past_and_terminates(self):
        page = [_post("a", 5), _post("b", 5)]
        client = _client([list(page), list(page), []])
        posts = list(_paginator(client).iter_posts())
        self.assertEqual([p["id"] for p in posts], ["a", "b"])
        afters = [c.kwargs["after"] for c in client.search_posts.call_args_list]
        self.assertEqual(afters, [None, 5, 6])
        self.assertEqual(client.search_posts.call_count, 3)


class TestResume(unittest.TestCase):
    def test_state_resumes_without_duplicates(self):
        first = [_post("a", 1), _post("b", 2)]
        rest = [_post("c", 3), _post("d", 4)]

        client_a = _client([first, rest, []])
        paginator_a = _paginator(client_a)
        pages = paginator_a.iter_pages()
        self.assertEqual([p["id"] for p in next(pages)], ["a", "b"])
        saved = paginator_a.state()
        self.assertEqual(saved["cursor"], 2)
        self.assertEqual(saved["total_fetched"], 2)

        client_b = _client([rest, []])
        paginator_b = _paginator(client_b, resume=saved)
        remaining = list(paginator_b.iter_posts())
        self.assertEqual([p["id"] for p in remaining], ["c", "d"])
        first_after = client_b.search_posts.call_args_list[0].kwargs["after"]
        self.assertEqual(first_after, 2)

    def test_resume_honours_original_max_posts(self):
        client_a = _client([[_post("a", 1), _post("b", 2)]])
        paginator_a = _paginator(client_a, max_posts=3)
        list(paginator_a.iter_pages())
        saved = paginator_a.state()

        client_b = _client([[_post("c", 3), _post("d", 4)]])
        paginator_b = _paginator(client_b, max_posts=3, resume=saved)
        self.assertEqual([p["id"] for p in paginator_b.iter_posts()], ["c"])

    def test_resume_rejects_mismatched_filters(self):
        client = _client([[_post("a", 1)]])
        saved = _paginator(client, subreddit="python").state()
        with self.assertRaises(ValueError):
            _paginator(MagicMock(), subreddit="rust", resume=saved)
        with self.assertRaises(ValueError):
            _paginator(MagicMock(), subreddit="python", sort="desc", resume=saved)

    def test_reconcile_stored_unions_ids_and_bumps_total(self):
        paginator = _paginator(_client([]))
        self.assertEqual(paginator.reconcile_stored(["a", "b"]), 2)
        self.assertEqual(paginator.total_fetched, 2)
        self.assertEqual(paginator.reconcile_stored(["b", "c", None, "  "]), 1)
        self.assertEqual(paginator.total_fetched, 3)
        self.assertEqual(paginator.state()["seen_ids"], ["a", "b", "c"])


class TestValidationAndLogging(unittest.TestCase):
    def test_rejects_bad_arguments(self):
        client = MagicMock()
        with self.assertRaises(ValueError):
            PostPaginator(client, sort="new")
        with self.assertRaises(ValueError):
            PostPaginator(client, page_size=101)
        with self.assertRaises(ValueError):
            PostPaginator(client, max_posts=0)

    def test_logs_progress(self):
        client = _client([[_post("a", 1), _post("b", 2)], []])
        with self.assertLogs("reddit_collector.paginator", level="INFO") as logs:
            list(_paginator(client).iter_posts())
        output = "\n".join(logs.output)
        self.assertIn("2 new posts", output)
        self.assertIn("no more results", output)

    def test_from_config_reads_limit_and_max(self):
        client = MagicMock()
        client.search_posts.return_value = {"data": []}
        config = {
            "subreddit": "python", "title": None, "query": None, "selftext": None,
            "author": None, "after": "2020-01-01", "before": None,
            "sort": "asc", "limit": 50, "max_posts": 10,
        }
        paginator = PostPaginator.from_config(config, client)
        list(paginator.iter_posts())
        _, kwargs = client.search_posts.call_args
        self.assertEqual(kwargs["limit"], 50)
        self.assertEqual(kwargs["subreddit"], "python")
        self.assertEqual(paginator.max_posts, 10)


if __name__ == "__main__":
    unittest.main()
