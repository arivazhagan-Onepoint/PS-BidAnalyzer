# Detailed Analyzer (`DetailedAnalyzer/`)

Second-stage analysis for the PS BidAnalyzer Tool. Where `analyzer/` answers
*"should Onepoint bid on this at all?"* with one 0–100 score, this module takes
the tenders that cleared that gate and produces the written brief a bid team can
work from.

> **Status: scaffold.** The infrastructure is complete and runnable — config,
> logging, sheet read, row selection, the Gemini call with retries, write-back,
> summary email. The **prompt and the output columns are not settled**; every
> such spot is marked `TODO` in the code. Write-back ships disabled, so a run
> today is read-only.

## How it works

```
project_config.json ──▶ SheetsClient.read_tenders()
                              │  (whole row, keyed by the sheet's header row)
                              ▼
                        filter: Bid Qualification ∈ PROCESS_STATUSES
                              │  and not already_detailed()
                              ▼
                        analyse_tender_detail(tender.data)
                              │  Gemini · shared Onepoint capability context
                              ▼
                        TenderDetail(summary, detail)
                              ▼
                        SheetsClient.write_updates()   ← gated on WRITE_BACK_ENABLED
   updates: OUTPUT_FIELD_MAP columns + [Comments] [Processed Date] [Last Modified Date]
```

## Files

| File | Responsibility |
|------|----------------|
| `sources.py` | **Ingestion layer** — reads Onepoint's Drive source sheets, strips PII, renders the corpus. Own entry point. |
| `main.py` | Orchestration: read sheet → select → analyse → write back → email. Entry point. |
| `detailed_analyzer.py` | Core `analyse_tender_detail()` — prompt, Gemini call, retries, result. |
| `config.py` | Model budget, scope, output mapping, write-back switch. Re-exports the root `config.py`. |
| `gemini_client.py` | Native Gemini client wrapper (own copy, so the two stages stay independent). |
| `onepoint_context.py` | Loads the **shared** capability context from `analyzer/knowledge/`. |
| `sheets_client.py` | Slim sheet read + write. No row colouring or tab sync — those stay the analyzer's. |
| `knowledge/` | Stage-specific reference material (the capability file is **not** duplicated here). |

## Run

```bash
# From the project root
python -m DetailedAnalyzer.sources             # build/refresh the source corpus
python -m DetailedAnalyzer.sources --dry-run   # render to stdout, write nothing

python -m DetailedAnalyzer.main                # every in-scope row
python -m DetailedAnalyzer.main --limit 3      # first 3 only (quick test)
```

## Source ingestion

`sources.py` is a **separate flow on its own cadence**, like the analyzer's
`maintain_knowledge.py` — the corpus changes when someone edits a source sheet,
not when a tender arrives. `build_corpus()` hits Drive/Sheets and writes the
cache; `load_corpus()` is all an analysis run touches, so a run costs no API
calls and the exact text sent to the model stays on disk to be audited.

Source of truth is the Drive shared drive **Bid Analyzer - Sources**
(`config.SOURCES_FOLDER_ID`), read by the project's existing service account.
The NotebookLM / Gemini Notebook links in `Requirements.md` are **not** ingestible
— they are consumer notebooks behind a Google sign-in wall, and the official API
is Gemini Notebook Enterprise only. A notebook is just a wrapper over source
documents, so this ingests the documents.

Normalisation is behaviour, not tidying. The sources are hand-maintained working
documents, and every filter in `config.py` answers something verified present in
the data:

| Filter | What it stops |
|---|---|
| PII columns / labels / free-text scrub | Named referees, personal mobiles, emails reaching the model and the on-disk cache |
| `SENSITIVE_ROW_MARKERS` | **Persons with Significant Control answers** — a director's date of birth, nationality and home address, sitting inside an ordinary Question/Response table where the column filters cannot see them. The question is kept, the answer withheld |
| `EXAMPLE_ROW_MARKERS` | A dummy example row that would otherwise be cited as a real £2M DWP contract |
| `PLACEHOLDER_VALUES` / `UNCONFIRMED_SUFFIX` | `TBC` reading as a clean absence, and `Yes?` being tidied into a confirmed `Yes` |
| `MONEY_ZERO_VALUES` | An unfilled `£0` contract-value column reading as a real zero-value contract |
| `DEDUPE_IDENTICAL_TABS` | The same past-performance tab counted twice — to an LLM, repeated evidence reads as more evidence |

Two rules the prompt states explicitly, because the gaps are real: `(not
provided)` means the source was blank — do not infer a figure — and
`(unconfirmed)` was flagged uncertain by its author, so it cannot be put to a
buyer as fact.

Deliberately **no blanket postcode or address scrub**: Onepoint's registered
office address is legitimate bid evidence and a regex cannot tell it from a
director's home address. The structural filter removes the personal one.

The corpus cache is **gitignored** — unreleased commercial evidence, even after
PII stripping.

Log: `DetailedAnalyzer/detailed_analyzer.log` (also echoed to console). Covered
by the root `.gitignore`'s `*.log`.

## What it deliberately does not do

- **Never writes `Bid Qualification`** or either `Bid Qualification Reason`
  column. The qualification is the analyzer's to set; the human reason is the
  team's. This stage adds to `Comments` and its own columns only.
- **Never creates a tab or column.** The sheet's structure is maintained by
  hand; an unmapped column is skipped with a warning.
- **No knowledge-maintenance flow.** The analyzer has one
  (`maintain_knowledge.py`) because its precedent is distilled from human
  decisions. Nothing equivalent exists for this stage yet, so no such file was
  scaffolded.

## Before this can run for real

1. ~~Confirm the scope~~ — settled: `PROCESS_STATUSES` is `{Bid(AI),
   Bid(Human)}`. The bare `Bid` is excluded because it carries no attribution as
   to who decided it; requiring the suffix means a row only reaches this stage
   once a machine or a person has owned that call.
2. **Decide the exit condition** — set `ALREADY_DETAILED_FIELD` to the column
   that marks a row as done. Without it, every run re-analyses every Bid row,
   because this stage does not change the status the way the analyzer does.
3. **Add the output column(s)** to the PS Tender Tracker by hand, then map them
   in `OUTPUT_FIELD_MAP`.
4. **Tune the prompt** — `_SYSTEM_PROMPT`, `CONTEXT_FIELDS` and the five-section
   brief in `_build_prompt()` are a starting shape, not an agreed one. Settle the
   section list with the bid team, since it decides what every future brief
   contains.
5. **Flip `WRITE_BACK_ENABLED`** to `True`.

Two constraints worth keeping through all of that: capability claims are grounded
**only** on `onepoint_capabilities.md` (the same wall the analyzer enforces), and
a failed analysis returns a flagged placeholder that is counted as an error and
**never written to the sheet** as though it were an assessment.
