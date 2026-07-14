# PS BidAnalyzer Tool

An LLM-based **bid qualification** tool for Onepoint. It reads tenders from the shared **PS Tender Tracker** Google Sheet, scores each tender's fit against Onepoint's documented capabilities using an OpenRouter-hosted model, and writes back a **Bid / NoBid / TBD** qualification with a system-generated reason.

**Lead:** PS Team

---

## What it does

For every qualifying tender in the sheet, the analyzer:

1. Sends the tender **title** + **description** to a Tender Analyst model, grounded strictly on Onepoint's capability context.
2. Gets back an overall fit **score out of 100** and a short justification.
3. Maps the score to a qualification — **Bid (> 75)**, **TBD (51–75)**, or **NoBid (≤ 50)**.
4. Writes the qualification, reason, and date back to the sheet, appends a timestamped audit comment, and colours the row.

It only touches rows the automated pipeline has flagged for analysis, so human decisions in the sheet are never overwritten.

> **Note:** this project previously included a multi-adapter scraper (`orchestrator.py`, `adapters/`) that populated the sheet. That layer has been removed — the analyzer now consumes a sheet populated by an upstream/external process.

---

## Architecture

```
project_config.json ──▶ SheetsClient.read_tenders()      (Google service account)
                              │   Name = title, Tender Description = description
                              ▼
             filter: Bid Qualification ∈ {PreQualified, ReCheck}
             AND Last Modified Date within the one-day window
                              │   (skips manual overrides & already-analysed rows)
                              ▼
                     analyze_tender(title, description)
                              │   OpenRouter · Gemini · Onepoint capability context
                              │   retries on transient upstream errors
                              ▼
             score 0-100 ──▶ Bid (>75) / TBD (51-75) / NoBid (≤50)
                              ▼
                   SheetsClient.write_qualifications() + row colour
   updates: [Bid Qualification] [Bid Qualification Reason] [Bid Qualification Date]
            [Comments] [Processed Date] [Last Modified Date] [Created Date]
```

---

## Project structure

```
config.py                         Shared config — column schema, sheet/folder, credentials paths
project_config.json               Sheet name, Drive folder ID, environment
requirements.txt                  Python dependencies
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

## Quick start

**1. Install dependencies**
```bash
pip install -r requirements.txt
```

**2. Set up credentials and config** — see [SETUP.md](SETUP.md) for full instructions:
- `credentials/service_account.json` (Google service account with access to the sheet's Drive folder)
- `credentials/openrouter_credentials.json` containing `{"openrouter_api_key": "..."}`
- `project_config.json` pointing at the correct Drive folder + sheet name
- `analyzer/knowledge/onepoint_capabilities.md` populated with Onepoint's capabilities

**3. Run the analyzer**
```bash
python -m analyzer.main                     # analyse today's window (UK time)
python -m analyzer.main --date 2026-07-03   # analyse a specific day (backfill / rerun)
python -m analyzer.main --limit 5           # cap to the first 5 qualifying rows (quick test)
```

---

## Which rows get analysed

Only rows whose `Bid Qualification` holds a **system-assigned** value are analysed (`PROCESS_STATUSES` in `analyzer/config.py`):

| `Bid Qualification` value | Analysed? | Why |
|---------------------------|-----------|-----|
| `PreQualified` | ✅ | System-qualified upstream, awaiting a bid decision |
| `ReCheck` | ✅ | Auto-flagged for re-evaluation after a change |
| `NotQualified`, blank | ❌ | Not a bid candidate |
| `Bid(AI)` / `TBD(AI)` / `NoBid(AI)` | ❌ | Already analysed by this tool |
| Any human-set value (Won, Lost, hand-typed NoBid, …) | ❌ | **Manual override — preserved** |

`PreQualified` / `ReCheck` are only ever written by automated steps, so any other value implies a human touched the row and is left alone.

---

## One-day window

The analyzer only processes rows dated within a **single day**, anchored on the `Last Modified Date` column (`WINDOW_DATE_FIELD` in `analyzer/config.py`). The window defaults to **today** (UK time); pass `--date YYYY-MM-DD` to target another day. Rows outside the window are counted as `Out of window` and never sent to the model.

---

## Scoring & qualification labels

Score thresholds (`score_to_qualification` in `analyzer/config.py`):

| Score | Qualification | Label written | Row colour |
|-------|---------------|---------------|------------|
| > 75  | **Bid**   | `Bid(AI)`   | white  |
| 51–75 | **TBD**   | `TBD(AI)`   | yellow |
| ≤ 50  | **NoBid** | `NoBid(AI)` | red    |

On empty input or an unrecoverable API failure, the analyzer records a deterministic `NoBid(AI)` with a reason explaining why, so every processed row always gets a result.

---

## The Onepoint capability context

Scoring is grounded **only** on `analyzer/knowledge/onepoint_capabilities.md`. The model is instructed never to assume capabilities beyond this file. If the file is missing or empty, analysis still runs but logs a warning and produces low-confidence scores — so populate it with Onepoint's real capabilities, past performance, accreditations, and target markets (see `Requirements.md`).

---

## Model & resilience

- **Model:** `ANALYZER_MODEL` in `analyzer/config.py` (default `google/gemini-2.5-flash`), via OpenRouter. `Requirements.md` specifies `google/gemini-3.5-flash`; change `ANALYZER_MODEL` in one place if/when that model is available on your account.
- **Transient-error handling:** OpenRouter occasionally returns HTTP 200 with `finish_reason` = `error` or `length` and a truncated body that fails JSON parsing. The analyzer detects this, rejects the incomplete response, and retries up to `ANALYZER_MAX_RETRIES` times (default 3, spaced by `API_THROTTLE_SECONDS`) before falling back to `NoBid(AI)`.

---

## Google Sheet I/O

- **Auth:** Google **service account** (`credentials/service_account.json`). Share the target Drive folder / sheet with the service account's email.
- **Sheet layout:** row 1 = summary, row 2 = headers, row 3+ = tender data. The tool locates the sheet by name in the configured folder — it does **not** create it, so the sheet must already exist and be populated.
- **Reads:** `Name` (title) and `Tender Description`, plus the status and date columns used for filtering.
- **Writes:** `Bid Qualification`, `Bid Qualification Reason`, `Bid Qualification Date`, `Comments` (appends a timestamped entry), `Processed Date`, `Last Modified Date`, `Created Date`.
- **Rate limits:** Sheets/Drive calls retry with exponential backoff + jitter (up to 6 attempts, capped at 120 s).

---

## Configuration reference (`analyzer/config.py`)

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

## Logging

The run logs to the console and to `analyzer/analyzer.log`. Each run ends with a summary: analysed, Bid, TBD, NoBid, skipped, out-of-window, and errors.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Sheet '…' not found in folder …` | The sheet doesn't exist in the configured folder, or the service account can't see it. Check `project_config.json` and share the folder/sheet with the service account email. |
| `openrouter_api_key is not set …` | `credentials/openrouter_credentials.json` is missing the `openrouter_api_key` field. |
| All scores low / "NO company context" warning | `analyzer/knowledge/onepoint_capabilities.md` is missing or empty — populate it. |
| `Analyzer failed … after N attempts` | The model kept returning incomplete/transient responses; the row is recorded as `NoBid(AI)` for manual review. Re-run later. |
| `HttpError 403/429` | Sheets/Drive rate limit — the tool retries automatically with backoff. |

See [SETUP.md](SETUP.md) for installation and first-run instructions.
