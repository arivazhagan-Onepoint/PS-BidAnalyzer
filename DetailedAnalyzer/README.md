# Detailed Analyzer (`DetailedAnalyzer/`)

Second-stage analysis for the PS BidAnalyzer Tool. Where `analyzer/` answers
*"should Onepoint bid on this at all?"* with one 0–100 score, this module takes
the tenders that cleared that gate and produces the written brief a bid team can
work from.

> **Status: running for real.** Reports are written to Drive, the tracker is
> written back, and an analysed row moves `Docs(Ready)` → `Done` so it leaves
> scope. What remains optional is `OUTPUT_FIELD_MAP` — the likelihood and report
> link reach the tracker through the `Bid Qualification Reason(System)` and
> `Comments` log entries, and only land in columns of their own once those columns
> are added to the sheet by hand and mapped.

## How it works

```
project_config.json ──▶ SheetsClient.read_tenders()
                              │  (whole row, keyed by the sheet's header row)
                              ▼
                        filter: Bid Qualification ∈ PROCESS_STATUSES
                              │  and not already_detailed()
                              ▼
                        analyse_tender_detail(tender.data)
                              │  Gemini, over three evidence streams:
                              │    · capability context  (shared, hand-authored)
                              │    · source corpus       (Onepoint's own records)
                              │    · tender pack         (this tender's documents)
                              │  + deterministic fields from the row and the clock
                              ▼
                        TenderBrief(fields, fit_dimensions, likelihood, documents)
                              ├──▶ ReportWriter.write()      ← gated on REPORTS_ENABLED
                              │      copy of the template, filled, in Drive
                              ▼
                        SheetsClient.write_updates()   ← gated on WRITE_BACK_ENABLED
   updates: Bid Qualification → Done · [Bid Qualification Reason(System)]
            [Comments] [Processed Date] [Last Modified Date] + OUTPUT_FIELD_MAP
```

## Files

| File | Responsibility |
|------|----------------|
| `sources.py` | **Ingestion layer** — reads Onepoint's Drive source sheets, strips PII, renders the corpus. Own entry point. |
| `tender_docs.py` | **The tender's own pack** — finds its Drive folder by OCID, extracts the documents, drops superseded versions, caps the total. |
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

## Tender documents

The third evidence stream, after the capability context and the source corpus.
Those two describe Onepoint and are identical for every tender; this one is the
buyer's own published pack for **one** tender — the ITT, the draft contract, the
code of conduct — and it is what turns "Mandatory Requirement" from an inference
off a scraped notice into a statement of what a bid is evaluated against.

Source is the Drive folder in `config.TENDER_DOCS_FOLDER_ID`, holding one
subfolder per tender named `<OCID>-<Tender Title>`. Matching is on the **OCID
prefix alone** — it is the OCDS global identifier, stable and distinct across all
529 tracker rows, whereas the title half gets reworded. That also excludes the
placeholder `Sample Tender #…` folders without needing to name them.

Fetched **per row during the run**, not on the corpus's cadence: a pack belongs to
its tender. Extracted text is cached per OCID under `knowledge/tender_docs/`
(gitignored — these are the buyer's documents, usually under the ITT's own
confidentiality terms), invalidated by a fingerprint over each file's id, size and
`modifiedTime` **plus the settings that shape the text**, so raising the cap
rebuilds rather than silently serving text truncated under the old one.

Three decisions here are behaviour, each measured on the real CITB pack
(2026-08-23):

| Decision | Why |
|---|---|
| Text comes from the **downloaded bytes** (`.docx` and `.xlsx` via stdlib `zipfile`+XML, `.pdf` via `pypdf`; native Google Docs and Sheets via their own APIs), never by converting a copy in Drive | Conversion works but takes ~29s/file against ~3s, and the copy lands in the customer's own folder where the service account holds `canEdit` but **not** `canDelete`/`canTrash`. The strays would then be re-ingested as tender documents on the next run |
| Superseded versions detected by **word 5-gram Jaccard ≥ 0.90**, not `difflib` | Measured: same document 0.9896, unrelated documents 0.0001–0.0011 — a ~900× margin, in 6ms. `difflib.ratio()` separates correctly but costs ~7 minutes across the pack; its cheap `quick_ratio()` scores two unrelated documents at 0.90 |
| Which copy wins is the **filename version marker**, never a timestamp | Both timestamps are unusable here: `modifiedTime` is *identical* for the two ITTs, and `createdTime` has v2 created 1.5s **before** v1 — "newest wins" would confidently keep the stale document |

Where no marker separates two copies, **both are kept and the run warns** rather
than a coin being flipped on contract terms — the same refusal to guess as
`report_writer._prefix_match`.

Over `TENDER_DOCS_MAX_TOTAL_CHARS`, documents are capped to a common ceiling
rather than dropped, so short ones survive whole and only the largest are cut; a
cut is stated in the prompt text and the manifest, never silent. The cap is a
backstop against a pathological pack, not a trimming budget — an ordinary
three-document pack must fit whole.

Every brief records its **evidence base**: which documents were read, their sizes,
and anything superseded, truncated or unreadable. It goes to the run log, the
dry-run render and the summary email. To have it in the report as well, add a row
to the template whose column A reads `Documents Reviewed` and point
`TENDER_DOCS_MANIFEST_FIELD` at it.

A tender with no folder, an empty folder, an unreadable document or a Drive outage
degrades to the notice summary and the corpus, and the prompt is told the pack was
unavailable so the brief says so rather than inferring an answer.

**A document that cannot be read is a caveat, not an error.** The tender is still
analysed, the report is still produced, and the run carries straight on to the
next row — nothing increments the run's error count, so the summary email stays
green. Three things happen instead:

1. **The detail is written to the row.** The `Comments` and
   `Bid Qualification Reason(System)` entries carry a `Documents:` clause naming
   what was read, what could not be and why, what was superseded, and what was
   truncated. `Comments` is where someone goes to find out why a brief says what
   it says, and "not stated in the tender" means something different when a
   document went unread:

   ```
   [2026-08-23 14:30] Detailed analysis: 70% (MEDIUM) | Report: https://… |
   Documents: 2 of 4 read; NOT READ: 'Code.pdf' (ModuleNotFoundError: No module
   named 'pypdf'); superseded: 'ITT.docx' by 'ITT v2.docx'; Bid Qualification
   left unchanged so this tender is re-analysed once the whole pack can be read
   ```

2. **The status is left alone** (`MARK_COMPLETE_REQUIRES_FULL_PACK`), so the row
   stays in `Docs(Ready)` and a later run redoes it against the full pack — but
   only up to `TENDER_DOCS_MAX_ATTEMPTS` (3). Every kind of read failure is
   treated the same way; no exception is made for a format this layer cannot
   handle, so the cap is what stops such a row returning forever. Attempts are
   counted from the `incomplete pack, attempt N` marker the entry itself leaves in
   `Bid Qualification Reason(System)`, so no extra tracker column is needed. On
   the last attempt the row is marked `Done` anyway and the entry says so, naming
   the documents and how to undo it.

3. **The pack is not cached.** The fingerprint covers only the files and the
   settings, so a cached failure would be served on every later run until someone
   happened to edit the pack in Drive.

Why the cap exists: a format outside `TENDER_DOCS_SUPPORTED_MIMES` fails
identically every run, and each run costs a fresh Gemini call plus **another
timestamped report** in a Drive folder the service account can add to but not
delete from (`canAddChildren: True`, `canDelete: False`). `AnalysisReports`
already holds four reports for one tender, from the era before anything moved a
row out of scope — the shape `c924792` fixed. Supporting `.xlsx` removes the
realistic cause (packs ship their pricing schedule and requirements matrix as a
spreadsheet); the cap is the backstop for everything else.

All three come from one measured incident: a run whose `pypdf` import failed
briefed a tender from 2 of its 4 documents, cached that as the pack's settled
state, wrote the report and marked the row `Done` — the PDF was fine and the
import worked minutes later, but the row was out of scope for good and only the
run log recorded the gap.

Log: `DetailedAnalyzer/detailed_analyzer.log` (also echoed to console). Covered
by the root `.gitignore`'s `*.log`.

## What it deliberately does not do

- **Never writes `Bid Qualification Reason(Human)`.** The team's manual notes are
  theirs. The stage prepends to `Bid Qualification Reason(System)` and appends to
  `Comments`, both shared append-only logs, so the two stages interleave into one
  history rather than keeping two rival ones.
- **Never creates a tab, column or Drive file outside the reports folder.** The
  sheet's structure is maintained by hand; an unmapped column is skipped with a
  warning. Notably this is why tender documents are parsed from downloaded bytes
  rather than converted in Drive.
- **Never writes a failed analysis as though it were an assessment.** A flagged
  result is counted as an error; no report is created and no row is updated.
- **No knowledge-maintenance flow.** The analyzer has one
  (`maintain_knowledge.py`) because its precedent is distilled from human
  decisions. Nothing equivalent exists for this stage.

It *does* write `Bid Qualification` (`Docs(Ready)` → `Done`), reversing an earlier
note — which follows from `Docs(Ready)` being the gate: whatever consumes a
workflow status has to be what advances it.

## Still open

1. **`OUTPUT_FIELD_MAP` is empty.** Add columns for the likelihood score and the
   report link to the tracker by hand, then map them. Until then both live in the
   report, the email and the log entries only.
2. **`TENDER_DOCS_MANIFEST_FIELD` is `None`.** Add a `Documents Reviewed` row to
   the reporting template to have each brief carry its evidence base.
3. **`DETAIL_MAX_TOKENS` is 4000**, never tested against a brief that needs more.
   A truncated reply shows as `finish_reason=MAX_TOKENS`.
4. **`main()` emails on `--dry-run` too.** Call `run()` directly to test without
   mailing anyone.

One constraint worth keeping through all of that: capability claims are grounded
**only** on `onepoint_capabilities.md` — the same wall the analyzer enforces. The
corpus and the tender pack are evidence *about* a requirement or *behind* a
capability; neither may substitute for that file in deciding what Onepoint can do.
