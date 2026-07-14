# Bid Analyzer (`analyzer/`)

LLM-based bid qualification stage for the PS BidAnalyzer Tool. It reads tenders
from the **PS Tender Tracker** Google Sheet (the one referenced in
`project_config.json`, populated by an upstream process), asks a Tender Analyst
model to score Onepoint's fit for each tender out of 100, and writes back a
**Bid / NoBid / TBD** qualification.

## How it works

```
project_config.json ──▶ SheetsClient.read_tenders()
                              │  (Name = title, Tender Description = description)
                              ▼
                        filter: only Bid Qualification ∈ {PreQualified, ReCheck}
                              │  (skips manual overrides & already-analysed rows)
                              ▼
                        analyze_tender(title, description)
                              │  OpenRouter · Gemini · Onepoint capability context
                              ▼
                        score 0-100 ──▶ Bid (>75) / TBD (51-75) / NoBid (≤50)
                              ▼
                        SheetsClient.write_qualifications()
   updates: [Bid Qualification] [Bid Qualification Reason] [Bid Qualification Date]
            [Comments] [Processed Date] [Last Modified Date] [Created Date]
```

## Files

| File | Responsibility |
|------|----------------|
| `main.py` | Orchestration: read sheet → analyse each tender → write back. Entry point. |
| `analyzer.py` | Core `analyze_tender()` — prompts the model and maps score → qualification. |
| `openrouter_client.py` | OpenRouter/OpenAI client (same pattern as `keyword_search.py`). |
| `onepoint_context.py` | Loads the Onepoint capability context that grounds the analysis. |
| `knowledge/onepoint_capabilities.md` | The capability context. **Populate this** from the NotebookLM sources. |
| `config.py` | Model, score thresholds, paths. Re-exports the root `config.py`. |
| `sheets_client.py` | Reads tenders and writes qualification/control columns to the sheet. |

## Prerequisites

1. **Populate `knowledge/onepoint_capabilities.md`** with Onepoint's capabilities
   from the four NotebookLM notebooks in `Requirements.md`. Without it, scores
   are low-confidence.
2. `credentials/openrouter_credentials.json` with `openrouter_api_key` (shared
   with `keyword_search.py`).
3. `credentials/service_account.json` with access to the tender sheet.
4. `project_config.json` pointing at the correct sheet/folder.

## Run

```bash
# From the project root
python -m analyzer.main                    # analyse today's window
python -m analyzer.main --date 2026-07-03  # analyse a specific day (backfill/rerun)
python -m analyzer.main --limit 5          # cap to first 5 rows (quick test)
```

## One-day window

The analyzer only processes rows dated within a **single day**, anchored on the
`Last Modified Date` column (`WINDOW_DATE_FIELD` in `config.py`). The upstream
process stamps this field to the run time for both newly-created `PreQualified`
rows and `ReCheck` rows it re-flags, so the window captures every row worth
analysing that day. The window defaults to **today** (UK time); pass `--date YYYY-MM-DD` to
target another day. Rows outside the window are counted as `Out of window` and
never sent to the model.

## Which rows get analysed

Only rows whose `Bid Qualification` is a **system-assigned** value are analysed
(`PROCESS_STATUSES` in `config.py`):

| Status | Analysed? | Why |
|--------|-----------|-----|
| `PreQualified` | ✅ | System-qualified upstream, awaiting a bid decision |
| `ReCheck` | ✅ | Auto-flagged for re-evaluation after a change |
| `NotQualified`, blank | ❌ | Not a bid candidate |
| `Bid` / `TBD` / `NoBid` | ❌ | Already analysed by this tool |
| Any human-set value (Won, Lost, hand-typed NoBid, …) | ❌ | **Manual override — preserved** |

Manual overrides are detected implicitly: `PreQualified`/`ReCheck` are only ever
written by automated steps, so any other value means a human touched the row.

## Score thresholds

Configured in `config.py` (`score_to_qualification`):

| Score | Qualification |
|-------|---------------|
| > 75  | **Bid** |
| 51–75 | **TBD** |
| ≤ 50  | **NoBid** |

## Note on the model

`Requirements.md` specifies `google/gemini-3.5-flash`. The value in `config.py`
(`ANALYZER_MODEL`) defaults to `google/gemini-2.5-flash` — the model proven in
this codebase (`keyword_search.py`). Update `ANALYZER_MODEL` in one place if/when
`gemini-3.5-flash` is available on your OpenRouter account.
