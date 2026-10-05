"""Tests for the thin CLI: subcommands, exit codes, safe output. No real HTTP."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from reddit_collector.__main__ import build_parser, main, redact, summarize_config
from reddit_collector.client import ArcticShiftAPIError
from reddit_collector.collector import CollectorError


def _write_config(tmp, extra=""):
    cfg = Path(tmp) / "config.yaml"
    cfg.write_text(
        "subreddit: python\n"
        "output:\n"
        f"  raw_dir: {tmp}/raw\n"
        f"  processed_dir: {tmp}/processed\n"
        f"  checkpoint_dir: {tmp}/checkpoints\n"
        "logging:\n"
        f"  file: {tmp}/run.log\n" + extra,
        encoding="utf-8",
    )
    return str(cfg)


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class TestHelp(unittest.TestCase):
    def test_top_level_help(self):
        code, out, _ = _run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("collect", out)

    def test_subcommand_help(self):
        for cmd in ("collect", "resume", "validate", "status"):
            with self.subTest(cmd=cmd):
                code, out, _ = _run([cmd, "--help"])
                self.assertEqual(code, 0)
                self.assertIn("--config", out)

    def test_no_command_exits_2(self):
        code, _, err = _run([])
        self.assertEqual(code, 2)
        self.assertTrue(err)


class TestValidate(unittest.TestCase):
    def test_valid_config_exits_0(self):
        with TemporaryDirectory() as tmp:
            code, out, _ = _run(["validate", "--config", _write_config(tmp)])
            self.assertEqual(code, 0)
            self.assertIn("Configuration OK", out)
            self.assertIn("python", out)

    def test_invalid_config_exits_2(self):
        with TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "bad.yaml"
            cfg.write_text("subreddit: ''\nlimit: 0\n", encoding="utf-8")
            code, _, err = _run(["validate", "--config", str(cfg)])
            self.assertEqual(code, 2)
            self.assertIn("Configuration error", err)


class TestSafeOutput(unittest.TestCase):
    def test_redact_hides_credential_like_values(self):
        cleaned = redact({"api_key": "abc", "nested": {"token": "t", "name": "x"}})
        self.assertEqual(cleaned, {"api_key": "***REDACTED***",
                                   "nested": {"token": "***REDACTED***", "name": "x"}})
        self.assertEqual(redact("plain", "subreddit"), "plain")

    def test_summary_never_carries_unknown_keys(self):
        summary = summarize_config({"subreddit": "python", "api_key": "abc",
                                    "collection": {}, "output": {}})
        self.assertNotIn("api_key", summary)


class TestCollectResumeStatus(unittest.TestCase):
    def _summary(self):
        return {"run_id": "r1", "pages": 1, "posts_this_run": 2, "total_posts": 2}

    def test_collect_success(self):
        with TemporaryDirectory() as tmp:
            with patch("reddit_collector.__main__.collect_new",
                       return_value=self._summary()) as collect:
                code, out, _ = _run(["collect", "--config", _write_config(tmp)])
            self.assertEqual(code, 0)
            self.assertIn("r1", out)
            self.assertIn("2 new posts", out)
            collect.assert_called_once()

    def test_collect_refusal_exits_2(self):
        with TemporaryDirectory() as tmp:
            with patch("reddit_collector.__main__.collect_new",
                       side_effect=CollectorError("use 'resume'")):
                code, _, err = _run(["collect", "--config", _write_config(tmp)])
            self.assertEqual(code, 2)
            self.assertIn("resume", err)

    def test_collect_api_failure_exits_1(self):
        with TemporaryDirectory() as tmp:
            with patch("reddit_collector.__main__.collect_new",
                       side_effect=ArcticShiftAPIError("boom", status_code=500)):
                code, _, err = _run(["collect", "--config", _write_config(tmp)])
            self.assertEqual(code, 1)
            self.assertIn("Collection failed", err)

    def test_collect_corrupt_checkpoint_exits_2_cleanly(self):
        with TemporaryDirectory() as tmp:
            cfg_path = _write_config(tmp)
            Path(f"{tmp}/checkpoints").mkdir(parents=True, exist_ok=True)
            Path(f"{tmp}/checkpoints/checkpoint.json").write_text("{broken", encoding="utf-8")
            code, _, err = _run(["collect", "--config", cfg_path])
            self.assertEqual(code, 2)
            self.assertIn("--fresh", err)
            self.assertNotIn("Traceback", err)

    def test_resume_missing_checkpoint_exits_2(self):
        with TemporaryDirectory() as tmp:
            with patch("reddit_collector.__main__.resume_collection",
                       side_effect=CollectorError("nothing to resume")):
                code, _, err = _run(["resume", "--config", _write_config(tmp)])
            self.assertEqual(code, 2)
            self.assertIn("Resume error", err)

    def test_status_empty(self):
        with TemporaryDirectory() as tmp:
            code, out, _ = _run(["status", "--config", _write_config(tmp)])
            self.assertEqual(code, 0)
            self.assertIn("Checkpoint: none", out)
            self.assertIn("Stored runs: none", out)

    def test_status_shows_checkpoint_and_runs(self):
        from reddit_collector.checkpoint import save_checkpoint
        from reddit_collector.storage import RunStore

        with TemporaryDirectory() as tmp:
            cfg_path = _write_config(tmp)
            store = RunStore(f"{tmp}/raw", f"{tmp}/processed", run_id="run1")
            store.append_raw([{"id": "a"}])
            from reddit_collector.paginator import PostPaginator
            from unittest.mock import MagicMock

            client = MagicMock()
            client.search_posts.return_value = {"data": [{"id": "a", "created_utc": 1}]}
            paginator = PostPaginator(client, subreddit="python", page_size=2)
            list(paginator.iter_pages())
            save_checkpoint(f"{tmp}/checkpoints", paginator.state(), run_id="run1")

            code, out, _ = _run(["status", "--config", cfg_path])
            self.assertEqual(code, 0)
            self.assertIn("run1", out)
            self.assertIn("1 posts", out)

    def test_parser_prog_name(self):
        self.assertEqual(build_parser().prog, "reddit-collector")

    def test_gui_without_tkinter_exits_2_with_hint(self):
        import reddit_collector.gui as gui_module

        if gui_module.tk is not None:
            self.skipTest("tkinter is installed; headless fallback not testable")
        code, _, _ = _run(["gui"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
