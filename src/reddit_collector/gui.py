"""Local desktop GUI (tkinter) on top of the existing collector.

Frontend only: every collection runs through `collector.collect_new` /
`resume_collection`, i.e. the same API client, rate limiter, paginator,
checkpoint system, and storage the CLI uses. No collection logic lives here.

- Collection runs in a background thread; the UI stays responsive and is
  updated from the main thread via a log-record queue (`after()` polling).
- Progress/rate-limit/error counters derive from the existing log stream —
  no collector changes were needed for observability.
- Stop is cooperative: a `threading.Event` wired to the collector's
  `should_stop` hook pauses cleanly after the current finished page.
- Requires the system package `python3-tk` (`sudo apt install -y python3-tk`).

Launch: `PYTHONPATH=src python3 -m reddit_collector gui [--config <file>]`
or `PYTHONPATH=src python3 -m reddit_collector.gui`.
"""

from __future__ import annotations

import logging
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - environment without python3-tk
    tk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

import yaml

from .checkpoint import (
    CheckpointError,
    list_collections,
    load_checkpoint_doc,
    validate_collection_compatible,
)
from .client import ArcticShiftError
from .collector import collect_comments, collect_new, resume_collection, CollectorError
from .config import DEFAULTS, ConfigError, load_config
from .logging_setup import setup_logging
from .storage import StorageError

logger = logging.getLogger(__name__)

GUI_CONFIG_PATH = "data/gui/last-run.yaml"


# -- non-visual helpers (importable and testable without tkinter) ---------

def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_config_dict(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble a full collector config dict from GUI form inputs.

    inputs keys: subreddit, keywords (-> query), title_kw, selftext_kw,
    after, before, max_posts (str/int), raw_dir, processed_dir,
    checkpoint_dir, log_file. Empty strings become None (i.e. no filter).
    Raises ValueError with a user-friendly message for bad numbers.
    """
    raw_max = inputs.get("max_posts")
    max_posts: int | None = None
    if _clean_text(raw_max) is not None:
        try:
            max_posts = int(str(raw_max).strip().replace(",", ""))
        except (TypeError, ValueError):
            raise ValueError(f"Target posts must be a whole number, got {raw_max!r}")
        if max_posts < 1:
            raise ValueError(f"Target posts must be at least 1, got {max_posts}")

    output = dict(DEFAULTS["output"])
    for key, field in (("raw_dir", "raw_dir"), ("processed_dir", "processed_dir"),
                       ("checkpoint_dir", "checkpoint_dir")):
        value = _clean_text(inputs.get(field))
        if value is not None:
            output[key] = value

    config = {
        "api": dict(DEFAULTS["api"]),
        "subreddit": _clean_text(inputs.get("subreddit")),
        "title": _clean_text(inputs.get("title_kw")),
        "query": _clean_text(inputs.get("keywords")),
        "selftext": _clean_text(inputs.get("selftext_kw")),
        "author": None,
        "after": _clean_text(inputs.get("after")),
        "before": _clean_text(inputs.get("before")),
        "limit": 100,
        "sort": "asc",
        "max_posts": max_posts,
        "output": output,
        "collection": dict(DEFAULTS["collection"]),
        "comments": dict(DEFAULTS["comments"]),
        "logging": {"level": "INFO", "file": _clean_text(inputs.get("log_file")) or "logs/gui.log"},
    }
    return config


def write_gui_config(inputs: Mapping[str, Any], path: str | Path = GUI_CONFIG_PATH) -> Path:
    """Validate GUI inputs, write them as YAML, reload through load_config.

    Reloading reuses the project's full validation: ConfigError/ValueError
    surface before any thread starts. Returns the validated config path.
    """
    config = build_config_dict(inputs)  # raises ValueError on bad numbers
    cfg_path = Path(path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    load_config(cfg_path)  # raises ConfigError if anything is invalid
    return cfg_path


def run_collection_flow(config: dict[str, Any], *, fresh: bool = False, resume: bool = False,
                        with_comments: bool = True, should_stop=None,
                        checkpoint: str | None = None) -> dict[str, Any]:
    """Run Phase 1 (posts) then, optionally, Phase 2 (comments) in one go.

    This is the exact path the GUI worker uses (and is unit-testable without
    tkinter). Returns {"post": <post summary>, "comments": <summary|None>}.
    When stopped during Phase 1, Phase 2 is skipped; resume later to continue.
    ``checkpoint`` selects an explicit named checkpoint (must match the config).
    """
    if resume:
        post_summary = resume_collection(config, should_stop=should_stop, checkpoint=checkpoint)
    else:
        post_summary = collect_new(config, fresh=fresh, should_stop=should_stop,
                                   checkpoint=checkpoint)
    comment_summary = None
    stop_requested = should_stop is not None and should_stop()
    if with_comments and not post_summary.get("stopped") and not stop_requested:
        logger.info("Phase 1 complete: starting comment collection for run %s",
                    post_summary["run_id"])
        comment_summary = collect_comments(config, run_id=post_summary["run_id"],
                                           should_stop=should_stop,
                                           checkpoint=checkpoint)
    return {"post": post_summary, "comments": comment_summary}


def prefill_inputs(config: Mapping[str, Any]) -> dict[str, Any]:
    """Map a loaded config back onto GUI form fields (for --config prefill)."""
    output = config.get("output", {})
    logging_cfg = config.get("logging", {})
    get = (lambda m, k: m.get(k) if isinstance(m, Mapping) else None)
    return {
        "subreddit": get(config, "subreddit") or "",
        "keywords": get(config, "query") or "",
        "title_kw": get(config, "title") or "",
        "selftext_kw": get(config, "selftext") or "",
        "after": get(config, "after") or "",
        "before": get(config, "before") or "",
        "max_posts": "" if get(config, "max_posts") is None else str(get(config, "max_posts")),
        "raw_dir": get(output, "raw_dir") or "",
        "processed_dir": get(output, "processed_dir") or "",
        "checkpoint_dir": get(output, "checkpoint_dir") or "",
        "log_file": get(logging_cfg, "file") or "",
    }


# -- checkpoint dropdown helpers (importable and testable without tkinter) --

def list_checkpoint_options(checkpoint_root: str | Path) -> list[dict[str, Any]]:
    """Summarize saved checkpoints for the GUI dropdown (never raises)."""
    try:
        entries = list_collections(checkpoint_root)
    except Exception as exc:
        logger.warning("Could not list checkpoints in %s: %s", checkpoint_root, exc)
        return []
    options: list[dict[str, Any]] = []
    for entry in entries:
        options.append({
            "name": entry.get("name", "?"),
            "label": format_checkpoint_option(entry),
            "status": _option_status(entry),
            "saved_at": entry.get("saved_at"),
            "run_id": entry.get("run_id"),
            "subreddit": ((entry.get("posts") or {}).get("subreddit")
                          if isinstance(entry.get("posts"), Mapping) else None),
            "legacy": bool(entry.get("legacy")),
            "has_error": bool(entry.get("error") or entry.get("comments_error")),
        })
    return options


def _option_status(entry: Mapping[str, Any]) -> str:
    if entry.get("error"):
        return "error"
    posts = entry.get("posts")
    if isinstance(posts, Mapping) and posts.get("present"):
        return str(posts.get("status", "in_progress"))
    if entry.get("has_comments"):
        return "comments_only"
    return "empty"


def format_checkpoint_option(entry: Mapping[str, Any]) -> str:
    """Human-readable dropdown label: subreddit + date range + status + time."""
    name = str(entry.get("name", "?"))
    if entry.get("error"):
        return f"{name} (error — see log)"
    if entry.get("legacy"):
        posts = entry.get("posts") or {}
        total = posts.get("total_fetched", "?") if isinstance(posts, Mapping) else "?"
        return f"legacy — shared checkpoint ({total} posts; migrate explicitly)"
    posts = entry.get("posts") if isinstance(entry.get("posts"), Mapping) else None
    status = posts.get("status", "unknown") if posts else "comments_only"
    total = posts.get("total_fetched", "?") if posts else "?"
    saved = entry.get("saved_at") or "unknown time"
    label = f"{name} ({status}, {total} posts, saved {saved})"
    if entry.get("has_comments"):
        comments = entry.get("comments") if isinstance(entry.get("comments"), Mapping) else None
        if comments and comments.get("present"):
            label += f" [+{comments.get('comments_collected', '?')} comments]"
    return label


def describe_selection_mismatch(form_config: Mapping[str, Any], selected_name: str | None,
                                checkpoint_root: str | Path) -> str | None:
    """Validate an explicitly selected checkpoint against the form config.

    Returns an error string (shown in the GUI, logged) or None when compatible.
    Performs no network requests and writes nothing.
    """
    if not selected_name:
        return None
    from .checkpoint import checkpoint_dir_for_name

    try:
        target = checkpoint_dir_for_name(checkpoint_root, selected_name)
        doc = load_checkpoint_doc(target)
    except (CheckpointError, OSError) as exc:
        return f"Checkpoint error: {exc}"
    if doc is None:
        return (f"No checkpoint {selected_name!r} found. "
                f"Select an available checkpoint or start a new collection.")
    try:
        validate_collection_compatible(doc, form_config, selected_name=selected_name)
    except CheckpointError as exc:
        logger.warning("Checkpoint selection rejected: %s", exc)
        return str(exc)
    return None


def checkpoint_form_values(checkpoint_root: str | Path, name: str) -> dict[str, str] | None:
    """Subreddit/date values from a checkpoint's stored metadata (for populate).

    Returns None when the checkpoint is missing or unreadable.
    """
    from .checkpoint import checkpoint_dir_for_name

    try:
        doc = load_checkpoint_doc(checkpoint_dir_for_name(checkpoint_root, name))
    except (CheckpointError, OSError):
        return None
    if doc is None:
        return None
    collection = doc.get("collection") if isinstance(doc.get("collection"), dict) else None
    paginator = doc.get("paginator") if isinstance(doc.get("paginator"), dict) else {}
    filters = paginator.get("filters", {}) if isinstance(paginator, Mapping) else {}
    if collection:
        sub = str(collection.get("subreddit", ""))
        after = collection.get("after")
        before = collection.get("before")
    else:
        sub = str(filters.get("subreddit", "") or "")
        after = filters.get("after")
        before = filters.get("before")
    return {
        "subreddit": sub,
        "after": "" if after is None else str(after),
        "before": "" if before is None else str(before),
    }


class QueueLogHandler(logging.Handler):
    """Forward log records to a queue for the GUI thread to drain."""

    def __init__(self, target: "queue.Queue[logging.LogRecord]") -> None:
        super().__init__(level=logging.DEBUG)
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.target.put_nowait(record)
        except queue.Full:
            pass


_PAGE_RE = re.compile(r"Page\s+(\d+):\s+(\d+)\s+new posts")


class StatsTracker:
    """Derive progress counters from the existing log stream (no I/O)."""

    def __init__(self, target_posts: int | None = None) -> None:
        self.target_posts = target_posts
        self.reset()

    def reset(self, target_posts: int | None = None) -> None:
        if target_posts is not None:
            self.target_posts = target_posts
        self.pages = 0
        self.posts = 0
        self.retry_waits = 0
        self.errors = 0
        self.last_message = ""
        self.waiting_on_rate_limit = False
        self.comment_posts_done = 0
        self.comments_collected = 0
        self.in_comment_phase = False

    def update(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            return
        self.last_message = message
        name = record.name or ""
        if record.levelno >= logging.ERROR:
            self.errors += 1
        if "before retry" in message and record.levelno == logging.WARNING:
            self.retry_waits += 1
            self.waiting_on_rate_limit = True
        elif name.endswith(".paginator") and "new posts" in message:
            match = _PAGE_RE.search(message)
            if match:
                self.pages = int(match.group(1))
                total = re.search(r"\((\d+)(?:/\d+)? total\)", message)
                if total:
                    self.posts = int(total.group(1))
            self.waiting_on_rate_limit = False
        elif name.endswith(".collector"):
            # Phase-2 per-post line: "Post <id>: N comments (M posts done)"
            match = re.search(r"Post \S+: (\d+) comments \((\d+) posts done\)", message)
            if match:
                self.in_comment_phase = True
                self.comments_collected += int(match.group(1))
                self.comment_posts_done = int(match.group(2))
            self.waiting_on_rate_limit = False
        elif name.endswith(".checkpoint") and "posts done, " in message:
            # "Comment checkpoint saved: M posts done, K comments (...)"
            match = re.search(r"(\d+) posts done, (\d+) comments", message)
            if match:
                self.in_comment_phase = True
                self.comment_posts_done = int(match.group(1))
                self.comments_collected = int(match.group(2))
            self.waiting_on_rate_limit = False
        elif "Rate limiting: waiting" in message:
            self.waiting_on_rate_limit = False  # normal spacing, not a limit hit

    def snapshot(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "posts": self.posts,
            "target": self.target_posts,
            "retry_waits": self.retry_waits,
            "errors": self.errors,
            "waiting_on_rate_limit": self.waiting_on_rate_limit,
            "last_message": self.last_message,
            "in_comment_phase": self.in_comment_phase,
            "comment_posts_done": self.comment_posts_done,
            "comments_collected": self.comments_collected,
        }


def format_summary(summary: Mapping[str, Any], duration_secs: float,
                   stats: Mapping[str, Any], processed_dir: str,
                   comments: Mapping[str, Any] | None = None) -> str:
    """Multiline completion text for the results view."""
    minutes, seconds = divmod(int(duration_secs), 60)
    requests = stats.get("pages", 0) + stats.get("retry_waits", 0)
    stopped_note = " (stopped early — resume to continue)" if summary.get("stopped") else ""
    text = (
        f"Total posts collected: {summary.get('total_posts')} "
        f"({summary.get('posts_this_run')} new this run){stopped_note}\n"
        f"Duration: {minutes}m {seconds:02d}s\n"
        f"Requests: ~{requests} ({stats.get('pages', 0)} pages + "
        f"{stats.get('retry_waits', 0)} retries)\n"
        f"Rate-limit waits: {stats.get('retry_waits', 0)}\n"
        f"Errors logged: {stats.get('errors', 0)}\n"
        f"Output: {processed_dir}/{summary.get('run_id')}/posts/ (chunked JSONL, "
        f"≤ chunk_size per file)"
    )
    if comments is not None:
        text += (
            f"\nComments: {comments.get('total_comments')} collected "
            f"({comments.get('posts_completed')} posts)"
        )
    return text


# -- the tkinter application (only constructed when tkinter exists) --------

class CollectorApp:
    """Main window. All tk usage stays inside this class and main()."""

    POLL_MS = 200

    def __init__(self, prefill: Mapping[str, Any] | None = None) -> None:
        if tk is None:
            raise SystemExit(
                "tkinter is not installed. Install it with: sudo apt install -y python3-tk"
            )
        self.root = tk.Tk()
        self.root.title("Reddit Collector")
        self.root.geometry("760x640")

        self.log_queue: "queue.Queue[logging.LogRecord]" = queue.Queue(maxsize=2000)
        self.result_queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=16)
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.stats = StatsTracker()
        self.started_at = 0.0
        self.last_run_id: str | None = None
        self.last_dirs: dict[str, str] = {}
        self._handler_attached = False
        self.checkpoint_options: list[dict[str, Any]] = []
        self.selected_checkpoint: str | None = None

        self._build_form(prefill or {})
        self._build_progress()
        self._build_log_panel()
        self.refresh_checkpoint_list()
        self.root.after(self.POLL_MS, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- widget construction ------------------------------------------

    def _build_form(self, prefill: Mapping[str, Any]) -> None:
        form = ttk.LabelFrame(self.root, text="Collection settings", padding=8)
        form.pack(fill="x", padx=8, pady=(8, 4))
        self.entries: dict[str, Any] = {}

        def row(label: str, key: str, default: str = "", hint: str = "") -> None:
            frame = ttk.Frame(form)
            frame.pack(fill="x", pady=2)
            ttk.Label(frame, text=label, width=22).pack(side="left")
            entry = ttk.Entry(frame)
            entry.insert(0, str(prefill.get(key, default)))
            entry.pack(side="left", fill="x", expand=True)
            if hint:
                ttk.Label(frame, text=hint).pack(side="left", padx=(4, 0))
            self.entries[key] = entry

        row("Subreddit", "subreddit", "Philippines", "(without r/)")
        row("Keywords (title + body)", "keywords", "", 'e.g. election OR "typhoon relief"')
        row("Advanced: title only", "title_kw")
        row("Advanced: body only", "selftext_kw")
        row("Start date", "after", "2024-01-01", "YYYY-MM-DD / epoch / 1year")
        row("End date", "before", "", "(empty = latest)")
        row("Target posts", "max_posts", "100", "whole number")

        self.collect_comments_var = tk.BooleanVar(value=True) if tk is not None else None
        comments_row = ttk.Frame(form)
        comments_row.pack(fill="x", pady=2)
        if tk is not None:
            ttk.Checkbutton(comments_row, text="Collect comments after posts (Phase 2)",
                            variable=self.collect_comments_var).pack(side="left")
        ttk.Label(comments_row, text="one request per post, same polite rate",
                  foreground="gray").pack(side="left", padx=(4, 0))

        for label, key in (("Raw output dir", "raw_dir"),
                           ("Processed output dir", "processed_dir"),
                           ("Checkpoint dir", "checkpoint_dir")):
            frame = ttk.Frame(form)
            frame.pack(fill="x", pady=2)
            ttk.Label(frame, text=label, width=22).pack(side="left")
            entry = ttk.Entry(frame)
            entry.insert(0, str(prefill.get(key, {"raw_dir": "data/raw",
                                                  "processed_dir": "data/processed",
                                                  "checkpoint_dir": "data/checkpoints"}[key])))
            entry.pack(side="left", fill="x", expand=True)
            entry.bind("<KeyRelease>", lambda _e: self.refresh_checkpoint_list())
            browse = ttk.Button(frame, text="Browse…",
                                command=lambda e=entry: self._browse_dir(e))
            browse.pack(side="left", padx=(4, 0))
            self.entries[key] = entry

        ttk.Label(form, text="Rate: ≤1 req/s polite throttle, retries with backoff (automatic).",
                  foreground="gray").pack(anchor="w", pady=(4, 0))

        checkpoint_box = ttk.LabelFrame(self.root, text="Saved checkpoints", padding=8)
        checkpoint_box.pack(fill="x", padx=8, pady=4)
        picker_row = ttk.Frame(checkpoint_box)
        picker_row.pack(fill="x", pady=2)
        ttk.Label(picker_row, text="Checkpoint", width=22).pack(side="left")
        self.checkpoint_var = tk.StringVar(value="")
        self.checkpoint_combo = ttk.Combobox(picker_row, textvariable=self.checkpoint_var,
                                             state="readonly", width=60)
        self.checkpoint_combo.pack(side="left", fill="x", expand=True)
        self.checkpoint_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_checkpoint_selected())
        ttk.Button(picker_row, text="Refresh",
                   command=self.refresh_checkpoint_list).pack(side="left", padx=(4, 0))
        self.checkpoint_status_var = tk.StringVar(value="No checkpoint selected.")
        ttk.Label(checkpoint_box, textvariable=self.checkpoint_status_var,
                  foreground="gray").pack(anchor="w")

        buttons = ttk.Frame(self.root)
        buttons.pack(fill="x", padx=8, pady=4)
        self.btn_start = ttk.Button(buttons, text="Start Collection", command=self.start_collection)
        self.btn_start.pack(side="left", padx=(0, 4))
        self.btn_stop = ttk.Button(buttons, text="Stop", command=self.stop_collection,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.btn_resume = ttk.Button(buttons, text="Resume Collection",
                                     command=self.resume_collection, state="disabled")
        self.btn_resume.pack(side="left", padx=4)
        self.btn_new = ttk.Button(buttons, text="New Collection (fresh)",
                                  command=self.new_collection)
        self.btn_new.pack(side="left", padx=4)
        self.btn_open = ttk.Button(buttons, text="Open Output Folder",
                                   command=self.open_output_folder, state="disabled")
        self.btn_open.pack(side="left", padx=4)

    def _build_progress(self) -> None:
        box = ttk.LabelFrame(self.root, text="Progress", padding=8)
        box.pack(fill="x", padx=8, pady=4)
        self.status_var = tk.StringVar(value="Idle — configure and press Start Collection.")
        ttk.Label(box, textvariable=self.status_var).pack(anchor="w")
        self.progress = ttk.Progressbar(box, mode="determinate", maximum=100, value=0)
        self.progress.pack(fill="x", pady=4)
        self.counters_var = tk.StringVar(value="Posts: 0 | Page: 0 | Retries/waits: 0 | Errors: 0 | Elapsed: 0s")
        ttk.Label(box, textvariable=self.counters_var).pack(anchor="w")
        self.path_var = tk.StringVar(value="Output: (none yet)")
        ttk.Label(box, textvariable=self.path_var).pack(anchor="w")

    def _build_log_panel(self) -> None:
        box = ttk.LabelFrame(self.root, text="Log", padding=8)
        box.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log_text = tk.Text(box, height=12, state="disabled", wrap="word")
        scroll = ttk.Scrollbar(box, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # -- actions -------------------------------------------------------

    def _form_inputs(self) -> dict[str, Any]:
        return {key: entry.get() for key, entry in self.entries.items()}

    def _browse_dir(self, entry: Any) -> None:
        chosen = filedialog.askdirectory(initialdir=entry.get() or ".")
        if chosen:
            entry.delete(0, tk.END)
            entry.insert(0, chosen)
            self.refresh_checkpoint_list()

    def _prepare_config(self) -> dict[str, Any] | None:
        """Assemble, write, and validate the GUI config. None + dialog on error."""
        try:
            cfg_path = write_gui_config(self._form_inputs())
            return load_config(cfg_path)
        except (ValueError, ConfigError, OSError) as exc:
            messagebox.showerror("Invalid configuration", str(exc))
            return None

    def _checkpoint_root(self) -> str:
        try:
            return self.entries["checkpoint_dir"].get().strip() or "data/checkpoints"
        except Exception:
            return "data/checkpoints"

    def _selected_checkpoint_name(self) -> str | None:
        """Map the dropdown display value back to the checkpoint name."""
        if tk is None or not hasattr(self, "checkpoint_var"):
            return None
        value = self.checkpoint_var.get().strip()
        if not value:
            return None
        for option in self.checkpoint_options:
            if option["label"] == value:
                return option["name"]
        return None  # stale display value: fall back to the form's collection

    def _form_config_silent(self) -> dict[str, Any] | None:
        """Build a config dict from the form without file IO or dialogs."""
        try:
            return build_config_dict(self._form_inputs())
        except (ValueError, Exception):
            return None

    def refresh_checkpoint_list(self) -> None:
        """Discover saved checkpoints and repopulate the dropdown."""
        if tk is None:
            return
        root = self._checkpoint_root()
        self.checkpoint_options = list_checkpoint_options(root)
        labels = [o["label"] for o in self.checkpoint_options]
        if hasattr(self, "checkpoint_combo"):
            # Prefer the form's own collection when it exists; otherwise keep
            # the previous selection; otherwise select nothing (the user picks).
            preferred: str | None = None
            form_config = self._form_config_silent()
            if form_config is not None:
                try:
                    from .checkpoint import collection_name_for_config

                    candidate = collection_name_for_config(form_config)
                    if any(o["name"] == candidate for o in self.checkpoint_options):
                        preferred = candidate
                except (CheckpointError, Exception):
                    preferred = None
            previous = self._selected_checkpoint_name()
            if preferred is not None:
                wanted = preferred
            elif previous and any(o["name"] == previous for o in self.checkpoint_options):
                wanted = previous
            else:
                wanted = None
            self.checkpoint_combo.configure(values=labels)
            if wanted is not None:
                match = next(o for o in self.checkpoint_options if o["name"] == wanted)
                self.checkpoint_var.set(match["label"])
                self.selected_checkpoint = wanted
            else:
                self.checkpoint_var.set("")
                self.selected_checkpoint = None
        if hasattr(self, "checkpoint_status_var"):
            if not self.checkpoint_options:
                self.checkpoint_status_var.set(
                    "No checkpoints available — start a collection to create one.")
            elif self.selected_checkpoint:
                self.checkpoint_status_var.set(
                    f"Selected checkpoint: {self.selected_checkpoint}")
            else:
                self.checkpoint_status_var.set(
                    "No checkpoint selected — pick one to resume, or start fresh.")
        self.refresh_resume_state()

    def _on_checkpoint_selected(self) -> None:
        """A dropdown choice populates subreddit/dates and revalidates."""
        name = self._selected_checkpoint_name()
        self.selected_checkpoint = name
        if hasattr(self, "checkpoint_status_var"):
            self.checkpoint_status_var.set(
                f"Selected checkpoint: {name}" if name else "No checkpoint selected.")
        if name and not str(name).lower().startswith("legacy"):
            values = checkpoint_form_values(self._checkpoint_root(), name)
            if values:
                for key in ("subreddit", "after", "before"):
                    entry = self.entries.get(key)
                    if entry is not None and values.get(key) not in (None, ""):
                        try:
                            entry.delete(0, tk.END)
                            entry.insert(0, values[key])
                        except Exception:
                            pass
        self.refresh_resume_state()

    def refresh_resume_state(self) -> None:
        """Enable Resume when the selection or the form matches a checkpoint."""
        if tk is None:
            return
        has_checkpoint = False
        try:
            root = self._checkpoint_root()
            selected = self._selected_checkpoint_name()
            if selected:
                from .checkpoint import checkpoint_dir_for_name

                target = checkpoint_dir_for_name(root, selected)
                has_checkpoint = (target / "posts-checkpoint.json").exists() or \
                    (target / "checkpoint.json").exists() or \
                    (target / "comments-checkpoint.json").exists()
            else:
                form_config = self._form_config_silent()
                if form_config is not None:
                    from .checkpoint import checkpoint_dir_for_config

                    target = checkpoint_dir_for_config(root, form_config)
                    has_checkpoint = (target / "posts-checkpoint.json").exists() or \
                        (target / "checkpoint.json").exists() or \
                        (target / "comments-checkpoint.json").exists()
        except Exception:
            has_checkpoint = False
        if hasattr(self, "btn_resume"):
            self.btn_resume.configure(state="normal" if has_checkpoint else "disabled")

    def _selection_error(self, config: dict[str, Any]) -> str | None:
        selected = self._selected_checkpoint_name()
        if not selected:
            return None
        return describe_selection_mismatch(config, selected, self._checkpoint_root())

    def start_collection(self, fresh: bool = False) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        config = self._prepare_config()
        if config is None:
            return
        problem = self._selection_error(config)
        if problem is not None:
            messagebox.showerror("Checkpoint mismatch", problem)
            logger.warning("Collection blocked: %s", problem)
            return
        if not fresh:
            try:
                from .checkpoint import checkpoint_dir_for_config

                target = checkpoint_dir_for_config(config["output"]["checkpoint_dir"], config)
                existing = load_checkpoint_doc(target)
            except (CheckpointError, OSError) as exc:
                messagebox.showerror("Checkpoint error", str(exc))
                return
            if existing is not None:
                from .checkpoint import collection_name_for_config

                cname = collection_name_for_config(config)
                messagebox.showinfo(
                    "Checkpoint exists",
                    f"Checkpoint {cname!r} already exists. Use Resume Collection to "
                    f"continue it, or New Collection to reset only this collection "
                    f"and start over. Other collections are untouched.",
                )
                return
        self._launch_worker(config, fresh=fresh, resume=False,
                            checkpoint=self._selected_checkpoint_name())

    def new_collection(self) -> None:
        config = self._prepare_config()
        if config is None:
            return
        problem = self._selection_error(config)
        if problem is not None:
            messagebox.showerror("Checkpoint mismatch", problem)
            logger.warning("Collection blocked: %s", problem)
            return
        try:
            from .checkpoint import checkpoint_dir_for_config, collection_name_for_config

            cname = collection_name_for_config(config)
            target = checkpoint_dir_for_config(config["output"]["checkpoint_dir"], config)
            has_checkpoint = load_checkpoint_doc(target) is not None
        except (CheckpointError, OSError):
            has_checkpoint = False
            cname = "this collection"
        if has_checkpoint and not messagebox.askyesno(
            "Reset checkpoint?",
            f"This resets checkpoint {cname!r} and starts a completely new collection. "
            f"Other collections are kept, and already-collected data files are kept. Continue?",
        ):
            return
        self._launch_worker(config, fresh=True, resume=False,
                            checkpoint=self._selected_checkpoint_name())

    def resume_collection(self) -> None:
        config = self._prepare_config()
        if config is None:
            return
        problem = self._selection_error(config)
        if problem is not None:
            messagebox.showerror("Checkpoint mismatch", problem)
            logger.warning("Resume blocked: %s", problem)
            return
        self._launch_worker(config, fresh=False, resume=True,
                            checkpoint=self._selected_checkpoint_name())

    def stop_collection(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stopping after the current page…")
        self.btn_stop.configure(state="disabled")

    def open_output_folder(self) -> None:
        target = ""
        if self.last_run_id and self.last_dirs.get("processed_dir"):
            target = str(Path(self.last_dirs["processed_dir"]) / self.last_run_id)
        if not target or not Path(target).is_dir():
            target = self.last_dirs.get("raw_dir", "data/raw")
        try:
            subprocess.Popen(["xdg-open", target])
        except (OSError, FileNotFoundError):
            messagebox.showinfo("Output folder", f"Output is stored under:\n{target}")

    # -- worker plumbing -----------------------------------------------

    def _attach_log_handler(self) -> None:
        if self._handler_attached:
            return
        handler = QueueLogHandler(self.log_queue)
        logging.getLogger().addHandler(handler)
        self._handler_attached = True

    def _launch_worker(self, config: dict[str, Any], fresh: bool, resume: bool,
                       checkpoint: str | None = None) -> None:
        setup_logging(level="INFO", log_file=config["logging"]["file"])
        self._attach_log_handler()
        target = config.get("max_posts")
        self.stats.reset(target_posts=target if isinstance(target, int) else None)
        self.stop_event.clear()
        self.started_at = time.monotonic()
        self.last_dirs = {k: config["output"][k] for k in ("raw_dir", "processed_dir")}
        self.last_run_id = None
        self.path_var.set(f"Output: {self.last_dirs['processed_dir']}/<run_id>/posts/ (chunked JSONL)")
        mode = "resume" if resume else ("fresh" if fresh else "collect")
        self.status_var.set(f"Collecting ({mode})…")
        self._set_running(True)
        self.progress.configure(mode="determinate" if self.stats.target_posts else "indeterminate")
        if not self.stats.target_posts:
            self.progress.start(10)

        def work() -> None:
            try:
                with_comments = bool(self.collect_comments_var.get()) \
                    if self.collect_comments_var is not None else True
                if with_comments:
                    logger.info("Comment collection enabled: Phase 2 will follow Phase 1")
                result = run_collection_flow(
                    config, fresh=fresh and not resume, resume=resume,
                    with_comments=with_comments,
                    should_stop=self.stop_event.is_set,
                    checkpoint=checkpoint,
                )
                self.result_queue.put({"ok": True, "summary": result, "config": config})
            except (CollectorError, ArcticShiftError, StorageError, Exception) as exc:
                logger.exception("Collection failed: %s", exc)
                self.result_queue.put({"ok": False, "error": str(exc)})

        self.worker = threading.Thread(target=work, name="collector-worker", daemon=True)
        self.worker.start()

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.btn_start.configure(state=state)
        self.btn_new.configure(state=state)
        self.btn_resume.configure(state=state)
        self.btn_stop.configure(state="normal" if running else "disabled")

    def _pump(self) -> None:
        """Main-thread poll: drain logs, refresh widgets, handle completion."""
        try:
            while True:
                record = self.log_queue.get_nowait()
                self._append_log(record)
                self.stats.update(record)
        except queue.Empty:
            pass
        snap = self.stats.snapshot()
        if self.worker is not None and self.worker.is_alive():
            elapsed = int(time.monotonic() - self.started_at)
            if snap["waiting_on_rate_limit"]:
                self.status_var.set("⏳ Rate limited — waiting, will continue automatically…")
            elif "Stopping" not in self.status_var.get():
                if snap["in_comment_phase"]:
                    self.status_var.set("Collecting comments…")
                else:
                    self.status_var.set("Collecting…")
            target = f"/{snap['target']}" if snap["target"] else ""
            counters = (
                f"Posts: {snap['posts']}{target} | Page: {snap['pages']} | "
                f"Retries/waits: {snap['retry_waits']} | Errors: {snap['errors']} | "
                f"Elapsed: {elapsed}s"
            )
            if snap["in_comment_phase"]:
                counters += (f" | Comments: {snap['comments_collected']} "
                             f"({snap['comment_posts_done']} posts)")
            self.counters_var.set(counters)
            if snap["target"]:
                self.progress["value"] = min(100.0, 100.0 * snap["posts"] / snap["target"])
        try:
            result = self.result_queue.get_nowait()
        except queue.Empty:
            result = None
        if result is not None:
            self._finish(result)
        self.root.after(self.POLL_MS, self._pump)

    def _append_log(self, record: logging.LogRecord) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, f"{record.levelname}: {record.getMessage()}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _finish(self, result: dict[str, Any]) -> None:
        duration = time.monotonic() - self.started_at
        if hasattr(self.progress, "stop"):
            try:
                self.progress.stop()
            except Exception:
                pass
        if not result["ok"]:
            self.status_var.set("Failed — see log panel.")
            messagebox.showerror("Collection failed", result["error"])
            self._set_running(False)
            self.refresh_checkpoint_list()
            return
        summary = result["summary"]["post"]
        comments = result["summary"]["comments"]
        config = result["config"]
        self.last_run_id = summary["run_id"]
        processed = str(Path(config["output"]["processed_dir"]) / summary["run_id"] / "posts")
        self.path_var.set(f"Output: {processed}")
        self.btn_open.configure(state="normal")
        stopped = summary.get("stopped") or (comments is not None and comments.get("stopped"))
        if stopped:
            self.status_var.set(
                f"Stopped by user after {summary['total_posts']} posts — resume to continue."
            )
        elif comments is None:
            self.status_var.set(
                f"Done: {summary['total_posts']} posts collected."
            )
        else:
            self.status_var.set(
                f"Done: {summary['total_posts']} posts, {comments['total_comments']} comments."
            )
        messagebox.showinfo(
            "Collection stopped" if stopped else "Collection complete",
            format_summary(summary, duration, self.stats.snapshot(),
                           config["output"]["processed_dir"], comments=comments),
        )
        self._set_running(False)
        self.refresh_checkpoint_list()

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno(
                "Collection running",
                "A collection is running. Closing now is crash-safe "
                "(progress is checkpointed per page), but the current page "
                "will be discarded. Close anyway?",
            ):
                return
            self.stop_event.set()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    """Entry point: python -m reddit_collector.gui [--config <file>]."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m reddit_collector.gui",
                                     description="Desktop GUI for the Reddit collector.")
    parser.add_argument("-c", "--config", default=None, help="Prefill the form from this YAML file.")
    args = parser.parse_args(argv)
    if tk is None:
        print("Error: tkinter is not installed. Run: sudo apt install -y python3-tk",
              flush=True)
        return 2
    prefill: dict[str, Any] = {}
    if args.config:
        try:
            prefill = prefill_inputs(load_config(args.config))
        except ConfigError as exc:
            print(f"Configuration error: {exc}", flush=True)
            return 2
    CollectorApp(prefill=prefill).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
