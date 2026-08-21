"""
DetailedAnalyzer configuration.

Layered exactly like ``analyzer/config.py``: shared settings (DATASET_FIELDS,
Google Sheet target, credential paths, UK timezone, NOTIFICATIONS) live in the
project root ``config.py``; this module re-exports those and adds the
detailed-analysis-specific settings — the model call budget, which rows are in
scope, and where the output goes.

Settings marked TODO are placeholders that need a decision before the module
does real work. They are deliberately explicit rather than absent, so the
unfinished parts are visible in one file instead of scattered through the code.
"""
import os

# Re-export all shared project configuration (DATASET_FIELDS, SHEET_NAME,
# TARGET_FOLDER_ID, SCOPES, SERVICE_ACCOUNT_FILE, UK_TIMEZONE, CREDENTIALS_DIR,
# ENVIRONMENT, NOTIFICATIONS…)
from config import *          # noqa: F401, F403
from config import (  # explicit for linters
    BASE_DIR as PROJECT_ROOT,
    CREDENTIALS_DIR,
    UK_TIMEZONE,
)

# --- Paths ------------------------------------------------------------------
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
LOG_FILE      = os.path.join(BASE_DIR, "detailed_analyzer.log")
KNOWLEDGE_DIR = os.path.join(BASE_DIR, "knowledge")

# The Onepoint capability context is SHARED with the analyzer, not copied here.
# It is hand-authored and tracked in git, and both stages must judge capability
# against the same text — two copies would drift and the two stages would start
# disagreeing about what Onepoint can do. Read from the analyzer's knowledge dir.
ONEPOINT_CONTEXT_FILE = os.path.join(
    PROJECT_ROOT, "analyzer", "knowledge", "onepoint_capabilities.md"
)

# --- Gemini model -----------------------------------------------------------
# Same provider and credentials as the analyzer (native google-genai SDK, no
# fallback). Kept as this module's own constants so its call budget can be tuned
# without touching the analyzer's proven settings.
GEMINI_CREDENTIALS_FILE = os.path.join(CREDENTIALS_DIR, "gemini_credentials.json")
GEMINI_API_KEY_FIELD    = "gemini_api_key"
GEMINI_MODEL            = "gemini-3.1-flash-lite"

DETAIL_MODEL       = GEMINI_MODEL
DETAIL_TEMPERATURE = 0.2
# Far larger than the analyzer's 700: that call returns a score plus 2-4
# sentences, this one returns a multi-section written assessment.
DETAIL_MAX_TOKENS  = 4000

# Gemini 3.x draw reasoning tokens from the same max_output_tokens budget, so
# with thinking on the budget can be consumed before any JSON is emitted
# (finish_reason=MAX_TOKENS) — the failure that forced the analyzer to 0. Start
# at 0 here for the same reason. If richer reasoning turns out to be worth it for
# this longer task, raise it AND raise DETAIL_MAX_TOKENS so the output still fits.
DETAIL_THINKING_BUDGET = 0

# A model call can fail transiently (network blip, upstream 5xx, truncated reply
# that fails JSON parsing). Retry before giving up.
DETAIL_MAX_RETRIES   = 3
API_THROTTLE_SECONDS = 10   # back-off between failed attempts, not a throttle

# --- Source corpus ingestion (DetailedAnalyzer.sources) ---------------------
# Onepoint's own evidence — capability matrix, supplier readiness questionnaire,
# past performance — lives in a Drive shared drive, NOT in the NotebookLM /
# Gemini Notebook links in Requirements.md. Those are consumer notebooks behind a
# Google sign-in wall: no service account or API key can read them (the official
# API is Gemini Notebook Enterprise only, on licensed Cloud projects). The Drive
# folder below is the ingestible replacement, and the project's existing service
# account can already read it.
SOURCES_FOLDER_ID = "1mcKppNQAIwxq3zArDw1VHpfMuMcHiXn4"   # "Bid Analyzer - Sources"

# Rendered corpus, built by `python -m DetailedAnalyzer.sources` and read by every
# analysis run. Cached on disk so a run costs no Drive/Sheets calls, and so the
# exact text sent to the model is auditable after the fact.
#
# GITIGNORED, deliberately: even after PII stripping this is unreleased commercial
# evidence (client names, contract detail, compliance answers). It must not enter
# git history.
CORPUS_FILE = os.path.join(KNOWLEDGE_DIR, "onepoint_corpus.md")

# --- Ingestion filters ------------------------------------------------------
# Everything below exists because the source sheets are hand-maintained working
# documents, not a clean dataset. Each filter corresponds to something verified
# present in the data on 2026-08-20; removing one silently degrades every score
# the module produces afterwards.

# 1. PII. Part 5 and Part 6 carry named referees with personal mobiles and email
#    addresses. Those get sent to Gemini on every call and written to the corpus
#    cache on disk, for no scoring benefit — capability is judged from evidence,
#    not from who to phone about it. Stripped three ways because the sheets use
#    two different layouts (column tables in Part 6, label:value rows in Part 5)
#    and free text leaks the rest.
#
#    A stripped field renders as PII_REDACTION rather than vanishing: a missing
#    row would let the model conclude "no referee available", which is a false
#    negative about Onepoint's evidence. Saying the detail was withheld is honest.
PII_REDACTION = "[contact detail withheld]"

#    Column headers whose entire column is dropped (matched case-insensitively as
#    a substring of the header cell).
PII_COLUMN_MARKERS = (
    "key contact", "referee", "phone", "telephone", "email",
    "contact name", "contact person", "signature",
)

#    Row labels (first cell) whose entire row is dropped — the label:value layout.
PII_LABEL_MARKERS = (
    "contact person", "position of contact person", "email address",
    "telephone", "phone number", "contact name", "signature",
    "name of supplier", "key contact",
)

#    Belt-and-braces scrub of anything the two structural filters missed. Applied
#    to every surviving cell. Deliberately does NOT touch URLs — a website is not
#    a personal contact detail and onepointltd.com links are useful evidence.
PII_EMAIL_PATTERN = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
PII_PHONE_PATTERN = r"(?<![\w/])(?:\+44\s?|0)(?:\d[\d\s\-]{7,12}\d)(?![\w/])"

#    Rows whose QUESTION is a personal-data disclosure. The readiness
#    questionnaire's Persons with Significant Control questions are answered with
#    a named individual's date of birth, nationality, country of residence and
#    home/service address — more sensitive than the phone numbers above, and
#    invisible to the two filters above because they sit in an ordinary
#    "Question / Your response" table. The question is kept (it evidences that
#    Onepoint completed the disclosure) and only the answer is withheld.
SENSITIVE_ROW_MARKERS = (
    "persons with significant control", "psc",
    "date of birth", "service address", "nationality",
    "director", "beneficial owner",
)

#    Which columns count as the answer to withhold on such a row.
RESPONSE_COLUMN_MARKERS = ("your response", "supplier response", "response", "notes")

#    Named individuals to redact wherever they appear in free text. An explicit
#    list, because detecting names in prose is unreliable in both directions —
#    it misses real ones and redacts words like a client called "Moto". This
#    catches internal working notes ("Discuss with Shashin", "REF: Shashin:
#    Rajesh…") that are chatter rather than evidence. Add names as they appear.
#
#    Note there is deliberately NO blanket postcode or address scrub: Onepoint's
#    registered office address is legitimate bid evidence, and a regex cannot tell
#    it from a director's home address. The structural filter above is what
#    removes the personal one.
PII_NAME_MARKERS = ("shashin shah", "shashin", "rajesh patel")

# 2. Dummy example rows. Row 4 of the past-performance matrix is a filled-in
#    example — "DWP", "Global Data Management", "£2M", "John Surname". Ingested
#    unfiltered, the model cites a £2M DWP contract as genuine past performance.
#    A row is dropped if any cell contains one of these (case-insensitive).
#    Note "example" alone is NOT a marker: "Contract Example 1" is a real section
#    heading in Part 5 and must survive.
EXAMPLE_ROW_MARKERS = ("e.g.", "add text here", "john surname", "xxxxx", "jonhn@")

# 3. Provisional values. ~118 cells are TBC/TBD/N/A and ~57 short cells carry a
#    trailing "?" ("AVG?", "Yes?", "?? 2020"). Neither is dropped and neither is
#    cleaned up: dropping makes an unanswered question look like a clean absence,
#    and stripping the "?" off "Yes?" fabricates a confirmed answer. Both are
#    relabelled so the model can see the data is not settled.
PLACEHOLDER_VALUES = ("tbc", "tbd", "n/a", "na", "?", "??", "???", "-")
NOT_PROVIDED_RENDER = "(not provided)"
UNCONFIRMED_SUFFIX  = " (unconfirmed)"

# 4. Money columns. Every one of the 44 Total Contract Value cells is blank or
#    £0 — the column was never filled in. Rendering "£0" invites the model to
#    read it as a real zero-value contract, so a zero in a value column is
#    reported as not provided instead. Contract-value fit simply cannot be scored
#    from this source until the column is populated.
MONEY_COLUMN_MARKERS = ("contract value", "tcv", "value (")
MONEY_ZERO_VALUES = ("0", "0.0", "0.00", "£0", "£0.00", "£ 0")

# 5. Duplicate tabs. 'Suppier Readiness ' in the capability matrix file is
#    byte-identical to 'Part 6' in the readiness file. Ingesting both counts the
#    same past performance twice, and repeated evidence reads to a model as more
#    evidence. Deduped on a content hash, first occurrence wins.
DEDUPE_IDENTICAL_TABS = True

# Tabs skipped entirely — no evidential content, and the Declaration is a
# signature block that is nothing but contact fields.
SKIP_TABS = ("read me", "declaration")

# --- Which rows get a detailed analysis -------------------------------------
# Confirmed 2026-08-21: scope is the single status 'Docs(Ready)'. Compared
# case-insensitively after trimming.
#
# This is a better gate than the Bid statuses it replaced, and for a specific
# reason: a detailed brief is only worth producing once the tender's documents
# are actually available to read. 'Bid(AI)' says someone thinks the opportunity is
# worth pursuing, which is not the same thing — briefing a tender whose pack has
# not landed yet produces an assessment built on the notice summary alone.
# 'Docs(Ready)' asserts the input this stage needs.
#
# Note this set has no natural exit condition the way the analyzer's does — a
# 'Docs(Ready)' row stays 'Docs(Ready)' after this module runs, so it would be
# picked up again on every subsequent run. Whatever marks a row as already-detailed
# is the other half of this decision; see ALREADY_DETAILED_FIELD below.
PROCESS_STATUSES = {"Docs(Ready)"}

# Column holding the qualification status used for the filter above.
STATUS_FIELD = "Bid Qualification"

# TODO. Set this to the column that records a completed detailed analysis; rows
# where it is non-empty are skipped, which is what stops every run re-analysing
# the same tenders. Leave as None to disable the check (every in-scope row is
# analysed every run — fine for testing, expensive in production).
ALREADY_DETAILED_FIELD = None

# --- Output: one report per tender ------------------------------------------
# Each brief is a COPY of the reporting template, written to the reports folder
# and named after the tender. Chosen over accumulating tabs in one spreadsheet so
# the template itself is never written to, each report is separately shareable
# with whoever owns that bid, and nothing in this code has to invent tabs inside
# a sheet whose structure is maintained by hand.
TEMPLATE_SPREADSHEET_ID = "1ImvX_fN7UHfgLFXV1to5V2pTZrGSJStPw3rHnBn6FLA"
TEMPLATE_TAB_NAME = "Detailed Analysis Template"
REPORTS_FOLDER_ID = "1yJ6tTlVB_R696RgwuipL2RTmdnnvxq10"

# Report file name: PortalName-TenderID-TenderTitle-Report-RunTime.
# The run time is part of the name on purpose — it makes every run's output
# distinct, so re-analysing a tender leaves the previous brief intact beside the
# new one rather than silently replacing an assessment someone may already have
# read and acted on.
REPORT_NAME_PATTERN = "{portal}-{tender_id}-{title}-Report-{runtime}"
REPORT_RUNTIME_FORMAT = "%Y%m%d-%H%M%S"

# Only the title is truncated — the portal, ID and timestamp are what make the
# name identifiable, so they are never cut.
REPORT_NAME_MAX_TITLE = 80

# Placeholders when the tracker row has no portal or ID. Kept explicit rather than
# collapsing the segment, so the name keeps its five-part shape and stays
# parseable even for an incomplete row.
REPORT_NAME_NO_PORTAL = "UnknownPortal"
REPORT_NAME_NO_ID = "NoID"

# Characters replaced in the name. '/' and '\' would read as path separators once
# a report is downloaded, and the rest are rejected or mangled by one OS or
# another. Hyphen is the field separator, so a title containing hyphens makes the
# name slightly ambiguous to split — accepted, since these names are read by
# people rather than parsed.
REPORT_NAME_UNSAFE_CHARS = r'/\:*?"<>|'

# The copied report's tab is left as the template named it ("Detailed Analysis
# Template"). The file name now carries the tender's identity, so renaming the tab
# adds nothing — and the full report name would exceed the 100-character tab
# limit anyway. Set True to have the tab renamed after the report instead.
RENAME_REPORT_TAB = False

# Column A / B of the template — the label column and the column filled in.
TEMPLATE_LABEL_COL = "A"
TEMPLATE_DETAIL_COL = "B"

# Master switch for creating reports in Drive. Unlike WRITE_BACK_ENABLED below
# this ships TRUE: the report IS the deliverable, it is written to a folder set
# aside for exactly this, and a wrong one can simply be trashed. Set False to
# analyse and log without touching Drive.
REPORTS_ENABLED = True

# --- Output: reports referenced from the summary email ----------------------
# The email links each report rather than attaching it. The Drive copy stays the
# single record — an attachment forks it the moment someone edits their copy —
# and a link costs nothing against the relay's message size limit, which matters
# when a run's scope is every eligible row.
EMAIL_LINK_REPORTS = True

# --- Output: write-back to the tracker --------------------------------------
# TODO. The tracker's DATASET_FIELDS has no column for the likelihood score or
# the report link. Add the column(s) to the sheet by hand first (the sheet's
# structure is maintained manually, not by this code), then map them here.
#
# Keys are TenderBrief attributes, values are sheet column names, e.g.
#   {"likelihood_summary": "Likelihood of Winning", "report_url": "Detailed Analysis"}
OUTPUT_FIELD_MAP = {}

# Master switch for the write-back step. Ships FALSE: until OUTPUT_FIELD_MAP is
# filled in and the columns exist, a run reads the sheet, analyses, logs what it
# would have written, and touches nothing. Flip to True once the mapping is real
# — the live PS Tender Tracker is not the place to discover a half-built writer.
WRITE_BACK_ENABLED = False


def should_analyse(status: str) -> bool:
    """True when a row's qualification puts it in scope for detailed analysis.

    Mirrors ``analyzer.config.should_analyse``: compared case-insensitively
    after trimming, applied once before the run loop so out-of-scope rows never
    reach the model or the per-row log.
    """
    return (status or "").strip().lower() in {s.lower() for s in PROCESS_STATUSES}


def already_detailed(row: dict) -> bool:
    """True when this row already carries a completed detailed analysis.

    Always False while ALREADY_DETAILED_FIELD is None (the check is disabled),
    so the caller can rely on it without special-casing the unconfigured state.
    """
    if not ALREADY_DETAILED_FIELD:
        return False
    return bool((row.get(ALREADY_DETAILED_FIELD, "") or "").strip())
