# PS BidAnalyzer Tool — Release Notes

---

## Unreleased

**Bid-side knowledge collection + email summary refinements.**

### Bid knowledge maintenance
- `analyzer/maintain_nobids.py` is renamed **`analyzer/maintain_knowledge.py`** — it now maintains both polarities, so the old name was misleading. Update any scheduled job that invokes the module path.
- Both maintenance steps are driven by a single `KNOWLEDGE_SOURCES` table in `analyzer/config.py`, so each polarity runs through identical code; adding a third is one table entry.
  - **Step 1 — extract:** one read of the sheet now feeds every polarity — `NoBid(Human)` → **PS NoBids** and `Bid(Human)` → **PS Bids** (deduped, idempotent). Requires a `bids_tab_name` tab with a row-1 header; a header-less tab is skipped with a warning.
  - **Step 2 — distil:** one Gemini call **per polarity**, each writing its own artifact — `knowledge/nobid_patterns.md` and the new `knowledge/bid_patterns.md` (both gitignored).
- Sources are fully independent: own guard, own LLM call, own file, own error handling. One polarity failing or lacking data never touches the other's artifact.
- `BID_MIN_EXAMPLES` (3) is lower than `NOBID_MIN_EXAMPLES` (5) — human Bid decisions are rarer than rejections. `--force` now bypasses the minimum for **every** source, but a source with **zero** genuine reasons is still skipped: there is nothing to send the model.
- Generalised the polarity-agnostic helpers: `genuine_nobid_reasons` → `genuine_reasons`, `NOBID_JUNK_MARKERS` → `JUNK_MARKERS`, `NOBID_DISTILL_*` → `DISTILL_*`.
### Bid precedent in scoring
- `bid_patterns.md` is now **injected into the analysis prompt** as a second precedent block, instructing the model to calibrate the score **UP** when a tender clearly matches a past Bid decision — mirroring the existing NoBid block's DOWN instruction. Like the NoBid block it is fenced and explicitly **not** a capability source: the Bid framing adds "never treat them as extending Onepoint's capabilities", since a human note like "strong fit, existing client" is evidence of commercial appetite, not of what Onepoint can deliver.
- `analyzer/nobid_patterns.py` is generalised to **`analyzer/patterns.py`** — one path-keyed cache behind `load_nobid_patterns()` / `load_bid_patterns()`, instead of two near-identical loaders. Update any direct import of the old module.
- `analyze_tender()` and `_build_prompt()` take an additional `bid_patterns` argument; `main.py` loads both artifacts once per run and threads them into every call. Each block is omitted entirely when its file is absent or empty, so deleting a patterns file cleanly disables that direction and a fresh environment with neither file behaves exactly as before.

**Known gap:** there is no conflict rule. A tender matching both a Decline and a Pursue heuristic is resolved however the model weighs them on that call, so its qualification can vary between runs. Making documented blockers (geography, clearance, out-of-scope) win over appetite is a one-line prompt change, not yet agreed.

### Summary email
- Adds an **Overall Summary** table above the sheet link: sheet-wide Bid and TBD totals across the whole tracker (rolled up by family, so `Bid(AI)`/`Bid(Human)`/`Bid` count together) plus a **Tenders Needing your Attention** total. Counted off the rows already read — no extra API call — and reflecting the sheet after write-back. Omitted on a fatal run rather than shown as 0.
- The existing per-run metrics table is now headed **Bid Analysis - This Run**.
- Sheet-wide totals are also written to the run log.

---

## v1.1.0 — 2026-07-22

**Consolidated NoBid precedent + summary-email refinements.**

### NoBid knowledge maintenance (new)
- Adds a standalone, scheduled maintenance flow (`analyzer/maintain_nobids.py`), **decoupled** from the per-tender analysis, that distils past human NoBid decisions into reusable heuristics and injects them into scoring:
  - **Step 1 — extract:** sync `NoBid(Human)` rows from the main tab into the **PS NoBids** tab (deduped).
  - **Step 2 — distil:** consolidate the human `Bid Qualification Reason(Human)` notes into general NoBid heuristics via one Gemini call → `analyzer/knowledge/nobid_patterns.md`.
- Guarded: Step 2 skips regeneration (keeps the existing file) unless there are ≥ `NOBID_MIN_EXAMPLES` (default 5) distinct genuine reasons; test/placeholder junk is filtered out. `--force` bypasses the guard.
- Only `NoBid(Human)` reasons feed the distillation — never `NoBid(AI)` — to avoid a self-reinforcing feedback loop.
- During analysis the distilled file (if present) is injected as **decision precedent**; capability is still judged only from the capability context. `nobid_patterns.py` loads it (cached, graceful when absent). The generated `nobid_patterns.md` is gitignored.

### Summary email
- The run summary email now includes a hyperlink to the **PS Tender Tracker** sheet.

### Housekeeping
- The `NoBid(Human)` → PS NoBids sync previously run at the end of each analysis was relocated into the maintenance flow.
- Removed the `genai_list_models.py` dev utility.

---

## v1.0.0 — 2026-07-14

**Initial release of the PS BidAnalyzer Tool — an LLM-based bid qualification stage for Onepoint's PS tender pipeline.**

---

### Overview

PS BidAnalyzer is a Python tool that reads tenders from the shared **PS Tender Tracker** Google Sheet, scores each tender's fit against Onepoint's documented capabilities using Google's **Gemini** model, and writes back a **Bid / NoBid / TBD** qualification with a system-generated reason. It only touches rows the automated pipeline has flagged for analysis, so human decisions in the sheet are never overwritten.

The analyzer consumes a sheet populated by an upstream process — it does not scrape or create the sheet itself.

---

### Features

#### Bid Qualification Pipeline
- Reads `Name` (title) and `Tender Description` for each qualifying tender from the sheet
- Sends title + description to a **Tender Analyst** model, grounded strictly on Onepoint's capability context
- Receives an overall fit **score out of 100** plus a short justification
- Maps the score to a qualification and writes it back with a system-generated reason and date

#### Scoring & Qualification Labels
| Score | Qualification | Label written | Row colour |
|-------|---------------|---------------|------------|
| > 75  | **Bid**   | `Bid(AI)`   | white  |
| 51–75 | **TBD**   | `TBD(AI)`   | yellow |
| ≤ 50  | **NoBid** | `NoBid(AI)` | red    |

- Thresholds configurable via `BID_THRESHOLD` (75) and `TBD_THRESHOLD` (51) in `analyzer/config.py`
- On empty input or an unrecoverable API failure, a deterministic `NoBid(AI)` is recorded with an explanatory reason, so every processed row always gets a result

#### Row Selection — System vs. Manual
- Only rows whose `Bid Qualification` holds a **system-assigned** value are analysed (`PROCESS_STATUSES = {PreQualified, ReCheck}`)
- `PreQualified` → system-qualified upstream, awaiting a bid decision
- `ReCheck` → auto-flagged for re-evaluation after a change
- `NotQualified` / blank → not a bid candidate; skipped
- `Bid(AI)` / `TBD(AI)` / `NoBid(AI)` → already analysed; skipped
- **Any human-set value (Won, Lost, hand-typed NoBid, …) is preserved** — since `PreQualified`/`ReCheck` are only ever written by automated steps, any other value implies a human touched the row

#### One-Day Processing Window
- Processes only rows dated within a **single day**, anchored on the `Last Modified Date` column (`WINDOW_DATE_FIELD`)
- Defaults to **today** (UK time); `--date YYYY-MM-DD` targets another day for backfill/rerun
- Rows outside the window are counted as `Out of window` and never sent to the model

#### Onepoint Capability Grounding
- Scoring is grounded **only** on `analyzer/knowledge/onepoint_capabilities.md`
- The model is instructed never to assume capabilities beyond this file
- If the file is missing or empty, analysis still runs but logs a warning and produces low-confidence scores

#### Model & Resilience
- Active provider is Google **Gemini** via the native `google-genai` SDK (`analyzer/gemini_client.py`); model `gemini-3.1-flash-lite` (`GEMINI_MODEL` / `ANALYZER_MODEL`)
- Thinking is disabled (`ANALYZER_THINKING_BUDGET = 0`) because Gemini 3.x draw reasoning tokens from the output budget, which truncated the JSON reply
- There is **no** runtime fallback provider — if Gemini fails, the tender is recorded as `NoBid(AI)`. The OpenRouter wrapper (`analyzer/openrouter_client.py`) and its `OPENROUTER_*` settings are retained only as a backup/reference and are not imported by the active path
- Detects empty/truncated/unparseable replies, rejects them, and retries up to `ANALYZER_MAX_RETRIES` (default 3, spaced by `API_THROTTLE_SECONDS`) before falling back to `NoBid(AI)`

#### Google Sheet I/O
- Authenticates via a Google **service account** (`credentials/service_account.json`); the target Drive folder/sheet must be shared with the service account email
- Sheet layout: row 1 = summary, row 2 = headers, row 3+ = tender data; the tool locates the sheet by name in the configured folder and does **not** create it
- **Writes:** `Bid Qualification`; `Bid Qualification Reason(System)` (dated reason prepended, newest first); `Bid Qualification Date`; `Comments` (appends a timestamped entry); `Processed Date`; `Last Modified Date`; `Created Date`. `Bid Qualification Reason(Human)` is never written, preserving manual notes
- Sheets/Drive calls retry with exponential backoff + jitter (up to 6 attempts, capped at 120 s)

#### Email Notifications
- Every run sends exactly one HTML summary email, colour-coded by outcome: ✅ SUCCESS (green), ⚠️ COMPLETED WITH ERRORS (amber), ❌ FAILURE (red, embeds the traceback and exits non-zero)
- Body reports environment, start/finish times, the window date, and the full summary table
- Configured via the `notifications` block of `project_config.json` (`config.NOTIFICATIONS`); SMTP auth read from `credentials/smtp_credentials.json` when the relay needs it
- `notifier.send_alert()` never raises — a broken mailer will not bring down an analysis run

#### Output & Logging
- Logs to the console and to `analyzer/analyzer.log`
- End-of-run summary: analysed, Bid, TBD, NoBid, skipped, out-of-window, and errors

---

### Configuration Reference (`analyzer/config.py`)

| Setting | Default | Purpose |
|---------|---------|---------|
| `GEMINI_MODEL` / `ANALYZER_MODEL` | `gemini-3.1-flash-lite` | Native Gemini model used for scoring |
| `ANALYZER_TEMPERATURE` | `0.2` | Sampling temperature |
| `ANALYZER_MAX_TOKENS` | `700` | Max output tokens |
| `ANALYZER_THINKING_BUDGET` | `0` | Gemini thinking budget (disabled for reliable JSON) |
| `ANALYZER_MAX_RETRIES` | `3` | Retries on transient/incomplete responses |
| `API_THROTTLE_SECONDS` | `10` | Delay between API calls / retries |
| `BID_THRESHOLD` | `75` | Score strictly above → Bid |
| `TBD_THRESHOLD` | `51` | Score at/above (and ≤ 75) → TBD; below → NoBid |
| `PROCESS_STATUSES` | `{PreQualified, ReCheck}` | Which statuses are analysed |
| `WINDOW_DATE_FIELD` | `Last Modified Date` | Column anchoring the one-day window |
| `ONEPOINT_CONTEXT_FILE` | `knowledge/onepoint_capabilities.md` | Capability context path |

Sheet name, Drive folder ID, environment, and the `notifications` block live in `project_config.json` (read by the root `config.py`).

---

### Architecture

```
project_config.json               Sheet name, Drive folder ID, environment, notifications block
config.py                         Shared config — column schema, sheet/folder, credential paths, NOTIFICATIONS
notifier.py                       Stdlib SMTP email transport (send_alert)
credentials/
  service_account.json            Google service account key (you provide)
  gemini_credentials.json         { "gemini_api_key": "..." } — active provider (you provide)
  openrouter_credentials.json     { "openrouter_api_key": "..." } — backup, unused (optional)
  smtp_credentials.json           { "username": "...", "password": "..." } — only if the relay needs auth
analyzer/
  main.py                         Entry point — read sheet → analyse → write back → email
  analyzer.py                     Core analyze_tender(); prompt + score → qualification
  gemini_client.py                Native Gemini client wrapper (active provider)
  openrouter_client.py            OpenRouter/OpenAI client wrapper (backup, unused)
  onepoint_context.py             Loads the Onepoint capability context
  sheets_client.py                Google Sheets read/write + row colouring
  config.py                       Analyzer settings (model, thresholds, retries, window)
  knowledge/
    onepoint_capabilities.md      Capability context injected into the prompt (you populate)
```

---

### Usage

```bash
python -m analyzer.main                     # analyse today's window (UK time)
python -m analyzer.main --date 2026-07-03   # analyse a specific day (backfill / rerun)
python -m analyzer.main --limit 5           # cap to the first 5 qualifying rows (quick test)
```

---

### Known Limitations

- Scoring quality depends entirely on `analyzer/knowledge/onepoint_capabilities.md`; if unpopulated, scores are low-confidence
- No runtime fallback provider — if Gemini is unavailable, affected rows are recorded as `NoBid(AI)` for manual review
- The sheet must already exist and be populated by the upstream process — the tool does not create or scrape it
- No built-in scheduler or UI; runs are manual or externally scheduled

---

### Setup Requirements

- Python 3.10+ (developed on 3.14) with dependencies listed in `requirements.txt`
- Google Cloud service account with Sheets + Drive access, key at `credentials/service_account.json`, shared with the sheet's Drive folder
- `credentials/gemini_credentials.json` containing `{"gemini_api_key": "..."}`
- `project_config.json` pointing at the correct Drive folder + sheet name, with a `notifications` block
- `analyzer/knowledge/onepoint_capabilities.md` populated with Onepoint's capabilities
- See `SETUP.md` for full installation and first-run instructions
