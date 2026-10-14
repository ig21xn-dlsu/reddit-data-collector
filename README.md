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
- [Comment collection (Phase 2)](#comment-collection-phase-2)
- [Inspecting the collected data](#inspecting-the-collected-data)
- [Verifying the collected posts](#verifying-the-collected-posts)
- [Starting a new collection](#starting-a-new-collection)
- [Logging](#logging)
- [Troubleshooting](#troubleshooting)
- [CLI cheat sheet](#cli-cheat-sheet)
- [Desktop GUI](#desktop-gui)
- [Project structure](#project-structure)
- [Module reference](#module-reference)
- [Tests](#tests)
- [Notes on Arctic Shift limits](#notes-on-arctic-shift-limits)
- [License](#license)

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
git clone <your-repo-url> reddit-collector
cd reddit-collector
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .   # exposes the short `reddit-collector` command
cp config.example.yaml config.yaml
```

Requires Python >= 3.10. Check dependencies with:

```bash
python3 -c "import requests, yaml; print('dependencies OK')"
```

No install is strictly required: without `pip install -e .`, run everything
in-place with `PYTHONPATH=src` (all examples below use this long form, which
always works):

```bash
PYTHONPATH=src python3 -m reddit_collector validate --config config.example.yaml
```

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
| Chunk size | `output.chunk_size` | `50000` = max JSONL records per chunk file (positive int) |

Use one keyword filter at a time unless you want the combination; broad
keyword searches over busy subreddits can time out server-side (the client
retries them — see [Rate limiting](#rate-limiting-behavior)). A
chronological sweep with no keywords and a narrow date window is the most
reliable shape.

Validation runs before anything else: an invalid file prints
`Configuration error:` listing every problem and exits 2 without touching
the API or any data. `${ENV_VAR}` expansion works in any string value, e.g.
`raw_dir: "${OUTPUT_DIR}/raw"`.

Only `config.yaml` is git-ignored. Name personal research configs clearly
(e.g. `config.myproject.yaml`) and check `git status` before committing, so
private research parameters don't end up in the repository — the
`config.*.example.yaml` files are the shareable templates.

## CLI commands

```
reddit-collector collect --config <file> [--fresh] [--checkpoint NAME]  # start (refuses if this collection has a checkpoint)
reddit-collector resume --config <file> [--checkpoint NAME]             # continue this collection
reddit-collector validate --config <file>            # validate + safe summary, collects nothing
reddit-collector status --config <file>              # this collection + all saved checkpoints, collects nothing
reddit-collector checkpoints --config <file>         # list saved checkpoints, collects nothing
reddit-collector migrate-checkpoint --config <file>  # copy legacy checkpoint into this collection (originals kept)
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

# 6. Verify the files (expect 10 and 10; one chunk each by default)
RUN=$(ls data/e2e-test/raw/)
cat data/e2e-test/raw/$RUN/posts/posts_*.jsonl | wc -l
cat data/e2e-test/processed/$RUN/posts/posts_*.jsonl | wc -l

# 7. Prove resume is idempotent (expect "0 new posts", counts unchanged)
PYTHONPATH=src python3 -m reddit_collector resume --config config.test-philippines.yaml
cat data/e2e-test/raw/$RUN/posts/posts_*.jsonl | wc -l
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

Each collection configuration gets its own persistent checkpoint directory,
named `<Subreddit>-<after>-<before>` (subreddit `r/`-prefix stripped,
filename-sanitized), e.g. `Philippines-2026-01-01-2026-06-30`:

```
data/checkpoints/
├── Philippines-2026-01-01-2026-06-30/
│   ├── posts-checkpoint.json
│   └── comments-checkpoint.json
└── Thailand-2026-01-01-2026-06-30/
    ├── posts-checkpoint.json
    └── comments-checkpoint.json
```

Switching subreddits never deletes or overwrites another collection's
checkpoint; resuming restores that collection's progress, run ID, and chunk
files. Post and comment checkpoints live in the same collection directory and
are bound to the same run ID, so comment data can never leak into another
collection's run.

- After every page, `save_checkpoint(dir, paginator.state(), run_id=...,
  collection=...)` persists cursor, total, seen ids, filters, run id, plus a
  `collection` block (subreddit, dates, title/query/selftext/author, sort).
  Writes are atomic (temp file in the same directory + `os.replace()`), so a
  crash never leaves a half-written checkpoint; a failed save keeps the
  previous one.
- `resume` loads the snapshot into `PostPaginator(resume=...)`, which
  re-validates filters and skips seen ids, reopens the same `run_id`, and
  reconciles ids already on disk first — so even a crash between writing a
  page and checkpointing it cannot duplicate that page.
- Compatibility is validated from stored metadata (never just the directory
  name) **before** any network request or checkpoint/dataset change. A
  mismatch fails cleanly, e.g.:
  `Checkpoint mismatch: selected checkpoint 'Thailand-2026-01-01-2026-06-30'
  belongs to r/Thailand, but the current configuration specifies
  r/Philippines. Select a compatible checkpoint or change the subreddit.`
  Nothing is cleared, overwritten, resumed, or fetched on mismatch.
- Missing checkpoint: `collect` starts fresh, `resume` exits 2 naming the
  collection and listing available checkpoints. Existing checkpoint:
  `collect` refuses (use `resume`, or `collect --fresh` to reset **only this
  collection** — other collections and all datasets on disk are kept).
  Corrupt checkpoint: clean exit 2, file left untouched for inspection.
- `--checkpoint NAME` selects a checkpoint explicitly (CLI and GUI dropdown);
  it must match the configuration or collection fails fast with the mismatch
  error above.
- Chunk-safe: on `resume`, `RunStore.existing()` scans the run's chunk files
  (plus any legacy single file) and continues in the first non-full chunk —
  never overwriting, duplicating, or skipping. Changing `output.chunk_size`
  between runs is safe: old chunks are left alone, new records follow the new
  size.
- Legacy shared checkpoints (`data/checkpoints/checkpoint.json` /
  `comments-checkpoint.json` at the root) are still listed by `status` /
  `checkpoints` and left untouched — never adopted silently. Copy one into a
  collection explicitly after verifying ownership:
  `reddit-collector migrate-checkpoint --config <file>` (refuses when
  incompatible or when the target already exists; originals are kept).
- Every save/resume/clear is logged with post count and cursor.

## Where raw data is stored

`data/raw/<run_id>/` (one timestamped directory per run; runs never overwrite
each other). Posts and comments are chunked at `output.chunk_size`
(default 50,000 records per file) — a new numbered file starts automatically:

```
data/raw/<run_id>/
├── posts/posts_00001.jsonl  (≤ chunk_size verbatim post objects each)
├── posts/posts_00002.jsonl
├── comments/comments_00001.jsonl
└── manifest.json            # run_id, started_at, params, chunk_size,
                             # chunk file list + record counts, totals
```

- `posts/posts_*.jsonl` — every API post object **verbatim** (~80 fields each),
  one JSON per line. Appended page by page (incrementally, never held fully
  in memory); keep for provenance/reproducibility.
- `manifest.json` — `run_id`, `started_at`/`updated_at`, `chunk_size`, the run
  parameters (subreddit, dates, sort, limits), plus `chunks` (file + record
  counts per stream) and `totals`. Refreshed after every append.

Configure the chunk size in YAML (`output.chunk_size: 50000`). For a 120,000
record test run you get `posts_00001.jsonl` (50,000), `posts_00002.jsonl`
(50,000), `posts_00003.jsonl` (20,000) — no chunk exceeds the limit.

Legacy single-file runs (`posts.jsonl` / `comments.jsonl` directly under the
run dir) are still read (legacy content first, then chunks) but are never
modified, renamed, or deleted; new records for those runs go to chunks.

## Where processed data is stored

`data/processed/<run_id>/posts/posts_*.jsonl` — normalized records, one JSON
per line, fixed keys (same chunking as raw):

`id, subreddit, title, selftext, author, score, num_comments, created_utc, created_iso, url, permalink`

The original post `id` is preserved verbatim; missing values are null;
`created_iso` is the UTC ISO-8601 rendering of `created_utc`. **Use these files
for data analysis** (e.g. `pd.concat([pd.read_json(f, lines=True) for f in
sorted(glob("data/processed/<run_id>/posts/posts_*.jsonl"))])`). JSON Lines was
chosen because pages append cleanly and readers stream — flat memory usage
for arbitrarily large datasets. There is no CSV/Parquet export in this
version.

## Comment collection (Phase 2)

Phase 1 (`collect`) gathers posts and is unchanged. Phase 2 fetches the
discussion for those posts — one `GET /api/comments/tree?link_id=<post_id>`
per post (never `/api/comments/search`), returning the complete nested
thread. Collect posts first, then:

```bash
# Comments for an existing run (run defaults to the post checkpoint's run)
PYTHONPATH=src python3 -m reddit_collector collect-comments --config config.philippines-500.yaml

# Explicit run, or a capped test run
PYTHONPATH=src python3 -m reddit_collector collect-comments --config config.yaml --run 20261005t101821z_philippines
PYTHONPATH=src python3 -m reddit_collector collect-comments --config config.yaml --max-comment-posts 20

# 5-post smoke test (first 5 posts; resumable into a full run afterwards)
PYTHONPATH=src python3 -m reddit_collector collect-comments --config config.yaml --smoke
```

Resuming is just re-running the same command: completed posts (tracked in
the collection's `comments-checkpoint.json`, independent of the post
checkpoint) are never re-requested, and already-stored comments are
reconciled before starting, so crashes can't duplicate data. `status` also
reports the comment checkpoint and stored comment counts.

Safety rails: a run with no processed posts (or no usable post IDs) fails
with a clear error instead of a silent zero-post "success" — while a healthy
run where every post simply has zero comments still completes successfully.
Per-run `status` lines show comment progress (`complete` / `in_progress` /
`not_started`, with posts and comment counts); if the checkpoint is gone but
comment chunk data exists, status says so explicitly instead of showing
nothing.

ID relationships: every comment carries `post_id` (bare post ID = the
`id` in the processed post chunks) and `parent_id` + `parent_kind` (`post` for
top-level, `comment` for replies), plus `depth` and root-to-node `path`,
so threads rebuild from `parent_id` alone.

Storage (same run directory, same chunked JSONL format):

- Raw: `data/raw/<run_id>/comments/comments_*.jsonl` — verbatim `{"kind", "data"}`
  API nodes in parents-before-children order, including collapsed
  `"kind": "more"` placeholders (left unresolved in Phase 1).
- Processed: `data/processed/<run_id>/comments/comments_*.jsonl` — normalized records:
  `comment_id, post_id, parent_id, parent_kind, kind, depth, path,
  subreddit, author, body, score, created_utc, created_iso, permalink,
  collapsed_children, collapsed_count` (`"more"` nodes are explicitly
  marked with their unresolved children IDs).

Timing: comments dominate the request budget — one (larger) request per post
versus ~1 per 100 posts in Phase 1. At ~1 req/s and observed 1–10s per tree,
10,000 posts take roughly **3–28 hours** depending on thread sizes (posts
with `num_comments == 0` are skipped by default at zero cost). Tune via
`comments.tree_limit` / `max_comment_posts` in config.

## Inspecting the collected data

```bash
ls data/raw/ data/processed/                             # all runs
ls data/raw/20261005t101821z_philippines/                # posts/ + comments/ + manifest.json
ls data/raw/20261005t101821z_philippines/posts/          # posts_00001.jsonl, ...
head -n 1 data/processed/20261005t101821z_philippines/posts/posts_00001.jsonl | python3 -m json.tool  # pretty-print one record
cat data/raw/20261005t101821z_philippines/manifest.json  # run parameters + chunk inventory
```

## Verifying the collected posts

```bash
RUN=20261005t101821z_philippines   # your run ID from the status output
cat data/raw/$RUN/posts/posts_*.jsonl | wc -l
cat data/processed/$RUN/posts/posts_*.jsonl | wc -l
PYTHONPATH=src python3 -c "
import glob, json
raw = [json.loads(l) for f in sorted(glob.glob('data/raw/$RUN/posts/posts_*.jsonl')) for l in open(f)]
proc = [json.loads(l) for f in sorted(glob.glob('data/processed/$RUN/posts/posts_*.jsonl')) for l in open(f)]
rids, pids = [r['id'] for r in raw], [r['id'] for r in proc]
print('raw unique:', len(set(rids)) == len(rids))
print('processed unique:', len(set(pids)) == len(pids))
print('layers agree:', set(rids) == set(pids))
print('subreddits:', {r['subreddit'] for r in proc})
"
```

`validate` also reports collective chunk totals per run (all chunks summed,
not a single file). For programmatic checks use
`RunStore.existing(...).validate_chunks()`.

For the 500-post run this reports `500` / `500`, all `True`, `{'Philippines'}`.

## Starting a new collection

A leftover checkpoint for the same collection makes plain `collect` refuse —
that is the safety net against accidentally continuing an old run. To start
over, reset only that collection explicitly (other collections and old data
files are never touched; the new run gets a fresh `run_id`):

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
| `collect` refuses: "checkpoint already exists" | That collection left state. `resume` (or `resume --checkpoint NAME`) to continue it, or `collect --fresh` to reset only that collection. |
| `resume`: "nothing to resume" (exit 2) | No checkpoint for this collection in `checkpoint_dir` — use `collect`, pick another `--checkpoint`, or `checkpoints` to list. |
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
PYTHONPATH=src python3 -m reddit_collector collect --config <file> --fresh  # reset only this collection, start over
PYTHONPATH=src python3 -m reddit_collector resume --config <file>            # continue this collection
PYTHONPATH=src python3 -m reddit_collector resume --config <file> --checkpoint <name>  # resume an explicit checkpoint
PYTHONPATH=src python3 -m reddit_collector checkpoints --config <file>      # list saved checkpoints
PYTHONPATH=src python3 -m reddit_collector migrate-checkpoint --config <file>  # adopt legacy checkpoint (originals kept)
PYTHONPATH=src python3 -m reddit_collector collect-comments --config <file>   # Phase 2: comments for the run
PYTHONPATH=src python3 -m reddit_collector collect-comments --config <file> --smoke  # 5-post smoke test
PYTHONPATH=src python3 -m reddit_collector status --config <file>            # checkpoint + run counts
PYTHONPATH=src python3 -m reddit_collector <command> --help                  # help for any command
tail -f logs/collector.log                   # watch progress live
ls data/raw/ data/processed/                 # all runs
cat data/processed/<run_id>/posts/posts_*.jsonl | wc -l   # count a run's records
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
  responsive): Phase 1 posts first, then Phase 2 comments when the
  "Collect comments after posts" box is checked. Refuses when this
  collection's checkpoint exists, like the CLI.
- **Saved checkpoints** dropdown lists every saved collection automatically
  (name, status, post count, saved time), with Refresh. Selecting one shows
  it clearly and fills subreddit/start/end dates from its stored metadata;
  Resume uses the selection, and a mismatched selection (e.g. Thailand
  checkpoint with a Philippines form) is blocked with a clear error before
  anything is fetched or written. "No checkpoints available" is shown when
  empty. The list refreshes after every run, stop, or reset.
- **Stop** pauses cleanly after the current finished page — the page is
  stored and checkpointed, so `Resume` continues with zero loss/duplicates.
  (One cooperative `should_stop` hook in `collector._run`; default behavior
  unchanged.)
- **Resume Collection** continues the selected (or form-matching) checkpoint
  (posts and comments resume independently from their own checkpoint files);
  **New Collection (fresh)** confirms naming the exact collection to reset,
  then clears only that collection's post and comment checkpoints and starts
  over — other collections and all datasets on disk are kept, so no stale
  checkpoint can leak into the new run.- Progress shows posts/target, page, retries/waits, errors, elapsed time —
  plus comment counters once Phase 2 starts; a
  ⏳ banner appears while waiting on rate limits, then collection continues
  automatically. The log panel streams the same log records as the CLI.
- The completion dialog reports total posts, duration, requests
  (pages + retries), rate-limit waits, comment totals when collected, and the
  output path; **Open Output
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
    comments.py                    # Phase 2: tree flattening + comment normalization
    gui.py                         # tkinter desktop GUI (form, worker thread, log panel)
  tests/                           # 229 unittest tests, mocked HTTP (no real calls)
  tests/test_chunking.py           # chunk boundaries, resume, legacy, manifest, 120k acceptance
  tests/test_named_checkpoints.py  # per-collection checkpoints, mismatch validation, GUI listing, migration
  data/raw/<run_id>/               # posts/posts_*.jsonl + comments/comments_*.jsonl + manifest.json
  data/processed/<run_id>/         # posts/posts_*.jsonl + comments/comments_*.jsonl (chunked)
  data/e2e-test/                   # isolated 10-post test artifacts
  data/checkpoints/<Sub>-<after>-<before>/  # posts-checkpoint.json + comments-checkpoint.json per collection
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
store.append_page(posts)          # one fetched page -> raw + processed chunks
resumed = RunStore.existing(raw_dir, processed_dir, run_id)  # resume run
for record in store.iter_processed():  # streams across legacy + all chunks
    ...
store.validate_chunks()           # collective totals + over-limit check
```

Chunk size: `output.chunk_size` (default 50000). `RunStore` rotates to
`posts_00001.jsonl`, `posts_00002.jsonl`, … under `<run_id>/posts/` (and the
same for `comments/`, raw + processed) and records every file + count in
`manifest.json`. Resume scans existing chunks and continues in the first
non-full file; legacy `posts.jsonl`/`comments.jsonl` files are read but never
written.

Checkpoints (`checkpoint.py`): per-collection dirs
`checkpoint_dir_for_config(root, config)` holding `posts-checkpoint.json` +
`comments-checkpoint.json` with a `collection` metadata block;
`save_checkpoint(dir, state, run_id, collection)` after each page;
`load_checkpoint(dir)` / `load_checkpoint_doc(dir)` on startup;
`clear_checkpoint(dir)` resets one collection; `list_collections(root)` for
discovery; `validate_collection_compatible(doc, config)` before any resume;
`migrate_legacy_checkpoint(root, config)` for explicit legacy adoption.

## Tests

Stdlib `unittest` with mocked HTTP (no real calls, no real sleeping):

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

229 tests cover client params/errors, spacing/429/`Retry-After`/backoff,
422-timeout retries, comment-tree fetching/flattening, Phase-2 collect/resume,
pagination/dedup/resume, per-collection checkpoints (naming, switching,
resume, mismatch validation, GUI listing, legacy migration),
checkpoint roundtrip/invalid, chunked storage
(boundaries, resume, legacy, manifest, collective validation),
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

## License

No license file is included yet. Add one (e.g. MIT) before publishing if you
want others to reuse this code — without it, a public repository defaults to
all rights reserved.
