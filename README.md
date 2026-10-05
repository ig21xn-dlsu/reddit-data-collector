# Reddit Collector (Arctic Shift API)

Academic research tool for collecting publicly available Reddit posts via the
[Arctic Shift API](https://github.com/ArthurHeitmann/arctic_shift/tree/master/api)
(`https://arctic-shift.photon-reddit.com`).

Status: fully working pipeline — configure a YAML file, then
`collect` → paginated, rate-limited fetching with per-page checkpointing
into raw + processed JSONL stores. Verified live: 10-post test run and a
500-post r/Philippines run (5 requests, 16s, zero duplicates).

## Contents

- [Requirements](#requirements)
- [Setup](#setup)
- [Configuration](#configuration)
- [CLI commands](#cli-commands)
- [First-run walkthrough (10 posts)](#first-run-walkthrough-10-posts)
- [Small test dataset](#small-test-dataset)
- [Large collection (10,000 posts)](#large-collection-10000-posts)
- [Rate limiting behavior](#rate-limiting-behavior)
- [Pagination behavior](#pagination-behavior)
- [Checkpoint / resume behavior](#checkpoint--resume-behavior)
- [Where raw data is stored](#where-raw-data-is-stored)
- [Where processed data is stored](#where-processed-data-is-stored)
- [Inspecting the collected data](#inspecting-the-collected-data)
- [Verifying the collected posts](#verifying-the-collected-posts)
- [Starting a new collection](#starting-a-new-collection)
- [Logging](#logging)
- [Troubleshooting](#troubleshooting)
- [CLI cheat sheet](#cli-cheat-sheet)
- [Project structure](#project-structure)
- [Module reference](#module-reference)
- [Tests](#tests)
- [Notes on Arctic Shift limits](#notes-on-arctic-shift-limits)

## Requirements

- Python >= 3.10 (`python3 --version`).
- Two libraries: `requests>=2.31`, `pyyaml>=6.0`. Check with:

```bash
python3 -c "import requests, yaml; print('dependencies OK')"
```

No virtualenv is required; the project has no `.venv` and runs against the
system Python. If the check above ever fails with `ModuleNotFoundError`:

```bash
sudo apt update && sudo apt install -y python3-requests python3-yaml
```

## Setup

```bash
cd ~/projects/reddit-collector   # or wherever you cloned it
```

No install step is needed. Run the program in-place with `PYTHONPATH=src`:

```bash
PYTHONPATH=src python3 -m reddit_collector validate --config config.example.yaml
```

(Optional, needs `pip`: `pip install -r requirements.txt` then
`pip install -e .` exposes the short `reddit-collector` command registered in
`pyproject.toml`. All examples below use the long form, which always works.)

## Configuration

All research parameters live in YAML files — never in Python source. Copy an
example, edit the copy, and pass it with `--config`:

```bash
cp config.philippines.example.yaml config.myresearch.yaml
nano config.myresearch.yaml   # or code, vim, any editor
```

Available config files:

| File | Purpose |
|---|---|
| `config.yaml` | Default when `--config` is omitted (worldnews example — back it up before overwriting) |
| `config.example.yaml` | Blank template with every option documented |
| `config.philippines.example.yaml` | Ready-made r/Philippines research setup |
| `config.test-philippines.yaml` | Pre-built 10-post safety test (isolated output dirs) |
| `config.philippines-500.yaml` | The 500-post run configuration |

| Research parameter | Config key | Example |
|---|---|---|
| Subreddit | `subreddit` | `"Philippines"` (`r/` prefix also accepted) |
| Keywords (title only) | `title` | `"election"` (or `null` for no filter) |
| Keywords (title + body) | `query` | `"typhoon relief"` (or `null`) |
| Keywords (body only) | `selftext` | (or `null`) |
| Start date | `after` | `"2024-01-01"` (also epoch numbers or `"1year"`) |
| End date | `before` | `"2024-12-31"` (`null` = latest) |
| Page size | `limit` | `100` (1–100, the API maximum; bigger = fewer requests) |
| Direction | `sort` | `"asc"` oldest-first (recommended) or `"desc"` |
| Target post count | `max_posts` | `500` (`null` = collect until exhausted) |
| Request rate | `collection.throttle_qps` | `1.0` = max ~1 request/second |
| Retries/backoff | `collection.max_retries`, `backoff_base_secs`, `backoff_max_secs` | defaults `5`, `1.0`, `60.0` — normally untouched |
| Output locations | `output.raw_dir` / `processed_dir` / `checkpoint_dir` | defaults under `data/` — normally untouched |

Use one keyword filter at a time unless you want the combination; broad
keyword searches over busy subreddits can time out server-side (the client
retries them — see [Rate limiting](#rate-limiting-behavior)). A
chronological sweep with no keywords and a narrow date window is the most
reliable shape.

Validation runs before anything else: an invalid file prints
`Configuration error:` listing every problem and exits 2 without touching
the API or any data. `${ENV_VAR}` expansion works in any string value, e.g.
`raw_dir: "${OUTPUT_DIR}/raw"`.

## CLI commands

```
reddit-collector collect --config <file> [--fresh]   # start (refuses if a checkpoint exists)
reddit-collector resume --config <file>              # continue an interrupted collection
reddit-collector validate --config <file>            # validate + safe summary, collects nothing
reddit-collector status --config <file>              # checkpoint + stored-run counts, collects nothing
reddit-collector <command> --help                    # help for any command
```

With the long form: `PYTHONPATH=src python3 -m reddit_collector collect --config ...`.
`--config` defaults to `config.yaml`; `--log-level DEBUG` (global flag) overrides
the configured log level for one run.

Exit codes: `0` success; `1` runtime failure mid-collection (API, storage);
`2` usage, configuration, or checkpoint problem (nothing was collected).

The CLI is thin: it parses arguments, prints summaries, and delegates to
`collector.py`. Output only ever shows a fixed set of non-sensitive fields —
anything resembling a credential (key, token, secret, password) is redacted.

## First-run walkthrough (10 posts)

From a fresh terminal to 10 verified Reddit posts:

```bash
# 1. Go to the project
cd ~/projects/reddit-collector

# 2. Confirm dependencies (expect "dependencies OK")
python3 -c "import requests, yaml; print('dependencies OK')"

# 3. Validate the pre-built 10-post test config (expect "Configuration OK", exit 0)
PYTHONPATH=src python3 -m reddit_collector validate --config config.test-philippines.yaml

# 4. Collect (expect "Done: run …: 10 new posts (10 total)", ~2 requests, a few seconds)
PYTHONPATH=src python3 -m reddit_collector collect --config config.test-philippines.yaml

# 5. Confirm status (expect checkpoint: 10 posts; Stored runs: 1, raw=10 processed=10)
PYTHONPATH=src python3 -m reddit_collector status --config config.test-philippines.yaml

# 6. Verify the files (expect 10 and 10)
RUN=$(ls data/e2e-test/raw/)
wc -l data/e2e-test/raw/$RUN/posts.jsonl data/e2e-test/processed/$RUN/posts.jsonl

# 7. Prove resume is idempotent (expect "0 new posts", counts unchanged)
PYTHONPATH=src python3 -m reddit_collector resume --config config.test-philippines.yaml
wc -l data/e2e-test/raw/$RUN/posts.jsonl
```

## Small test dataset

Use `config.test-philippines.yaml`: r/Philippines, a 2-day window
(`2024-01-01` → `2024-01-03`), `limit: 5`, `max_posts: 10`. Two pages
exercise the pagination cursor; outputs stay isolated under `data/e2e-test/`
and `logs/e2e-test.log` so the main `data/` tree is untouched. For ~20 posts,
copy it and set `max_posts: 20`.

## Large collection (10,000 posts)

```bash
cp config.philippines-500.yaml config.philippines-10k.yaml
nano config.philippines-10k.yaml   # set max_posts: 10000 (keep limit: 100)
PYTHONPATH=src python3 -m reddit_collector validate --config config.philippines-10k.yaml
PYTHONPATH=src python3 -m reddit_collector status --config config.philippines-10k.yaml  # no stale checkpoint?
PYTHONPATH=src python3 -m reddit_collector collect --config config.philippines-10k.yaml
# If interrupted: PYTHONPATH=src python3 -m reddit_collector resume --config config.philippines-10k.yaml
```

Request math: 10,000 ÷ 100 per page = **~100 API requests** clean
(~10 minutes at ~5–6s observed per request), plus bounded retries.
Disk: ~55 MB raw + ~5 MB processed (measured live: ~5.5 KB / ~0.5 KB per
post). A checkpoint is saved after every page (100 of them), so the run is
interruption-safe throughout. `max_posts` is a cap, not a guarantee — widen
`after`/`before` if the window may hold fewer than 10,000 posts.

## Rate limiting behavior

`src/reddit_collector/rate_limit.py` — `RateLimiter`, attached automatically
by `ArcticShiftClient.from_config()`:

- Spacing: at most `collection.throttle_qps` requests/sec (default 1.0, well
  under the documented couple-per-second). Never raises the rate on its own —
  no bypass logic, no proxies, no multi-IP.
- Retries (`collection.max_retries`, default 5): HTTP 429, HTTP 422 transient
  timeouts (`"Timeout. Maybe slow down a bit"`), HTTP 5xx, and connection
  errors/timeouts. Other 4xx fail immediately.
- 429 handling honours the standard `Retry-After` header first, then Arctic
  Shift's `X-RateLimit-Reset`; the server's wait always wins over the
  computed backoff. (422 retries use pure backoff: `X-RateLimit-Reset` rides
  along on every response including 200s, so it is informational there.)
- Backoff: `backoff_base_secs * 2**attempt`, capped at `backoff_max_secs`
  (defaults 1s / 60s, both configurable).
- Every wait is logged (`Rate limiting: waiting …` at INFO,
  `Waiting … before retry` at WARNING).

## Pagination behavior

`src/reddit_collector/paginator.py` — `PostPaginator`:

- Mechanism per current API docs (no page tokens exist): results sorted by
  `created_utc`; each request advances a time cursor (`after=max(...)` for
  `asc`, `before=min(...)` for `desc`). Pagination calls `search_posts()`,
  inheriting its rate limiter; the client module needed no changes.
- Stops when a page comes back empty/short, at `max_posts`, or on an
  unmoving cursor (safety stop against infinite loops, logged with a warning).
- Deduplicates by post `id`; a full page of only duplicates steps the cursor
  past the boundary (with a warning — same-second posts past a page boundary
  can theoretically be skipped, which is logged).
- Progress: `Page N: M new posts (T total)` at INFO per page.
- One page (≤100 posts) is held in memory at a time, plus the seen-id set.

## Checkpoint / resume behavior

`src/reddit_collector/checkpoint.py` — `data/checkpoints/checkpoint.json`:

- After every page, `save_checkpoint(dir, paginator.state(), run_id=...)`
  persists cursor, total, seen ids, filters, and run id. Writes are atomic
  (temp file in the same directory + `os.replace()`), so a crash never
  leaves a half-written checkpoint; a failed save keeps the previous one.
- `resume` loads the snapshot into `PostPaginator(resume=...)`, which
  re-validates filters and skips seen ids, reopens the same `run_id`, and
  reconciles ids already on disk first — so even a crash between writing a
  page and checkpointing it cannot duplicate that page.
- Missing checkpoint: `collect` starts fresh, `resume` exits 2 with guidance.
  Existing checkpoint: `collect` refuses (use `resume`, or `collect --fresh`
  to deliberately discard). Corrupt checkpoint: clean exit 2, file left
  untouched for inspection.
- Every save/resume/clear is logged with post count and cursor.

## Where raw data is stored

`data/raw/<run_id>/` (one timestamped directory per run; runs never overwrite
each other):

- `posts.jsonl` — every API post object **verbatim** (~80 fields each), one
  JSON per line. Appended page by page; keep for provenance/reproducibility.
- `manifest.json` — `run_id`, `started_at`, and the run parameters
  (subreddit, dates, sort, limits).

## Where processed data is stored

`data/processed/<run_id>/posts.jsonl` — normalized records, one JSON per
line, fixed keys:

`id, subreddit, title, selftext, author, score, num_comments, created_utc, created_iso, url, permalink`

The original post `id` is preserved verbatim; missing values are null;
`created_iso` is the UTC ISO-8601 rendering of `created_utc`. **Use this file
for data analysis** (e.g. `pd.read_json(path, lines=True)`). JSON Lines was
chosen because pages append cleanly and readers stream — flat memory usage
for arbitrarily large datasets. There is no CSV/Parquet export in this
version.

## Inspecting the collected data

```bash
ls data/raw/ data/processed/                             # all runs
ls data/raw/20261005t101821z_philippines/                # posts.jsonl + manifest.json
head -n 1 data/processed/20261005t101821z_philippines/posts.jsonl | python3 -m json.tool  # pretty-print one record
cat data/raw/20261005t101821z_philippines/manifest.json  # run parameters
```

## Verifying the collected posts

```bash
RUN=20261005t101821z_philippines   # your run ID from the status output
wc -l data/raw/$RUN/posts.jsonl data/processed/$RUN/posts.jsonl
PYTHONPATH=src python3 -c "
import json
raw = [json.loads(l) for l in open('data/raw/$RUN/posts.jsonl')]
proc = [json.loads(l) for l in open('data/processed/$RUN/posts.jsonl')]
rids, pids = [r['id'] for r in raw], [r['id'] for r in proc]
print('raw unique:', len(set(rids)) == len(rids))
print('processed unique:', len(set(pids)) == len(pids))
print('layers agree:', set(rids) == set(pids))
print('subreddits:', {r['subreddit'] for r in proc})
"
```

For the 500-post run this reports `500` / `500`, all `True`, `{'Philippines'}`.

## Starting a new collection

A leftover checkpoint makes plain `collect` refuse — that is the safety net
against accidentally continuing an old run. To start over, discard it
explicitly (old data files are never touched; the new run gets a fresh
`run_id`):

```bash
PYTHONPATH=src python3 -m reddit_collector collect --config config.myresearch.yaml --fresh
PYTHONPATH=src python3 -m reddit_collector status --config config.myresearch.yaml   # confirm
```

## Logging

Configured via `logging.level` and `logging.file` (default `logs/collector.log`).
Every run logs to stderr plus the file in this format:

```
2026-10-05 12:00:00 | INFO     | reddit_collector | message
```

Watch a running collection with `tail -f logs/collector.log`. You will see
per-page progress, checkpoint saves with cursor values, and any rate-limit
waits — everything needed to audit what the program did.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `collect` refuses: "checkpoint already exists" | A previous run left state. `resume` to continue it, or `collect --fresh` to discard and restart. |
| `resume`: "nothing to resume" (exit 2) | No checkpoint in this config's `checkpoint_dir` — use `collect`, or check you're pointing at the right config. |
| `Configuration error:` (exit 2) | Fix the listed fields; nothing was fetched. Quote odd YAML values; dates accept `YYYY-MM-DD`, epochs, or `"1year"`. |
| `Collection failed: … 429 …` (exit 1) | Rate limit persisted through all retries. Wait a few minutes, lower `throttle_qps`, then `resume` — progress is checkpointed. |
| `Collection failed: … timed out …` (exit 1) | Query too heavy. Narrow the date window, add a subreddit/keyword filter, or lower `limit`, then `resume` or `--fresh`. |
| `ModuleNotFoundError: requests/yaml` | See [Requirements](#requirements) — install the system packages. |
| `0 new posts` but exit 0 | Window holds no (more) posts, or `max_posts` already reached — check `status` counts before assuming failure. |
| Counts differ between raw and processed | Should never happen (both append together); report the run ID and check the log for mid-page errors. |

## CLI cheat sheet

```bash
cd ~/projects/reddit-collector
PYTHONPATH=src python3 -m reddit_collector validate --config <file>          # validate only
PYTHONPATH=src python3 -m reddit_collector collect --config <file>           # start
PYTHONPATH=src python3 -m reddit_collector collect --config <file> --fresh  # discard checkpoint, start over
PYTHONPATH=src python3 -m reddit_collector resume --config <file>            # continue after interruption
PYTHONPATH=src python3 -m reddit_collector status --config <file>            # checkpoint + run counts
PYTHONPATH=src python3 -m reddit_collector <command> --help                  # help for any command
tail -f logs/collector.log                   # watch progress live
ls data/raw/ data/processed/                 # all runs
wc -l data/processed/<run_id>/posts.jsonl    # count a run's records
```

## Desktop GUI

`src/reddit_collector/gui.py` — a simple tkinter frontend over the exact same
collector (same client, limiter, paginator, checkpoints, storage; no
duplicated logic). Needs the system package `python3-tk` once:

```bash
sudo apt install -y python3-tk
cd ~/projects/reddit-collector
PYTHONPATH=src python3 -m reddit_collector gui --config config.philippines-500.yaml
```

Form fields map 1:1 onto config keys: subreddit, keywords (title+body, with
an advanced title-only/body-only row), start/end dates, target posts, and
output directories with Browse buttons. The rate stays at the proven polite
defaults (shown read-only).

- **Start Collection** writes `data/gui/last-run.yaml`, validates it through
  the normal `load_config`, and collects in a background thread (UI stays
  responsive). Refuses when a checkpoint exists, like the CLI.
- **Stop** pauses cleanly after the current finished page — the page is
  stored and checkpointed, so `Resume` continues with zero loss/duplicates.
  (One cooperative `should_stop` hook in `collector._run`; default behavior
  unchanged.)
- **Resume Collection** continues from the checkpoint; **New Collection
  (fresh)** confirms, then discards the checkpoint and starts over.
- Progress shows posts/target, page, retries/waits, errors, elapsed time; a
  ⏳ banner appears while waiting on rate limits, then collection continues
  automatically. The log panel streams the same log records as the CLI.
- The completion dialog reports total posts, duration, requests
  (pages + retries), rate-limit waits, and the output path; **Open Output
  Folder** reveals the run directory (`xdg-open`, path always displayed).

## Project structure

```
reddit-collector/
  config.example.yaml              # template — copy to config.yaml
  config.philippines.example.yaml  # r/Philippines research setup
  config.test-philippines.yaml     # 10-post safety test (isolated dirs)
  config.philippines-500.yaml      # 500-post run configuration
  requirements.txt / pyproject.toml
  src/reddit_collector/
    __init__.py
    __main__.py                    # thin CLI: collect/resume/validate/status subcommands
    config.py                      # YAML load + validation + ${ENV_VAR} expansion
    logging_setup.py               # console + file logging
    client.py                      # ArcticShiftClient: GET /api/posts/search, returns parsed JSON
    rate_limit.py                  # RateLimiter: spacing + 429/422/5xx retries with backoff
    paginator.py                   # PostPaginator: time-cursor pagination, dedup, resume state
    checkpoint.py                  # JSON checkpoint store: atomic save, load, clear
    storage.py                     # RunStore: raw + processed JSONL per run, normalize_post
    collector.py                   # collection loop: paginator -> storage -> checkpoints
    gui.py                         # tkinter desktop GUI (form, worker thread, log panel)
  tests/                           # 132 unittest tests, mocked HTTP (no real calls)
  data/raw/<run_id>/               # posts.jsonl (verbatim) + manifest.json, one dir per run
  data/processed/<run_id>/         # posts.jsonl (normalized records), one dir per run
  data/e2e-test/                   # isolated 10-post test artifacts
  data/checkpoints/                # checkpoint.json resume state
  logs/
```

## Module reference

API client (`client.py`, uses `requests`):

```python
from reddit_collector.client import ArcticShiftClient
from reddit_collector.config import load_config

client = ArcticShiftClient.from_config(load_config("config.yaml"))
payload = client.search_posts(subreddit="worldnews", title="wuhan",
                              after="2019-12-30", sort="asc", limit=25)
posts = payload.get("data", [])
```

- Endpoint per current docs: `GET {base_url}/api/posts/search` with
  `subreddit, title/query/selftext, author, after, before, sort, limit (1-100), fields`.
- `base_url` comes from `api.base_url` in config (default
  `https://arctic-shift.photon-reddit.com`).
- Returns the parsed JSON dict; never prints or writes files.
- Errors: `ArcticShiftRateLimitError` on 429 (exposes
  `retry_after_secs`/`reset_at`), `ArcticShiftQueryTimeoutError` on transient
  timeouts (HTTP 422 `"Timeout. Maybe slow down a bit"`, retried with
  backoff, or timeout wording in 200 responses), `ArcticShiftAPIError` /
  `ArcticShiftNetworkError` otherwise.
- No proxies, no multi-IP logic by design — one origin, one session.

Pagination (`paginator.py`):

```python
paginator = PostPaginator.from_config(load_config("config.yaml"), client)
for post in paginator.iter_posts():
    ...  # one post at a time, minimal memory
```

Storage (`storage.py`):

```python
store = RunStore.from_config(load_config("config.yaml"))  # fresh run_id
store.write_manifest({"subreddit": "python", "sort": "asc"})
store.append_page(posts)          # one fetched page -> raw + processed
resumed = RunStore.existing(raw_dir, processed_dir, run_id)  # resume run
for record in store.iter_processed():  # streams, never loads all
    ...
```

Checkpoints (`checkpoint.py`): `save_checkpoint(dir, state, run_id)` after
each page; `load_checkpoint(dir)` / `load_checkpoint_doc(dir)` on startup;
`clear_checkpoint(dir)` for `--fresh`.

## Tests

Stdlib `unittest` with mocked HTTP (no real calls, no real sleeping):

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

121 tests cover client params/errors, spacing/429/`Retry-After`/backoff,
422-timeout retries, pagination/dedup/resume, checkpoint roundtrip/invalid,
storage normalize/append/corrupt, config validation, CLI exit codes, and
end-to-end collect/resume with mocked HTTP. Live behavior was additionally
verified with small real queries (10-post and 500-post runs).

## Notes on Arctic Shift limits

Rate limiting is dynamic (based on server load + query complexity), not a fixed
QPS. Docs advise a couple of requests/second is fine for normal users; on `429`
honour `X-RateLimit-Reset` / `X-RateLimit-Reset-At`, which this tool does
automatically with conservative spacing plus exponential backoff. Heavy,
unfiltered queries can also return HTTP 422 timeouts, which are retried the
same way. For massive backfills, use the monthly `.zst` dumps instead of the API.
