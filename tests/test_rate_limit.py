"""Tests for RateLimiter and its integration with ArcticShiftClient.

No real API calls or real sleeping: time is faked via injected clock/sleep.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from reddit_collector.client import ArcticShiftAPIError, ArcticShiftClient, ArcticShiftRateLimitError
from reddit_collector.rate_limit import RateLimiter, parse_retry_after_secs


class FakeClock:
    """Injectable clock: sleep() advances virtual time and records waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs


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


def _client(responses, clock, **limiter_kwargs):
    session = MagicMock()
    session.get.side_effect = list(responses)
    limiter_kwargs.setdefault("min_interval_secs", 0.001)  # negligible spacing; retry waits dominate
    limiter_kwargs.setdefault("sleep_fn", clock.sleep)
    limiter_kwargs.setdefault("clock_fn", clock.monotonic)
    limiter = RateLimiter(**limiter_kwargs)
    client = ArcticShiftClient(session=session, rate_limiter=limiter)
    return client, session, limiter


class TestNormalSpacing(unittest.TestCase):
    def test_second_request_waits_for_interval(self):
        clock = FakeClock()
        limiter = RateLimiter(min_interval_secs=2.0, sleep_fn=clock.sleep, clock_fn=clock.monotonic)
        self.assertEqual(limiter.wait_for_turn(), 0.0)  # first request goes immediately
        self.assertEqual(limiter.wait_for_turn(), 2.0)  # second waits the full interval
        self.assertEqual(clock.sleeps, [2.0])

    def test_no_wait_once_interval_elapsed(self):
        clock = FakeClock()
        limiter = RateLimiter(min_interval_secs=1.0, sleep_fn=clock.sleep, clock_fn=clock.monotonic)
        limiter.wait_for_turn()
        clock.now += 5.0  # time passes without requests
        self.assertEqual(limiter.wait_for_turn(), 0.0)
        self.assertEqual(clock.sleeps, [])

    def test_interval_configurable_from_throttle_qps(self):
        limiter = RateLimiter.from_config({"collection": {"throttle_qps": 0.5}})
        self.assertAlmostEqual(limiter.min_interval_secs, 2.0)
        self.assertAlmostEqual(limiter.throttle_qps, 0.5)


class TestBackoffComputation(unittest.TestCase):
    def test_exponential_growth_and_cap(self):
        limiter = RateLimiter(backoff_base_secs=1.0, backoff_max_secs=60.0)
        self.assertEqual([limiter.backoff_for_attempt(n) for n in range(5)], [1.0, 2.0, 4.0, 8.0, 16.0])
        self.assertEqual(limiter.backoff_for_attempt(20), 60.0)  # capped

    def test_server_wait_wins_if_larger(self):
        limiter = RateLimiter(backoff_base_secs=1.0, backoff_max_secs=60.0)
        self.assertEqual(limiter.backoff_for_attempt(0, retry_after_secs=42.0), 42.0)
        # Server wait is never capped down by backoff_max_secs.
        self.assertEqual(limiter.backoff_for_attempt(0, retry_after_secs=300.0), 300.0)


class TestParseRetryAfter(unittest.TestCase):
    def test_standard_retry_after_seconds(self):
        self.assertEqual(parse_retry_after_secs({"Retry-After": "5"}), 5.0)

    def test_arctic_shift_reset_header(self):
        self.assertEqual(parse_retry_after_secs({"X-RateLimit-Reset": "12"}), 12.0)

    def test_retry_after_takes_precedence(self):
        headers = {"Retry-After": "5", "X-RateLimit-Reset": "50"}
        self.assertEqual(parse_retry_after_secs(headers), 5.0)

    def test_missing_or_garbage_returns_none(self):
        self.assertIsNone(parse_retry_after_secs({}))
        self.assertIsNone(parse_retry_after_secs({"X-RateLimit-Reset": "soon"}))


class TestClientRetryIntegration(unittest.TestCase):
    def test_429_then_success(self):
        clock = FakeClock()
        client, session, _ = _client(
            [_mock_response(429, text="slow", headers={"X-RateLimit-Reset": "3"}),
             _mock_response(200, {"data": [{"id": "a"}]})],
            clock, max_retries=3, backoff_base_secs=1.0,
        )
        result = client.search_posts(subreddit="python")
        self.assertEqual(result, {"data": [{"id": "a"}]})
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(clock.sleeps, [3.0])  # max(server 3s, backoff 1s)

    def test_retry_after_header_respected(self):
        clock = FakeClock()
        client, session, _ = _client(
            [_mock_response(429, text="slow", headers={"Retry-After": "5"}),
             _mock_response(200, {"data": []})],
            clock, max_retries=3, backoff_base_secs=1.0,
        )
        client.search_posts(subreddit="python")
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(clock.sleeps, [5.0])

    def test_5xx_uses_exponential_backoff(self):
        clock = FakeClock()
        client, session, _ = _client(
            [_mock_response(500, text="boom"),
             _mock_response(503, text="still boom"),
             _mock_response(200, {"data": []})],
            clock, max_retries=3, backoff_base_secs=1.0, backoff_max_secs=60.0,
        )
        client.search_posts(subreddit="python")
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(clock.sleeps, [1.0, 2.0])

    def test_gives_up_after_max_retries(self):
        clock = FakeClock()
        client, session, _ = _client(
            [_mock_response(500, text="boom")] * 5, clock, max_retries=2,
        )
        with self.assertRaises(ArcticShiftAPIError):
            client.search_posts(subreddit="python")
        self.assertEqual(session.get.call_count, 3)  # 1 initial + 2 retries

    def test_persistent_429_raises_with_reset_info(self):
        clock = FakeClock()
        client, session, _ = _client(
            [_mock_response(429, text="slow", headers={"X-RateLimit-Reset": "9"})] * 4,
            clock, max_retries=2,
        )
        with self.assertRaises(ArcticShiftRateLimitError) as ctx:
            client.search_posts(subreddit="python")
        self.assertEqual(ctx.exception.retry_after_secs, 9.0)
        self.assertEqual(session.get.call_count, 3)

    def test_4xx_other_than_429_not_retried(self):
        clock = FakeClock()
        client, session, _ = _client([_mock_response(400, text="bad")], clock, max_retries=3)
        with self.assertRaises(ArcticShiftAPIError):
            client.search_posts(subreddit="python")
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(clock.sleeps, [])

    def test_422_timeout_retried_with_pure_backoff(self):
        from reddit_collector.client import ArcticShiftQueryTimeoutError

        clock = FakeClock()
        live_payload = {"data": None, "error": "Timeout. Maybe slow down a bit"}
        client, session, _ = _client(
            [_mock_response(422, live_payload, text="timeout",
                             headers={"X-RateLimit-Reset": "42"}),
             _mock_response(200, {"data": []})],
            clock, max_retries=3, backoff_base_secs=1.0,
        )
        self.assertEqual(client.search_posts(subreddit="python"), {"data": []})
        self.assertEqual(session.get.call_count, 2)
        # Pure exponential backoff: the reset header rides along on every
        # response, so it is informational here, not a 42s wait demand.
        self.assertEqual(clock.sleeps, [1.0])

    def test_422_non_transient_not_retried(self):
        clock = FakeClock()
        client, session, _ = _client(
            [_mock_response(422, {"error": "invalid parameter: foo"}, text="bad")],
            clock, max_retries=3,
        )
        with self.assertRaises(ArcticShiftAPIError):
            client.search_posts(subreddit="python")
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(clock.sleeps, [])

    def test_persistent_422_timeout_raises_query_timeout(self):
        from reddit_collector.client import ArcticShiftQueryTimeoutError

        clock = FakeClock()
        payload = {"data": None, "error": "Timeout. Maybe slow down a bit"}
        client, session, _ = _client(
            [_mock_response(422, payload, text="timeout")] * 4,
            clock, max_retries=2, backoff_base_secs=1.0,
        )
        with self.assertRaises(ArcticShiftQueryTimeoutError):
            client.search_posts(subreddit="python")
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(clock.sleeps, [1.0, 2.0])

    def test_200_slow_down_message_is_query_timeout(self):
        from reddit_collector.client import ArcticShiftQueryTimeoutError

        clock = FakeClock()
        client, _, _ = _client(
            [_mock_response(200, {"error": "Please slow down a bit"})], clock, max_retries=3
        )
        with self.assertRaises(ArcticShiftQueryTimeoutError):
            client.search_posts(subreddit="python")

    def test_logs_when_waiting(self):
        clock = FakeClock()
        client, _, _ = _client(
            [_mock_response(500, text="boom"), _mock_response(200, {"data": []})],
            clock, max_retries=1,
        )
        with self.assertLogs(level="WARNING") as logs:
            client.search_posts(subreddit="python")
        self.assertTrue(any("Waiting" in msg for msg in logs.output))


if __name__ == "__main__":
    unittest.main()
