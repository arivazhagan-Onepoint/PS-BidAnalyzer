# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

- **Name:** PS-BidAnalyzer
- **Purpose:** Reads tenders from the **PS Tender Tracker** Google Sheet, scores each
  against Onepoint's documented capabilities using an LLM, and writes back a
  Bid / NoBid / TBD qualification (plus reason, date, comment, and row colour).
- The tool does **not** create the sheet — it locates an existing sheet by name in
  a configured Drive folder (row 1 = summary, row 2 = headers, row 3+ = data),
  populated by an upstream process.

## Tech Stack

- **Language:** Python 3.10+ (developed on 3.14)
- **LLM (active):** Google **Gemini** via the native `google-genai` SDK.
  Model is `gemini-3.1-flash-lite` (`GEMINI_MODEL` in `analyzer/config.py`).
  Thinking is disabled (`ANALYZER_THINKING_BUDGET = 0`) because Gemini 3.x draws
  reasoning tokens from the output budget, which truncated the JSON reply.
- **LLM (backup, not used):** **OpenRouter** via the `openai` SDK. The wrapper
  (`analyzer/openrouter_client.py`) and its `OPENROUTER_*` settings are retained
  for reference only; nothing in the active path imports them. There is **no
  runtime fallback** — if Gemini fails, the tender is recorded as `TBD(AI)`.
- **Google Sheets/Drive:** `google-api-python-client` + `google-auth` (service-account auth).
- **Email alerts:** Python standard library only (`smtplib` + `email`) via
  `notifier.py`; no extra dependency. Each run sends one HTML summary email.
- **Other:** `requests`, `beautifulsoup4`, `pytz`, `holidays`.

## Run Commands

```bash
# Install dependencies
pip install -r requirements.txt

# From the project root:
python -m analyzer.main                     # analyse every PreQualified/ReCheck row
python -m analyzer.main --limit 5           # cap to the first 5 qualifying rows (quick test)

# Knowledge maintenance — separate cadence, NOT per analysis run:
python -m analyzer.maintain_knowledge          # extract + distil (respects the data guards)
python -m analyzer.maintain_knowledge --force  # distil below the example minimums (testing)
```

There is no automated test suite yet; `--limit` is the quick-check mechanism.

## Required Local Setup (not in git)

- `credentials/service_account.json` — Google service-account key; share the target
  Drive folder / sheet with its email as Editor.
- `credentials/gemini_credentials.json` — `{ "gemini_api_key": "..." }` (active provider).
- `credentials/openrouter_credentials.json` — `{ "openrouter_api_key": "..." }` (backup only).
- `analyzer/knowledge/onepoint_capabilities.md` — capability context injected into the
  prompt; scoring is grounded **only** on this file.
- `credentials/smtp_credentials.json` — `{ "username": "...", "password": "..." }` for
  SMTP relays that require auth (e.g. AWS SES). Omit the file entirely for an
  unauthenticated internal relay.
- `project_config.json` — `google_sheets` (`sheet_name`, `target_folder_id`,
  `environment`), a `notifications` block (see Email notifications below), and a
  `google_drive_locations` block (`Source_Docs`, `Tender_Docs`,
  `Analysis_Reports`) giving the Drive folders the DetailedAnalyzer reads and
  publishes its briefs to. None of the three has a **default** —
  `config.drive_location()` raises if any is missing. Drive reports a wrong
  folder ID and an empty folder identically, so a silent fallback would produce
  a brief that looks complete on no documents at all — or publish every brief
  into a folder nobody is reading.

See `SETUP.md` for the full step-by-step (service account, sheet sharing, etc.).

## Architecture

Entry point is `analyzer/main.py`, which orchestrates one run:

1. `sheets_client.py` — opens the sheet (service-account auth) and reads tender rows.
2. Row selection — `Bid Qualification` in `PreQualified`/`ReCheck` is the analyzer's
   **single entry point**, applied once before the loop (`should_analyse()`), so
   manual overrides and already-qualified rows are dropped there and never reach
   the loop, the model, or the per-row log. **There is no date filter** — a row
   stays in scope until it has a qualification, so one missed by a failed or
   skipped run is picked up by the next. Nothing bounds run size except `--limit`,
   which is manual; see the note in `analyzer/config.py`.
3. `analyzer.py` — `analyze_tender(title, description, nobid_patterns=…, bid_patterns=…)`
   builds a prompt from the Onepoint context (`onepoint_context.py`) plus the
   distilled decision precedent (`patterns.py`, loaded once per run in `main.py`),
   calls Gemini, and maps the returned score (0–100) to `Bid(AI)` / `TBD(AI)` /
   `NoBid(AI)` via thresholds in `config.py`. Retries transient/incomplete replies up
   to `ANALYZER_MAX_RETRIES`; on total failure returns a deterministic `TBD(AI)`
   flagged with `analysis_failed=True` (never `NoBid(AI)` — the tender was never
   scored, so NoBid would assert a judgement that was never made and bury the row;
   TBD means undetermined and surfaces in the email's attention total). The flag
   makes `main._build_row_update()` write "not scored — analysis failed" in place
   of the placeholder `score 0/100`. Empty input still returns `NoBid(AI)`.
4. Write-back — `main.py` writes `Bid Qualification`, prepends the dated reason
   to `Bid Qualification Reason(System)` (newest first; prior runs kept below),
   writes `Bid Qualification Date`, appends a `Comments` entry, updates the
   control columns, and colours each changed row. `Bid Qualification
   Reason(Human)` is never written — manual notes are preserved.
5. Email alert — after the run (success or failure), `main.py` builds an HTML
   summary via `_build_report()` and sends it through `notifier.send_alert()`.

Config layering: root `config.py` holds shared settings (column schema, sheet/folder,
credential paths, UK timezone, `NOTIFICATIONS`); `analyzer/config.py` re-exports those
and adds the analyzer-specific settings (model, thresholds, retries, eligible
statuses, thinking budget).

## Bid knowledge maintenance

`analyzer/maintain_knowledge.py` is a **separate, scheduled flow** — not part of an
analysis run. It turns the team's manual decisions into decision precedent in two
steps, both iterating the `KNOWLEDGE_SOURCES` table in `analyzer/config.py` so each
polarity runs through identical code:

1. **Extract** — one read of the sheet, then `sync_matching_to_tab()` per source:
   `NoBid(Human)` → `PS NoBids`, `Bid(Human)` → `PS Bids`. Deduped by ID / OCID /
   Direct URL / Name; idempotent. Columns are matched to each tab's row-1 header
   **by name**; a header-less tab is skipped with a warning. **The tracker is the
   source of truth** — a tender in both is rewritten from the tracker, including
   with a blank, so an edit made in a tab does not survive the next sync. Tab rows
   whose tender no longer matches the status are kept as history.
2. **Distil** — one Gemini call per source, consolidating that tab's
   `Bid Qualification Reason(Human)` notes into general heuristics →
   `knowledge/nobid_patterns.md` and `knowledge/bid_patterns.md` (both gitignored).

Invariants worth preserving when changing this code:

- **Only human-set statuses feed it** — never `Bid(AI)`/`NoBid(AI)`, or the analyzer
  starts learning from its own output.
- **Sources are fully independent** — own guard, own LLM call, own file, own error
  handling. One polarity's failure or data shortfall must never touch the other's
  artifact.
- **Guards protect existing files.** A source regenerates only with ≥ its
  `min_examples` distinct genuine reasons (`NOBID_MIN_EXAMPLES` 5, `BID_MIN_EXAMPLES`
  3; junk filtered by `genuine_reasons()`). `--force` bypasses the minimums but a
  source with **zero** reasons is still skipped — there is nothing to distil.
- **The capability wall.** Precedent is injected as a *separate* prompt block framed
  as decision precedent, never merged into the capability context. Capability is
  judged only from `onepoint_capabilities.md`, which is hand-authored and tracked in
  git — no generated content may overwrite it.
- **Phrasing is behaviour, not cosmetics.** Each source pins a `verb` (`Decline` /
  `Pursue`) that the distillation prompt requires every generated bullet to open
  with. An imperative reads to the scoring call as a constraint; a description reads
  as a tendency it may trade off — measured at 10-15 score points on the same rules
  (see the note above `KNOWLEDGE_SOURCES`). Don't relax these to softer forms like
  "Give low priority to…", which measured *weaker* than plain description and put a
  blocked tender one point below the Bid threshold.

Both artifacts are injected into the analysis prompt. `analyzer/patterns.py` loads
each (one path-keyed cache, `load_nobid_patterns()` / `load_bid_patterns()`),
`analyzer.main` loads them once per run and threads both into every
`analyze_tender()` call, and `_build_prompt()` emits one fenced block per polarity —
NoBid instructing "calibrate the score DOWN", Bid "calibrate the score UP". A block
is omitted entirely when its file is absent/empty, so deleting a patterns file
disables that direction cleanly.

**Open decision:** there is no conflict rule. A tender matching both a Decline and a
Pursue heuristic is resolved however the model weighs them on that call, so its
qualification can vary between runs. The recommended fix is one prompt line making
documented blockers (geography, clearance, out-of-scope) win over appetite — not yet
agreed, so don't add it unasked.

**Watch the inflation direction.** `Bid > 75` is the expensive threshold to cross by
mistake: a false `Bid(AI)` costs bid-team time, whereas a false `NoBid(AI)` can still
be recovered via `ReCheck`. When changing the Bid block's wording or the thresholds,
A/B the current `TBD(AI)` rows (the ones near the boundary) rather than clear misses —
poor-fit tenders show no difference either way and prove nothing.

## Email notifications

Every run sends exactly one HTML summary email, colour-coded by outcome:

- ✅ **SUCCESS** (green) — run completed with zero row errors.
- ⚠️ **COMPLETED WITH ERRORS** (amber) — run finished but ≥1 row hit an
  analyzer error (`summary['errors'] > 0`).
- ❌ **FAILURE** (red) — the run aborted with an exception; the email embeds the
  traceback and the process still exits non-zero (so schedulers register it).

The body reports environment, start/finish times and the run's scope (every row
awaiting qualification), then two tables:

- **Overall Summary** — sheet-wide Bid and TBD totals across *every* row in the
  tracker (not just this run), rolled up by qualification family so `Bid(AI)`,
  `Bid(Human)` and bare `Bid` count together, plus a "Tenders Needing your
  Attention" total. Counted in `run()` off the rows already read — no extra API
  call — with this run's new qualifications substituted in so the totals reflect
  the sheet after write-back. Omitted entirely on a fatal error, where no totals
  were produced (better than showing a misleading 0).
- **Bid Analysis - This Run** — the per-run metrics (Eligible / Analysed / Bid /
  TBD / NoBid / Skipped / Errors). `Eligible` leads because it is the run's scope;
  `Analysed` below it shows how much of that scope was got through, the two
  differing when `--limit` is set or a row has no title/description.

The sheet hyperlink sits between them. The sheet-wide figures are also written to
the run log so a run is auditable without opening the email.

Configuration lives in the `notifications` block of `project_config.json`
(exposed as `config.NOTIFICATIONS`):

```json
"notifications": {
  "enabled": true,
  "smtp_host": "email-smtp.eu-west-1.amazonaws.com",
  "smtp_port": 587,
  "use_starttls": true,
  "use_ssl": false,
  "from_address": "ps-no-reply@onepointltd.com",
  "recipients": ["arivazhagan.mani@onepointltd.com"]
}
```

Set `"enabled": false` to disable alerts. SMTP auth (if the relay needs it) is
read from `credentials/smtp_credentials.json`. `notifier.send_alert()` never
raises — a broken mailer will not bring down an analysis run. This mirrors the
notifier in the upstream **PS-WebScrapper** module.

## Key Files

| File | Role |
|------|------|
| `analyzer/main.py` | Entry point — read sheet → select → analyse → write back |
| `analyzer/analyzer.py` | `analyze_tender()` — prompt, Gemini call, score → qualification |
| `analyzer/maintain_knowledge.py` | Standalone knowledge maintenance — extract human decisions → distil heuristics |
| `analyzer/patterns.py` | Loads the distilled Bid/NoBid precedent (cached per path, graceful when absent) |
| `analyzer/gemini_client.py` | Native Gemini client wrapper (active provider) |
| `analyzer/openrouter_client.py` | OpenRouter/OpenAI wrapper (backup, unused) |
| `analyzer/onepoint_context.py` | Loads the Onepoint capability context |
| `analyzer/sheets_client.py` | Google Sheets read/write + row colouring |
| `analyzer/config.py` | Analyzer settings (provider/model, thresholds, retries, eligible statuses) |
| `config.py` | Shared config — column schema, sheet/folder, credential paths, `NOTIFICATIONS` |
| `notifier.py` | Stdlib SMTP email transport (`send_alert`); alert body built in `main.py` |

## Output

- Updated `PS Tender Tracker` sheet in the configured Drive folder.
- One HTML summary email per run (see Email notifications).
- Run log: `analyzer/analyzer.log` (also echoed to console).
