# PS Tender Tracker — Release Notes

> **Note:** These are the release notes for the prior **PS Tender Tracker** tool (a multi-adapter procurement scraper), preserved here for history. The current project — the **PS BidAnalyzer Tool** — has its own notes in [RELEASE_NOTES.md](RELEASE_NOTES.md).

---

## v2.1.0 — 2026-06-23

**Config restructure, GCP service account auth, expanded keywords, enriched descriptions, column renames, and sheet color logic improvements.**

---

### Overview

v2.1.0 consolidates configuration into two split JSON files, removes the interactive OAuth flow in favour of a GCP service account, extends keyword coverage, and tightens filtering and column naming throughout both adapters. The sheet colour logic is overhauled so NoBid→ReCheck only fires on real data changes, and a new PME_Flag column is introduced alongside a rename of the SME field.

---

### Breaking Changes

- **Config files changed** — `config.json` is deleted. Adapter settings now live in `adapter_config.json`; sheet/folder settings in `project_config.json`. Per-environment variants (`project_config_dev.json`, `project_config_trial.json`) are ignored by `.gitignore`.
- **Authentication changed** — OAuth 2.0 browser flow and `ps_tender_token.json` are removed. Both adapters now authenticate via `credentials/service_account.json` (GCP service account key).
- **Column renames** — `Tender Status` → `Bid Qualification`, `Tender Status Date` → `Bid Qualification Date`, `Tender Status Reason` → `Bid Qualification Reason`, `Suitable for SMEs?` → `SME_Flag`. Existing sheets are migrated automatically by `_ensure_columns_match()`.
- **SC_Flag values** — changed from `True`/`False` to `Yes`/`No`.
- **New column: PME_Flag** — inserted into the dataset schema; existing sheets gain this column automatically on first run.

---

### New Features

#### GCP Service Account Authentication
- Browser-based OAuth flow removed from both adapters' `google_sheets_auth.py`
- Both adapters now build credentials from `credentials/service_account.json` using `google.oauth2.service_credentials`
- Supports fully headless and scheduled runs with no interactive login step
- `ps_tender_token.json` token file no longer created or required

#### Split Configuration Files
- `adapter_config.json` — adapter registry merged with per-adapter filters, keywords, CPV codes, date windows, qualification thresholds, and API key
- `project_config.json` — Google Sheets folder ID and sheet name (environment-specific variants: `project_config_dev.json`, `project_config_trial.json`)
- Root `config.py` now only loads project-level settings; adapter constants removed from root
- Each adapter's `config.py` loads its own section from `adapter_config.json` and defines its own date range functions
- `config.json` deleted; both parsers source `PLANNING_THRESHOLD` and `TENDER_THRESHOLD` from config instead of hardcoded values

#### Expanded Keyword List (~30 new terms)
- **Data Engineering** — `ETL`, `ELT`, `Data Lake`, `Data Lakehouse`, `Data Mesh`, `Snowflake`, `Databricks`, `dbt`, `Apache Spark`
- **Cloud & Integration** — `iPaaS`, `Middleware`, `MuleSoft`, `Azure Integration Services`, `AWS Glue`, `Event-driven Architecture`
- **Agentic AI** — `RAG`, `Retrieval-Augmented Generation`, `LangChain`, `LlamaIndex`, `Agentic AI`, `AI Agents`
- **Power Platform** — `Power BI`, `Power Automate`, `Power Apps`, `Copilot Studio`
- **Legacy Modernisation** — `Legacy Modernisation`, `Application Rationalisation`, `Technical Debt`
- Keywords precompiled as `\b`-bounded regex patterns in each adapter's `config.py`

#### Word-Boundary Keyword Matching
- Replaced bare `in` substring check with precompiled `re.compile(r'\b<keyword>\b', re.IGNORECASE)` in both adapters' `scraper.py`
- Eliminates false positives from short abbreviations (e.g. `EMB` previously matched inside "members")
- Patterns compiled once at module load; no per-tender recompilation overhead

#### PME_Flag Column
- New `PME_Flag` field: `Yes` when `Procurement Stage` contains `planning` (covers `planning` and `planningUpdate` OCDS tags), `No` otherwise
- Tracked in `CHANGE_FIELDS` alongside `SME_Flag` for change detection on re-scrape

#### Enriched Tender Description
- New `_build_description()` helper in both `tender_parser.py` files
- Appends each lot's title and description beneath the base `tender.description` text
- Increases description coverage from ~2 k to ~20 k characters for multi-lot tenders

#### EXCLUDED_TAGS Filter
- New `EXCLUDED_TAGS` set in both adapters' `scraper.py`: filters out `award`, `contract`, `awardUpdate`, `contractTermination`, `implementation` releases at fetch time
- `EXCLUDED_STATUSES` expanded to include `cancelled` and `withdrawn` (previously only `complete`)

#### Due Date Qualification Window — UK Working Days
- Qualification's minimum due-date check now uses **5 UK working days** instead of a flat calendar-day offset
- Working-day counter skips weekends and UK bank holidays via the `holidays` library (England subdivision) to include all bank holidays correctly

#### Row Colour Logic Overhaul
| Scenario | Previous colour | New colour |
|---|---|---|
| NoBid re-scraped, **field data changed** | Amber | ReCheck (Amber) |
| NoBid re-scraped, **no field changes** | No colour applied | Red |
| Bid status row (updated) | Unset | White / no background |
| Bid status row (stale) | Unset | White / no background |
- New `_has_field_changes()` helper detects real data changes, excluding `Bid Qualification`
- NoBid→ReCheck promotion only fires when `_has_field_changes()` returns `True`

#### Adapter2 Improvements
- Malformed JSON handling synced with adapter1 (same error recovery path)
- `ID` column now sourced from `release.tender.id` instead of `release.id`
- `run_ts` timestamp used consistently for all `extract_json/` and malformed filenames across both adapters

---

### Updated Architecture (v2.1.0)

```
orchestrator.py               Top-level runner — loads project_config.json, dispatches adapters
project_config.json           Sheet name and Drive folder ID (environment-specific variants ignored by .gitignore)
adapter_config.json           Adapter registry + per-adapter filters, keywords, thresholds, API key
config.py                     Project-level settings loader; date helper (working-day aware)
list_bank_holidays.py         Utility: print UK bank holidays for a given year
adapters/
  adapter1/
    main.py                   Adapter orchestration — 6-step pipeline
    scraper.py                FTS OCDS API client; keyword filter (word-boundary regex); tag/status exclusions
    tender_parser.py          OCDS JSON → dataset dict; _build_description(); qualify_tender()
    sheets_writer.py          Google Sheets append / batchUpdate; colour logic; _has_field_changes()
    google_sheets_auth.py     GCP service account authentication
    sc_checker.py             Portal page SC clearance scanner
    config.py                 Loads adapter1 section from adapter_config.json; date range functions
  adapter2/
    (mirrors adapter1 structure)
```

---

### Updated Dataset Schema

| Column | Field | Notes |
|--------|-------|-------|
| A | Portal Name | |
| B | Adapter | Which adapter wrote this row |
| C | Direct URL | |
| D | OCID | Full OCDS identifier; deduplication key |
| E | Name | |
| F | **Bid Qualification** | Renamed from Tender Status |
| G | Published On | |
| H | Clarification Due Date | |
| I | Tender Due Date | |
| J | **Bid Qualification Date** | Renamed from Tender Status Date |
| K | ID | |
| L | Procurement Stage | |
| M | Total Contract Value | |
| N | Contract Duration | |
| O | Annual Contract Value | |
| P | Tender Description | Enriched with per-lot titles + descriptions |
| Q | Buyer Name | |
| R | CPV Code | |
| S | **SC_Flag** | Now `Yes` / `No` (was `True` / `False`) |
| T | Country | |
| U | Locality | |
| V | **SME_Flag** | Renamed from "Suitable for SMEs?" |
| W | **PME_Flag** | NEW — Yes if Planning stage |
| X | Processed Date | |
| Y | Comments | |
| Z | Last Modified Date | |
| AA | Created Date | |
| AB | **Bid Qualification Reason** | Renamed from Tender Status Reason |

---

### Known Limitations

- SME and due-date filters remain commented out in `scraper.py`; date range is temporarily relaxed for broader test coverage
- `Tender Due Date` is still sparsely populated for award/contract-type notices (these are now pre-filtered by `EXCLUDED_TAGS`)
- ~10 % of tenders carry no value field in the API (unpublished by the buyer)
- No built-in scheduler or UI; runs are manual or externally scheduled

---

## v2.0.0 — 2026-06-01

**Major architectural refactor: multi-adapter orchestration, expanded schema, auto-qualification, and OCID-based deduplication.**

---

### Overview

v2.0.0 restructures the tool from a single-portal script into a multi-adapter platform. A new orchestration layer (`orchestrator.py` + `config.json`) manages multiple data-source adapters; the original FTS scraper is promoted to `adapters/adapter1/`. The dataset schema is expanded to 25 columns and a new auto-qualification step automatically categorises tenders as `PreQualified` or `NotQualified` before they reach the sheet.

---

### Breaking Changes

- **Schema change** — four new columns added (`Adapter`, `OCID`, `Country`, `Locality`); column order revised. Existing sheets are migrated automatically via `_ensure_columns_match()` on first run.
- **Deduplication key changed** — URL-based dedup replaced by OCID-based dedup. Duplicate detection is now per-adapter so multiple adapters can coexist in the same sheet without overwriting each other.
- **Entry point changed** — top-level `main.py` is removed; use `python orchestrator.py` (or `python orchestrator.py adapter1` to run a single adapter).

---

### New Features

#### Multi-Adapter Orchestration
- `orchestrator.py` — top-level runner that loads `config.json`, iterates enabled adapters, and dispatches each via `importlib`
- `config.json` — adapter registry with `adapter_id`, `portal`, `type`, `frequency`, `module`, and `enabled` flag
- Adapters can be individually enabled/disabled or targeted by ID: `python orchestrator.py adapter1`
- Each adapter logs to its own file (`adapters/adapter1/adapter1.log`) and manages its own `extract_json/` and `target_json/` directories

#### Auto-Qualification (new pipeline step 5)
- New `qualify_tender()` method in `TenderParser` applied after SC checking
- Rules applied in order:
  - `planning` stage + annual value < £1,000,000 → `PreQualified`
  - `tender` / `opportunity` stage + annual value < £139,689 → `PreQualified`
  - Annual value missing → `PreQualified` (benefit of the doubt)
  - All other value/stage scenarios → `NotQualified`
- Secondary due-date check: if the tender has a parseable due date outside the configured window, it is demoted to `NotQualified`
- `Status` field is populated automatically; manual overrides (any value other than `PreQualified`, `NotQualified`, or blank) are preserved on subsequent runs

#### Manual Status Preservation
- On update, `SheetsWriter` detects whether the existing `Status` cell holds a system value (`PreQualified`, `NotQualified`, blank) or a human override
- Human overrides (e.g. `Shortlisted`, `Rejected`) are never overwritten; `Status Date` is also frozen in that case
- System statuses are replaced by the freshly computed qualification, and `Status Date` is stamped on change

#### Expanded Dataset Schema (25 columns)

| Column | Field | Notes |
|--------|-------|-------|
| A | Portal Name | |
| B | **Adapter** | NEW — which adapter wrote this row |
| C | Direct URL | |
| D | Published On | |
| E | ID | |
| F | **OCID** | NEW — full OCDS identifier; deduplication key |
| G | Name | |
| H | Due Date | |
| I | Procurement Stage | |
| J | Total Contract Value | |
| K | Contract Duration | |
| L | Annual Contract Value | |
| M | Tender Description | |
| N | Buyer Name | |
| O | CPV Code | |
| P | SC_Flag | |
| Q | **Country** | NEW — buyer's country from OCDS parties |
| R | **Locality** | NEW — buyer's locality from OCDS parties |
| S | Suitable for SMEs? | |
| T | Status | Now auto-set by qualification step |
| U | Status Date | Auto-stamped on status change |
| V | Processed Date | |
| W | Comments | |
| X | Last Modified Date | |
| Y | Created Date | |

#### Sheet Column Auto-Repair
- `_ensure_columns_match()` compares the live sheet header row against `DATASET_FIELDS` and inserts any missing columns at the correct positions using `batchUpdate`
- Existing data is never shifted; only blank columns are inserted where headers are absent
- Runs automatically on every execution when an existing sheet is found

#### OCID-Based Deduplication
- `SheetsWriter` now indexes existing rows by `OCID` (not URL), scoped to the current adapter
- `_dedup_by_ocid()` resolves within-batch conflicts (same OCID, multiple releases) by keeping the release with the latest `Published On` date
- Adapter-scoped indexing means adapter rows are isolated — one adapter cannot overwrite another's records

#### Expanded Due Date Extraction (4-priority fallback)
1. `tender.tenderPeriod.endDate` — official bid closing date
2. `planning.milestones[].dueDate` — milestone date on pre-market notices
3. `tender.expressionOfInterestDeadline` — EOI deadline on early-stage notices
4. `tender.communication.futureNoticeDate` — anticipated tender publication date

#### SME Suitability Extended
- Now checks `tender.lots[].suitability.sme` in addition to `tender.suitability.sme`; any positive lot-level flag returns `Yes`

#### SC_Flag Tracked in Change Diff
- `SC_Flag` added to `CHANGE_FIELDS`; SC clearance changes are now included in the per-run diff comment appended to the `Comments` column

#### Rate Limit Handling — Exponential Backoff with Jitter
- `SheetsWriter._execute_with_retry()` retries up to 6 times with exponential backoff (`5 × 2^attempt`) plus random jitter, capped at 120 s

---

### Pipeline Summary (v2.0.0)

```
[1/6] Init scraper
[2/6] Scrape FTS API (fetch + filter)
[3/6] Parse tender details (OCDS → 25-field dict)
[4/6] SC Flag check (portal page scan)
[5/6] Qualify tenders (PreQualified / NotQualified)
[5b]  Write parsed tenders to target_json/
[6/6] Write / update Google Sheet
```

---

### Architecture (v2.0.0)

```
orchestrator.py           Top-level runner — loads config.json and dispatches adapters
config.json               Adapter registry
config.py                 Shared configuration (keywords, date logic, schema)
adapters/
  adapter1/
    main.py               Adapter orchestration — 6-step pipeline
    scraper.py            FTS OCDS API client + filtering
    tender_parser.py      OCDS JSON → 25-field dict + qualify_tender()
    sheets_writer.py      Google Sheets append / batchUpdate / column repair
    google_sheets_auth.py OAuth 2.0 token management
    sc_checker.py         Portal page SC clearance scanner
    config.py             Adapter-specific path overrides (re-exports root config)
```

---

### Known Limitations

- SME and due-date filters remain commented out in `scraper.py`; date range is temporarily relaxed for broader test coverage
- `Due Date` is still sparsely populated for award/contract-type notices
- ~10 % of tenders carry no value field in the API (unpublished by the buyer)
- No built-in scheduler or UI; runs are manual or externally scheduled

---

## v1.0.0 — 2026-05-21

**Initial production release of the PS Tender Tracker web scraper and data integration tool.**

---

### Overview

PS Tender Tracker is a Python-based tool that scrapes UK government procurement notices from the [Find a Tender Service (FTS)](https://www.find-tender.service.gov.uk) OCDS API, filters them against OnePoint's service keywords, and writes structured tender data into a Google Sheet for business development tracking.

---

### Features

#### Data Acquisition
- Connects to the FTS OCDS API (`find-tender.service.gov.uk/api/1.0`) to fetch procurement releases
- Supports optional `CDP-Api-Key` authentication header
- Raw API responses saved as timestamped JSON extracts (`extract_json/extract_YYYYMMDD_HHMMSS.json`) for auditability

#### Filtering
- **Keyword filter** — 54 terms across six categories: AI/ML, Data, Cloud/Integration, Delivery, Social Value, and Sector-specific (e.g. `Artificial Intelligence`, `GenAI`, `LLM`, `Azure`, `DevSecOps`, `Social Value`, `Public Sector Transformation`)
- **Publication date filter** — rolling 7-day window; start date rounded back to Monday when run mid-week
- **Closing date filter** — tenders closing 2–14 days from today; end rounded forward to Saturday
- **Status filter** — excludes `tender.status == "complete"` (closed/awarded notices)
- **Deduplication** — URL-based dedup prevents writing the same tender twice within or across runs

#### Field Extraction (21 columns, A–U)
| Column | Field | Source |
|--------|-------|--------|
| A | Portal Name | Static: `Find-A-Tender` |
| B | Direct URL | `release.id` → portal URL |
| C | Published On | `release.date` |
| D | ID | `release.ocid` |
| E | Name | `tender.title` |
| F | Due Date | `tender.tenderPeriod.endDate` |
| G | Procurement Stage | `release.tag` + `release.initiationType` |
| H | Total Contract Value | `tender.value` → `lots[].value` → `awards[].value` → `contracts[].value` (prefers `amountGross`) |
| I | Contract Duration | `tender.lots[].contractPeriod` → `awards[].contractPeriod` → `contracts[].period` → `tender.contractPeriod` |
| J | Annual Contract Value | Total Value ÷ (base months / 12) |
| K | Tender Description | `tender.description` |
| L | Buyer Name | `buyer.name` |
| M | Suitable for SMEs? | `tender.suitability.sme` → Yes / No |
| N | Status | Default blank (set manually: Shortlisted / Pre-qualified / Rejected) |
| O | Status Date | Default blank |
| P | Processed Date | Run timestamp |
| Q | Comments | Timestamped audit trail; appended on each update |
| R | Last Modified Date | `release.date` |
| S | Created Date | First-seen timestamp |
| T | CPV Code | `tender.classification` + `tender.items[].classification` |
| U | SC_Flag | Default `TBD` |

#### Google Sheets Integration
- OAuth 2.0 authentication via service credentials (`credentials/credentials.json` + `credentials/ps_tender_token.json`)
- Auto-creates the `PS Tender Tracker` sheet inside the configured Google Drive folder if it does not exist
- **New tenders** — appended as new rows
- **Existing tenders** — updated in-place using `batchUpdate` (matched by URL)
- `LAST_COL` is resolved dynamically; no hardcoded column range

#### Output & Logging
- Parsed tenders saved as `target_json/tenders_YYYYMMDD_HHMMSS.json` after each run
- Full run log written to `tender_scraper.log` (console + file)
- End-of-run summary: tenders found, parsed, written, updated, skipped, errors

#### Configuration (`config.py`)
- All environment-specific values centralised: API base URL, Sheet name, Drive folder ID, credentials paths, timezone, keywords, CPV codes, date-range logic
- UK timezone (`Europe/London`) used throughout for date calculations

---

### Architecture

```
main.py               Orchestration — 5-step pipeline
scraper.py            FTS OCDS API client + filtering
tender_parser.py      OCDS JSON → 21-field dict
sheets_writer.py      Google Sheets append / batchUpdate
google_sheets_auth.py OAuth 2.0 token management
config.py             Central configuration & date helpers
```

---

#### Security Clearance (SC) Flag Check
- After parsing, each tender's portal page (`Direct URL`) is fetched and scanned for security clearance requirements
- Detects all five UK government clearance levels:
  - Security Check (SC)
  - Enhanced Security Check (eSC)
  - Counter Terrorist Check (CTC)
  - Developed Vetting (DV)
  - Enhanced Developed Vetting (eDV)
- Matches both full phrases and abbreviations (abbreviations `DV`/`SC` are only matched when paired with clearance/vetting context to avoid false positives)
- `SC_Flag` column (U) is set to `True` (with found terms listed) or `False` (with a "not mentioned" note); `TBD` is retained only if the page could not be fetched
- SC check result is appended as a timestamped entry in the `Comments` column
- SC_Flag changes are tracked in update diff comments on subsequent runs
- Courtesy 1.2-second delay between portal page fetches; retries on rate limit (HTTP 429)
- Run summary includes SC flag totals: `SC Flag — True: N | False: M`

---

### Known Limitations

- `Due Date` is sparsely populated — most notices on FTS are award/contract type where `tenderPeriod` is already closed
- ~10 % of tenders carry no value field in the API (genuinely unpublished by the buyer)
- CPV code and SME filters are present in `config.py` but commented out in `scraper.py` pending verification against live API behaviour
- Tool is designed for manual / scheduled runs; no built-in scheduler or UI

---

### Setup Requirements

- Python 3.x with dependencies listed in `requirements.txt`
- Google Cloud project with Sheets + Drive APIs enabled
- OAuth credentials placed in `credentials/credentials.json`
- See `SETUP.md` for full installation and first-run instructions
