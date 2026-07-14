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
  runtime fallback** — if Gemini fails, the tender is recorded as `NoBid(AI)`.
- **Google Sheets/Drive:** `google-api-python-client` + `google-auth` (service-account auth).
- **Email alerts:** Python standard library only (`smtplib` + `email`) via
  `notifier.py`; no extra dependency. Each run sends one HTML summary email.
- **Other:** `requests`, `beautifulsoup4`, `pytz`, `holidays`.

## Run Commands

```bash
# Install dependencies
pip install -r requirements.txt

# From the project root:
python -m analyzer.main                     # analyse today's window (UK time)
python -m analyzer.main --date 2026-07-03   # analyse a specific day (backfill / rerun)
python -m analyzer.main --limit 5           # cap to the first 5 qualifying rows (quick test)
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
  `environment`) plus a `notifications` block (see Email notifications below).

See `SETUP.md` for the full step-by-step (service account, sheet sharing, etc.).

## Architecture

Entry point is `analyzer/main.py`, which orchestrates one run:

1. `sheets_client.py` — opens the sheet (service-account auth) and reads tender rows.
2. Row selection — only rows whose `Bid Qualification` is `PreQualified`/`ReCheck`
   **and** whose `Last Modified Date` falls in the target one-day window are analysed
   (skips manual overrides and already-processed rows).
3. `analyzer.py` — `analyze_tender(title, description)` builds a prompt from the
   Onepoint context (`onepoint_context.py`), calls Gemini, and maps the returned
   score (0–100) to `Bid(AI)` / `TBD(AI)` / `NoBid(AI)` via thresholds in
   `config.py`. Retries transient/incomplete replies up to `ANALYZER_MAX_RETRIES`;
   on total failure returns a deterministic `NoBid(AI)`.
4. Write-back — `main.py` writes `Bid Qualification`, prepends the dated reason
   to `Bid Qualification Reason(System)` (newest first; prior runs kept below),
   writes `Bid Qualification Date`, appends a `Comments` entry, updates the
   control columns, and colours each changed row. `Bid Qualification
   Reason(Human)` is never written — manual notes are preserved.
5. Email alert — after the run (success or failure), `main.py` builds an HTML
   summary via `_build_report()` and sends it through `notifier.send_alert()`.

Config layering: root `config.py` holds shared settings (column schema, sheet/folder,
credential paths, UK timezone, `NOTIFICATIONS`); `analyzer/config.py` re-exports those
and adds the analyzer-specific settings (model, thresholds, retries, window, thinking
budget).

## Email notifications

Every run sends exactly one HTML summary email, colour-coded by outcome:

- ✅ **SUCCESS** (green) — run completed with zero row errors.
- ⚠️ **COMPLETED WITH ERRORS** (amber) — run finished but ≥1 row hit an
  analyzer error (`summary['errors'] > 0`).
- ❌ **FAILURE** (red) — the run aborted with an exception; the email embeds the
  traceback and the process still exits non-zero (so schedulers register it).

The body reports environment, start/finish times, the one-day window date, and
the full summary table (Analysed / Bid / TBD / NoBid / Skipped / Out of window /
Errors).

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
| `analyzer/gemini_client.py` | Native Gemini client wrapper (active provider) |
| `analyzer/openrouter_client.py` | OpenRouter/OpenAI wrapper (backup, unused) |
| `analyzer/onepoint_context.py` | Loads the Onepoint capability context |
| `analyzer/sheets_client.py` | Google Sheets read/write + row colouring |
| `analyzer/config.py` | Analyzer settings (provider/model, thresholds, retries, window) |
| `config.py` | Shared config — column schema, sheet/folder, credential paths, `NOTIFICATIONS` |
| `notifier.py` | Stdlib SMTP email transport (`send_alert`); alert body built in `main.py` |

## Output

- Updated `PS Tender Tracker` sheet in the configured Drive folder.
- One HTML summary email per run (see Email notifications).
- Run log: `analyzer/analyzer.log` (also echoed to console).
