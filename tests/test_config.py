"""Tests for YAML config loading and validation. No real API calls."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from reddit_collector.__main__ import main
from reddit_collector.config import ConfigError, load_config


MINIMAL = "subreddit: Philippines\n"


def _write(tmp, content, name="config.yaml"):
    path = Path(tmp) / name
    path.write_text(content, encoding="utf-8")
    return str(path)


class TestValidConfigs(unittest.TestCase):
    def test_minimal_gets_defaults(self):
        with TemporaryDirectory() as tmp:
            config = load_config(_write(tmp, MINIMAL))
            self.assertEqual(config["subreddit"], "Philippines")
            self.assertIsNone(config["after"])
            self.assertIsNone(config["max_posts"])
            self.assertEqual(config["limit"], 25)
            self.assertEqual(config["collection"]["throttle_qps"], 1.0)
            self.assertEqual(config["output"]["raw_dir"], "data/raw")

    def test_repo_examples_load(self):
        for name in ("config.example.yaml", "config.philippines.example.yaml"):
            with self.subTest(name=name):
                config = load_config(name)
                self.assertTrue(config["subreddit"].strip())

    def test_philippines_example_targets_philippines(self):
        config = load_config("config.philippines.example.yaml")
        self.assertEqual(config["subreddit"], "Philippines")
        self.assertEqual(config["sort"], "asc")

    def test_env_expansion(self):
        with TemporaryDirectory() as tmp:
            import os

            os.environ["RC_TEST_DIR"] = f"{tmp}/out"
            path = _write(tmp, MINIMAL + "output:\n  raw_dir: ${RC_TEST_DIR}/raw\n")
            try:
                config = load_config(path)
            finally:
                del os.environ["RC_TEST_DIR"]
            self.assertEqual(config["output"]["raw_dir"], f"{tmp}/out/raw")


class TestInvalidConfigs(unittest.TestCase):
    def _assert_rejected(self, content, fragment):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write(tmp, content))
            self.assertIn(fragment, str(ctx.exception))

    def test_missing_file(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                load_config(f"{tmp}/nope.yaml")

    def test_empty_file(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                load_config(_write(tmp, ""))

    def test_missing_subreddit(self):
        self._assert_rejected("limit: 10\n", "'subreddit'")

    def test_bad_limit(self):
        self._assert_rejected(MINIMAL + "limit: 500\n", "'limit'")

    def test_bad_sort(self):
        self._assert_rejected(MINIMAL + "sort: new\n", "'sort'")

    def test_bad_max_posts(self):
        self._assert_rejected(MINIMAL + "max_posts: 0\n", "'max_posts'")

    def test_blank_keyword_rejected(self):
        self._assert_rejected(MINIMAL + "title: '   '\n", "'title'")

    def test_blank_date_rejected(self):
        self._assert_rejected(MINIMAL + "after: '  '\n", "'after'")

    def test_unquoted_yaml_date_coerced_to_iso(self):
        with TemporaryDirectory() as tmp:
            config = load_config(_write(tmp, MINIMAL + "after: 2019-12-30\n"))
            self.assertEqual(config["after"], "2019-12-30")

    def test_unquoted_yaml_datetime_coerced_to_iso(self):
        with TemporaryDirectory() as tmp:
            config = load_config(_write(tmp, MINIMAL + "after: 2019-12-30 10:00:00\n"))
            self.assertEqual(config["after"], "2019-12-30T10:00:00")

    def test_numeric_epoch_accepted(self):
        with TemporaryDirectory() as tmp:
            config = load_config(_write(tmp, MINIMAL + "after: 1577836800\nbefore: 1577923200.5\n"))
            self.assertEqual(config["after"], 1577836800)
            self.assertEqual(config["before"], 1577923200.5)

    def test_bool_date_rejected(self):
        self._assert_rejected(MINIMAL + "after: true\n", "'after'")
    def test_bad_throttle(self):
        self._assert_rejected(
            MINIMAL + "collection: {throttle_qps: -1}\n", "'collection.throttle_qps'"
        )

    def test_bad_output_dir(self):
        self._assert_rejected(
            MINIMAL + "output: {raw_dir: ''}\n", "'output.raw_dir'"
        )

    def test_multiple_errors_all_reported(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write(tmp, "subreddit: ''\nlimit: 0\nsort: bad\n"))
            message = str(ctx.exception)
            self.assertIn("'subreddit'", message)
            self.assertIn("'limit'", message)
            self.assertIn("'sort'", message)


class TestInvalidConfigNeverCollects(unittest.TestCase):
    def test_cli_exits_2_with_clear_message(self):
        with TemporaryDirectory() as tmp:
            path = _write(tmp, "subreddit: ''\nlimit: 0\n")
            import io
            from contextlib import redirect_stderr

            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["validate", "--config", path])
            self.assertEqual(code, 2)
            self.assertIn("Configuration error", err.getvalue())
            self.assertIn("'subreddit'", err.getvalue())


if __name__ == "__main__":
    unittest.main()
