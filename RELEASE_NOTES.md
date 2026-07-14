# PS BidAnalyzer Tool — Release Notes

---

## v1.0.0 — 2026-07-08

**Initial release of the PS BidAnalyzer Tool — an LLM-based bid qualification stage for Onepoint's PS tender pipeline.**

---

### Overview

PS BidAnalyzer is a Python tool that reads tenders from the shared **PS Tender Tracker** Google Sheet, scores each tender's fit against Onepoint's documented capabilities using an OpenRouter-hosted model, and writes back a **Bid / NoBid / TBD** qualification with a system-generated reason. It only touches rows the automated pipeline has flagged for analysis, so human decisions in the sheet are never overwritten.

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
- Default model `google/gemini-2.5-flash` via OpenRouter (`ANALYZER_MODEL`); `Requirements.md` specifies `google/gemini-3.5-flash` — switch in one place when available on the account
- Detects OpenRouter transient failures (HTTP 200 with `finish_reason` = `error`/`length` and a truncated/unparseable body), rejects the incomplete response, and retries up to `ANALYZER_MAX_RETRIES` (default 3, spaced by `API_THROTTLE_SECONDS`) before falling back to `NoBid(AI)`

#### Google Sheet I/O
- Authenticates via a Google **service account** (`credentials/service_account.json`); the target Drive folder/sheet must be shared with the service account email
- Sheet layout: row 1 = summary, row 2 = headers, row 3+ = tender data; the tool locates the sheet by name in the configured folder and does **not** create it
- **Writes:** `Bid Qualification`, `Bid Qualification Reason`, `Bid Qualification Date`, `Comments` (appends a timestamped entry), `Processed Date`, `Last Modified Date`, `Created Date`
- Sheets/Drive calls retry with exponential backoff + jitter (up to 6 attempts, capped at 120 s)

#### Output & Logging
- Logs to the console and to `analyzer/analyzer.log`
- End-of-run summary: analysed, Bid, TBD, NoBid, skipped, out-of-window, and errors

---

### Configuration Reference (`analyzer/config.py`)

| Setting | Default | Purpose |
|---------|---------|---------|
| `ANALYZER_MODEL` | `google/gemini-2.5-flash` | OpenRouter model used for scoring |
| `ANALYZER_TEMPERATURE` | `0.2` | Sampling temperature |
| `ANALYZER_MAX_TOKENS` | `700` | Max completion tokens |
| `ANALYZER_MAX_RETRIES` | `3` | Retries on transient/incomplete responses |
| `API_THROTTLE_SECONDS` | `10` | Delay between API calls / retries |
| `BID_THRESHOLD` | `75` | Score strictly above → Bid |
| `TBD_THRESHOLD` | `51` | Score at/above (and ≤ 75) → TBD; below → NoBid |
| `PROCESS_STATUSES` | `{PreQualified, ReCheck}` | Which statuses are analysed |
| `WINDOW_DATE_FIELD` | `Last Modified Date` | Column anchoring the one-day window |
| `ONEPOINT_CONTEXT_FILE` | `knowledge/onepoint_capabilities.md` | Capability context path |

Sheet name, Drive folder ID, and environment live in `project_config.json` (read by the root `config.py`).

---

### Architecture

```
project_config.json               Sheet name, Drive folder ID, environment
config.py                         Shared config — column schema, sheet/folder, credentials paths
credentials/
  service_account.json            Google service account key (you provide)
  openrouter_credentials.json     { "openrouter_api_key": "..." } (you provide)
analyzer/
  main.py                         Entry point — read sheet → analyse → write back
  analyzer.py                     Core analyze_tender(); prompt + score → qualification
  openrouter_client.py            OpenRouter/OpenAI client wrapper
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
- Default model is `google/gemini-2.5-flash`; `google/gemini-3.5-flash` (per `Requirements.md`) must be enabled on the OpenRouter account before switching
- The sheet must already exist and be populated by the upstream process — the tool does not create or scrape it
- No built-in scheduler or UI; runs are manual or externally scheduled

---

### Setup Requirements

- Python 3.x with dependencies listed in `requirements.txt`
- Google Cloud service account with Sheets + Drive access, key at `credentials/service_account.json`, shared with the sheet's Drive folder
- `credentials/openrouter_credentials.json` containing `{"openrouter_api_key": "..."}`
- `project_config.json` pointing at the correct Drive folder + sheet name
- `analyzer/knowledge/onepoint_capabilities.md` populated with Onepoint's capabilities
- See `SETUP.md` for full installation and first-run instructions
