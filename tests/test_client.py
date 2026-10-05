"""Basic unit tests for ArcticShiftClient. No real API calls: HTTP is mocked."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from reddit_collector.client import (
    DEFAULT_BASE_URL,
    ArcticShiftAPIError,
    ArcticShiftClient,
    ArcticShiftNetworkError,
    ArcticShiftQueryTimeoutError,
    ArcticShiftRateLimitError,
)


def _mock_response(status=200, json_data=None, headers=None, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = text
    if isinstance(json_data, Exception):
        resp.json.side_effect = json_data
    else:
        resp.json.return_value = json_data
    return resp


def _client_with(response=None, side_effect=None):
    session = MagicMock()
    if side_effect is not None:
        session.get.side_effect = side_effect
    else:
        session.get.return_value = response
    return ArcticShiftClient(session=session), session


class TestSearchPostsParams(unittest.TestCase):
    def test_builds_url_and_params(self):
        client, session = _client_with(_mock_response(200, {"data": []}))
        result = client.search_posts(
            subreddit="worldnews", title="wuhan",
            after="2019-12-30", before="2020-01-31",
            sort="asc", limit=10,
        )
        self.assertEqual(result, {"data": []})
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], DEFAULT_BASE_URL + "/api/posts/search")
        self.assertEqual(kwargs["params"], {
            "subreddit": "worldnews", "title": "wuhan",
            "after": "2019-12-30", "before": "2020-01-31",
            "sort": "asc", "limit": "10",
        })
        self.assertEqual(kwargs["timeout"], 30)

    def test_omits_none_params(self):
        client, session = _client_with(_mock_response(200, {"data": []}))
        client.search_posts(subreddit="python")
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"], {"subreddit": "python", "limit": "25"})

    def test_strips_r_prefix_and_uses_custom_base_url(self):
        client, session = _client_with(_mock_response(200, {"data": []}))
        self.assertTrue(client.base_url == DEFAULT_BASE_URL)
        custom, custom_session = _client_with(_mock_response(200, {"data": []}))
        custom = ArcticShiftClient(base_url="https://example.test/", session=custom_session)
        custom.search_posts(subreddit="r/python")
        args, kwargs = custom_session.get.call_args
        self.assertEqual(args[0], "https://example.test/api/posts/search")
        self.assertEqual(kwargs["params"]["subreddit"], "python")

    def test_author_strips_literal_u_prefix_only(self):
        client, session = _client_with(_mock_response(200, {"data": []}))
        client.search_posts(author="u/spez")
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["author"], "spez")

        client.search_posts(author="ursula")
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["author"], "ursula")

        client.search_posts(author="united")
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["author"], "united")

    def test_rejects_bad_limit_and_sort(self):
        client, _ = _client_with(_mock_response(200, {"data": []}))
        for bad in (0, 101, "10", True, None):
            with self.assertRaises(ValueError):
                client.search_posts(subreddit="x", limit=bad)
        with self.assertRaises(ValueError):
            client.search_posts(subreddit="x", sort="new")

    def test_from_config_uses_configured_url_and_timeout(self):
        session = MagicMock()
        session.get.return_value = _mock_response(200, {"data": []})
        cfg = {"api": {"base_url": "https://custom.test"},
               "collection": {"timeout_secs": 7}}
        client = ArcticShiftClient.from_config(cfg, session=session)
        self.assertEqual(client.base_url, "https://custom.test")
        self.assertEqual(client.timeout_secs, 7)

    def test_no_proxy_or_retry_configured(self):
        # Client must not set proxies or perform hidden retries: exactly one GET.
        client, session = _client_with(_mock_response(200, {"data": []}))
        client.search_posts(subreddit="python")
        self.assertEqual(session.get.call_count, 1)
        _, kwargs = session.get.call_args
        self.assertNotIn("proxies", kwargs)


class TestSearchPostsErrors(unittest.TestCase):
    def test_rate_limit_429_exposes_reset_header(self):
        resp = _mock_response(429, {"error": "slow down"}, text="slow down",
                              headers={"X-RateLimit-Reset": "42",
                                       "X-RateLimit-Reset-At": "2026-01-01T00:00:42Z"})
        client, _ = _client_with(resp)
        with self.assertRaises(ArcticShiftRateLimitError) as ctx:
            client.search_posts(subreddit="x")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.retry_after_secs, 42.0)
        self.assertEqual(ctx.exception.reset_at, "2026-01-01T00:00:42Z")

    def test_query_timeout_message(self):
        client, _ = _client_with(_mock_response(200, {"error": "Query timed out"}))
        with self.assertRaises(ArcticShiftQueryTimeoutError):
            client.search_posts(subreddit="x", title="common word")

    def test_single_shot_422_timeout_raises_query_timeout(self):
        payload = {"data": None, "error": "Timeout. Maybe slow down a bit"}
        client, session = _client_with(_mock_response(422, payload, text="timeout"))
        with self.assertRaises(ArcticShiftQueryTimeoutError):
            client.search_posts(subreddit="x")
        self.assertEqual(session.get.call_count, 1)

    def test_http_500_raises_api_error(self):
        client, _ = _client_with(_mock_response(500, None, text="boom"))
        with self.assertRaises(ArcticShiftAPIError) as ctx:
            client.search_posts(subreddit="x")
        self.assertEqual(ctx.exception.status_code, 500)

    def test_non_json_raises_api_error(self):
        client, _ = _client_with(_mock_response(200, ValueError("bad json"), text="<html>"))
        with self.assertRaises(ArcticShiftAPIError):
            client.search_posts(subreddit="x")

    def test_connection_error_wrapped(self):
        import requests

        client, _ = _client_with(
            side_effect=requests.ConnectionError("dns down"))
        with self.assertRaises(ArcticShiftNetworkError):
            client.search_posts(subreddit="x")


if __name__ == "__main__":
    unittest.main()
