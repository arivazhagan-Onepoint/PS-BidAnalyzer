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
import re

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

# --- Onepoint's public website ----------------------------------------------
# Ingested as PART OF THE CORPUS, by the same build_corpus() run and into the same
# artifact. The website and the internal records describe one company and go stale
# the same way, so they are gathered together and there is one thing to refresh.
#
# Within the corpus it keeps its own section and its own caveat: a procurement
# answer is something Onepoint committed to in a document, whereas website copy is
# marketing, and the marketing register is what inflates a fit score — the
# expensive direction to be wrong in. Labelling the section costs nothing and
# keeps that distinction available to the model.
#
# The domain is onepointltd.com, NOT onepoint.com. Checked 2026-08-23: TLS on
# www.onepoint.com does not even cover that hostname, and it is not Onepoint's —
# ingesting it would have put another company's claims into the capability
# evidence. onepointltd.com serves "Onepoint | Your trusted companions for the
# digital journey", matching the onepointltd.com addresses already in this config.
SITE_ENABLED      = True
SITE_BASE_URL     = "https://www.onepointltd.com"
SITE_SITEMAP_URL  = "https://www.onepointltd.com/sitemap_index.xml"

# BOTH the allowlist and the priority order: a page is ingested only if its path
# matches one of these, and earlier entries are fetched first so SITE_MAX_PAGES
# trims the tail rather than dropping the certifications page on alphabetical luck.
#
# An allowlist rather than a growing list of exclusions, because the default has to
# be right. Measured 2026-08-25: the 24 product pages were 90,684 chars — half the
# website content — and tracing the brief's phrases back to source showed they were
# restating what the hand-authored capability context already said (Woven,
# Differential, Rapid Value Method, Living Wage all appear in it), while the score
# came out at 75% MEDIUM either way. With an exclude list, next month's product
# page would quietly reintroduce that bulk; with an allowlist a new client story or
# policy is picked up automatically and a new product page is not.
SITE_INCLUDE_PREFIXES = (
    "/core-capabilities",   # the capability statement itself
    "/certifications",      # ISO/Cyber Essentials — Section 4B asks this directly
    "/client-stories",      # named past performance, the strongest public evidence
    "/policies",            # modern slavery, carbon, EDI — public buyers ask
    "/tech-",               # /tech-architecture/, /tech-build/ … the delivery stack
    "/rapid-value-method",  # delivery methods the brief cites by name
    "/valuepath",
    "/discover-onepoint",
    "/",                    # home — matched EXACTLY, never as a prefix
)

# Applied AFTER the allowlist, so it only ever carves out sub-paths of a section
# that is otherwise wanted. Everything else — /insights/, /news/, the product
# pages — is already absent by not being on the allowlist, and listing it here too
# would be config that looks load-bearing but is not.
#
# These five are website governance dressed as policy. Measured 2026-08-25: 26k
# chars of cookie explanations, IP notices and site terms. No buyer assesses a
# supplier on its copyright notice, whereas the policies alongside them
# (anti-bribery, modern slavery, carbon reduction, environmental, EDI, quality) are
# exactly what a public buyer asks for.
SITE_EXCLUDE_PREFIXES = (
    "/policies/cookie-policy",
    "/policies/copyright-policy",
    "/policies/disclaimer",
    "/policies/privacy-notice",
    "/policies/terms-of-website-use",
)

# A safety net against a site that suddenly grows, NOT an active trimmer: the
# allowlist above does the filtering. At 40 the cap was dropping 17 pages on
# alphabetical order within the lowest tier, including /tech-architecture/ and
# /tech-build/ — capability evidence lost to nothing more than the letter T. Raise
# this if the site outgrows it; do not use it to trim.
SITE_MAX_PAGES       = 80
SITE_MAX_PAGE_CHARS  = 8_000    # per page; the home page is the only one near this
SITE_MIN_PAGE_CHARS  = 200      # below this it is a landing shell, not content
SITE_REQUEST_TIMEOUT = 25
SITE_REQUEST_DELAY   = 0.5      # courtesy gap between requests

# Sent when fetching a web page, so the traffic is identifiable in a server log
# rather than looking like an anonymous scraper.
WEB_USER_AGENT = (
    "Mozilla/5.0 (compatible; PS-BidAnalyzer/1.0; +https://www.onepointltd.com)"
)

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

# --- Tender documents: the buyer's own pack, per tender ---------------------
# The third evidence stream, after the capability context and the source corpus.
# Those two describe Onepoint and are the same for every tender; this one is the
# tender's own published pack — the ITT, the draft contract, the code of conduct —
# and it is what turns "Mandatory Requirement" from a guess off the notice summary
# into a quotation from the document a bid would be evaluated against.
#
# Deliberately NOT part of sources.py. That corpus is tender-independent and
# cached on its own cadence; a pack arrives with its tender, so it is fetched
# during the run, per row.
TENDER_DOCS_FOLDER_ID = "16wMwZ_VpJ0GkhoCEUMpWqoqjPfq5QdW3"   # "Tender Documents"
TENDER_DOCS_ENABLED = True

# Subfolder per tender, named "<OCID>-<Tender Title>". Matched on the OCID prefix
# ALONE: it is the OCDS global identifier, stable and verified distinct across all
# 529 tracker rows, whereas the title half gets reworded and truncated. Matching
# on OCID also means the folder's title half can say anything without breaking the
# lookup — and the "Sample Tender #XXX" folders, which carry no OCID, are excluded
# for free rather than needing a skip-list.
TENDER_DOCS_MATCH_FIELD = "OCID"

# Extracted text, one file per tender, keyed by OCID. Cached for the same reason
# the corpus is: the exact text sent to the model stays on disk to be audited, and
# a retried or re-run tender does not re-download and re-parse its whole pack.
# Invalidated by a fingerprint over each file's id, size and modifiedTime, so a
# replaced or added document rebuilds it.
#
# GITIGNORED: a live tender pack is the buyer's material, often under the ITT's own
# confidentiality terms. It must not enter git history.
TENDER_DOCS_CACHE_DIR = os.path.join(KNOWLEDGE_DIR, "tender_docs")

# What can be read. Text is extracted from the downloaded bytes — NOT by converting
# in Drive. Measured 2026-08-23: converting a copy to a Google Doc and exporting it
# works, but takes ~29s per file against ~3s locally, and the copy lands in the
# tender's own folder where the service account CANNOT remove it (it holds
# canEdit but not canDelete/canTrash on that shared drive). Those leftovers would
# then be re-ingested as tender documents on the next run.
DOCX_MIME   = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME    = "application/pdf"
GDOC_MIME   = "application/vnd.google-apps.document"
# Spreadsheets are not optional: a pack ships its pricing schedule and its
# requirements matrix as one, so the most structured document in the pack would
# otherwise be the only one unreadable — and an unreadable document holds its row
# in scope on every run (see TENDER_DOCS_MAX_ATTEMPTS).
XLSX_MIME   = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
TENDER_DOCS_SUPPORTED_MIMES = (DOCX_MIME, PDF_MIME, GDOC_MIME, XLSX_MIME, GSHEET_MIME)

# Total characters of pack text allowed into one prompt, across all documents.
#
# 400k chars is ~100k tokens, which with the corpus (~18k) and the capability
# context (~3k) sits far inside the model's context. The cap is a backstop against
# a pathological pack, NOT a trimming budget: the CITB pack is 194k chars once its
# superseded ITT is dropped, and a first attempt at 120k truncated all three of
# its real documents — an ordinary three-document pack must fit whole, or the
# feature silently degrades into summarising fragments.
#
# Over the cap, documents are capped to a common ceiling rather than dropped, so
# short documents survive intact and only the largest are cut; dropping a 33k
# code of conduct to fit a 90k contract would be the wrong trade. A cut is always
# stated in the prompt text and the manifest, never silent.
TENDER_DOCS_MAX_TOTAL_CHARS = 400_000
TENDER_DOCS_MIN_DOC_CHARS = 2_000   # never cap a document below this

# --- Superseded versions ----------------------------------------------------
# A pack routinely holds both "X ITT.docx" and "X ITT v2.docx". Measured on the
# CITB pack: those two are 98.96% identical but differ SUBSTANTIVELY — one deletes
# the "4 (four) x 1 (one) year extensions" term and changes several evaluation
# figures. Feeding both hands the model contradictory contract terms, and, by the
# same reasoning behind DEDUPE_IDENTICAL_TABS, makes duplicated evidence read as
# stronger evidence.
TENDER_DOCS_DEDUPE_ENABLED = True

# Detection is word-shingle Jaccard, NOT difflib. Measured across the real pack:
#
#   metric                      same document    unrelated documents    cost
#   difflib ratio()                    0.9988        0.0477 - 0.0863    ~7 min
#   difflib quick_ratio()              0.9995        0.4833 - 0.8966    instant
#   5-gram Jaccard                     0.9896        0.0001 - 0.0011    6 ms
#
# difflib's exact ratio separates correctly but costs minutes per pair; its cheap
# quick_ratio scores two entirely unrelated documents at 0.90, because it compares
# character counts and ignores order. Jaccard leaves a ~900x margin between the two
# populations at a fraction of the cost, so the threshold sits nowhere near
# anything.
TENDER_DOCS_SIMILARITY_THRESHOLD = 0.90
TENDER_DOCS_SHINGLE_WORDS = 5

# Which copy wins, once a pair is known to be the same document.
#
# NOT the timestamps. Measured on this pack, both are unusable: modifiedTime is
# IDENTICAL for the two ITTs (it is the source file's mtime, preserved through a
# single upload batch), and createdTime is Drive upload order, which has v2
# created 1.5 SECONDS BEFORE v1 — so "newest wins" would confidently keep the
# stale document. The filename marker is the only signal in the data that is
# actually about the document rather than about how it reached Drive.
#
# A name with no marker at all is treated as version 1, so "ITT v2" beats "ITT".
TENDER_DOCS_VERSION_PATTERNS = (
    r"\bv(?:er(?:sion)?)?\s*[._-]?\s*(\d+(?:\.\d+)*)\b",   # v2, v2.1, ver 3, version 4
    r"\brev(?:ision)?\s*[._-]?\s*(\d+)\b",                 # rev 2, revision 3
    r"\bissue\s*[._-]?\s*(\d+)\b",                         # issue 2
)
TENDER_DOCS_IMPLIED_VERSION = 1.0

# When no marker separates two copies of the same document, BOTH are kept and the
# run warns. Picking one would be a coin flip on contract terms; the same refusal
# to guess is already in report_writer._prefix_match, for the same reason — a brief
# that looks complete and states something false is worse than a flagged gap.
TENDER_DOCS_WARN_UNRESOLVED = True

# --- The document manifest --------------------------------------------------
# Every brief records which documents it was built from — names, sizes, and
# anything superseded, truncated or unreadable. An assessment whose evidence base
# is invisible cannot be checked, which is the same reason the corpus text is
# cached on disk rather than only ever existing inside a prompt.
#
# Always written to the run log and the summary email. To have it land in the
# report as well, add a row to the reporting template whose column A reads
# "Documents Reviewed" (the sheet's structure is maintained by hand) and set this
# to that label; leave it None and no report row is attempted, so no run warns
# about a template row that was never added.
TENDER_DOCS_MANIFEST_FIELD = None

# --- Which rows get a detailed analysis -------------------------------------
# Scope is the single status 'Docs-Ready'. Compared case-insensitively after
# trimming.
#
# This is a better gate than the Bid statuses it originally replaced, and for a
# specific reason: a detailed brief is only worth producing once the tender's
# documents are actually available to read. 'Bid(AI)' says someone thinks the
# opportunity is worth pursuing, which is not the same thing — briefing a tender
# whose pack has not landed yet produces an assessment built on the notice
# summary alone. 'Docs-Ready' asserts the input this stage needs.
#
# Spelled 'Docs-Ready' since 2026-08-25, previously 'Docs(Ready)'. Nothing in this
# project writes it — it is set by hand or by the upstream scraper — so the risk of
# renaming a GATE is not a broken write but a silent one: a row spelled the old way
# would simply never be selected, and this stage has no date filter precisely so
# that a row missed once is picked up later. is_near_miss() below exists to make
# that loud instead.
PROCESS_STATUSES = {"Docs-Ready"}

# Column holding the qualification status used for the filter above.
STATUS_FIELD = "Bid Qualification"

# --- Marking a row complete -------------------------------------------------
# What stops a row being analysed again. On success the row's STATUS_FIELD is
# moved from 'Docs-Ready' to this, which takes it out of PROCESS_STATUSES and
# therefore out of scope — the exit condition this stage previously lacked, and
# the reason the first two runs produced two reports for the same tender.
#
# Note this means the stage DOES write STATUS_FIELD, unlike its earlier design.
# That follows from 'Docs-Ready' being the gate: whatever consumes a workflow
# status has to be what advances it, or the workflow cannot move.
#
# Named for what happened rather than for a generic end-state: a tracker column
# read by people alongside Bid(AI)/NoBid(Human)/Docs-Ready is clearer when every
# value says which step it refers to. Changed from 'Done' 2026-08-25. Nothing
# keys off the literal — scope is PROCESS_STATUSES and this value is simply not in
# it — so rows still carrying the old 'Done' remain out of scope and need no
# migration for correctness, only for a tidy vocabulary.
COMPLETED_STATUS = "Analysis-Complete"
MARK_COMPLETE = True

# --- Where the report link is recorded --------------------------------------
# The dated entry (carrying the report URL) is prepended to the system reason
# column, newest first, exactly as analyzer/main.py does — so one column reads as
# the full history of every automated judgement on the row, both stages included.
# The same entry is appended to Comments, which stays oldest-first.
#
# Note this means the stage now writes the system reason column, which its earlier
# design avoided. That is safe precisely because the convention is prepend: both
# stages add to the top of a shared log rather than overwriting each other, and
# Bid Qualification Reason(Human) is still never touched.
SYSTEM_REASON_FIELD = "Bid Qualification Reason(System)"

# Fields whose text gets URLs turned into real hyperlinks. Measured 2026-08-22:
# Sheets does NOT auto-link a URL embedded in a longer text block under either
# RAW or USER_ENTERED — only a cell that is nothing but the URL becomes clickable.
# Since both these columns are append-only logs, the link has to be applied as
# explicit rich-text runs (see sheets_client.write_updates), which is also why
# this needs no valueInputOption special-casing.
LINK_FIELDS = (SYSTEM_REASON_FIELD, "Comments")

# Only ever marked after a report exists. A row marked Done whose report failed
# to write would be silently stranded — out of scope, with nothing to show for
# it — so a report failure deliberately leaves the row in 'Docs-Ready' for the
# next run to retry.
MARK_COMPLETE_REQUIRES_REPORT = True

# Same reasoning, one level up: a document in the pack that could not be read
# means the brief was written against an incomplete evidence base. The report is
# still produced — it is worth having, and its manifest names what was missing —
# but the row stays in scope so a later run redoes it against the full pack.
#
# Measured 2026-08-23: a run whose pypdf import failed briefed a tender from 2 of
# its 4 documents, wrote that report, and marked the row Done. Nothing was wrong
# with the missing PDF and the import worked minutes later, but the row was out of
# scope for good and only the manifest recorded the gap. A read failure is almost
# never a property of the document, so it is worth one retry.
MARK_COMPLETE_REQUIRES_FULL_PACK = True

# Backstop on the guard above. A file this layer genuinely cannot read — a format
# outside TENDER_DOCS_SUPPORTED_MIMES, or a scanned PDF with no text layer — fails
# identically every run, so holding the status forever means re-analysing that
# tender and depositing ANOTHER timestamped report on every run, none of which the
# service account can delete (it holds canAddChildren but not canDelete on the
# reports drive). The folder already carries four reports for one tender from the
# era before anything moved a row out of scope.
#
# After this many attempts the row is marked COMPLETED_STATUS anyway, and the
# Comments entry says plainly that it completed on an incomplete pack and what to
# do about it. Attempts are counted from the "incomplete pack, attempt N" marker
# in the system reason column, so no extra tracker column is needed. Set to 0 for
# unlimited retries.
TENDER_DOCS_MAX_ATTEMPTS = 3

# Optional belt-and-braces: a column that, when non-empty, takes a row out of
# scope regardless of status. Not needed now that COMPLETED_STATUS moves the row
# out on its own; set it if you later want the status and the completion mark to
# be independent.
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

# --- The report's table name ------------------------------------------------
# The template's grid is a Google Sheets TABLE object, and its name shows as a
# chip above the header row. Every copy inherits the template's name, so without
# this every report is headed "Table1" — the one place in the report that says
# nothing about the tender it covers.
#
# Named for the tender and the run, so the chip identifies the brief the way the
# file name does. Table names are not free text: measured 2026-08-26 against the
# API, a space, underscore, period and apostrophe are all accepted and length is
# not a practical limit, but a HYPHEN is rejected and a name may not START WITH A
# DIGIT — they have to stay compatible with formula references.
RENAME_REPORT_TABLE = True
REPORT_TABLE_NAME_PATTERN = "{title} {runtime}"

# The same instant as the file name's timestamp, to the second, with a space where
# the file name has a hyphen — REPORT_RUNTIME_FORMAT's hyphen is illegal here. The
# digits are identical, so a table and its file still match by eye.
REPORT_TABLE_RUNTIME_FORMAT = "%Y%m%d %H%M%S"

# Only the title is trimmed if the name gets long; the timestamp is what ties the
# table to its file, so it is never cut.
REPORT_TABLE_NAME_MAX_TITLE = 80

# A title starting with a digit ("2026 Framework…") would make an illegal name, so
# it gets this in front. Applied only when needed, so most names are untouched.
REPORT_TABLE_LEADING_DIGIT_PREFIX = "Tender "

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
# Optional extra columns. The tracker has no column for the likelihood score or
# the report link yet; add them to the sheet by hand (its structure is maintained
# manually, not by this code) and map them here to have them written.
#
# Keys are TenderBrief attributes, values are sheet column names, e.g.
#   {"likelihood_summary": "Likelihood of Winning", "report_url": "Detailed Analysis"}
# An empty map is fine — it only means those two values live in the report and the
# email rather than in a tracker column.
OUTPUT_FIELD_MAP = {}

# Master switch for the write-back step. Now TRUE: it was False while the writer
# was unproven, but marking a row 'Done' IS a write, so leaving this off would
# make MARK_COMPLETE silently do nothing and every run would keep re-analysing the
# same rows. The columns written are all ones that already exist — STATUS_FIELD,
# Comments, Processed Date, Last Modified Date — plus anything mapped above.
WRITE_BACK_ENABLED = True


def should_analyse(status: str) -> bool:
    """True when a row's qualification puts it in scope for detailed analysis.

    Mirrors ``analyzer.config.should_analyse``: compared case-insensitively
    after trimming, applied once before the run loop so out-of-scope rows never
    reach the model or the per-row log.
    """
    return (status or "").strip().lower() in {s.lower() for s in PROCESS_STATUSES}


def _squash(status: str) -> str:
    """Reduce a status to letters and digits: 'Docs (Ready)' -> 'docsready'."""
    return re.sub(r"[^a-z0-9]+", "", (status or "").lower())


_SCOPE_SQUASHED = {_squash(s) for s in PROCESS_STATUSES}


def is_near_miss(status: str) -> bool:
    """True for a status that READS as in scope but does not match exactly.

    'Docs(Ready)', 'Docs (Ready)', 'docs_ready' all squash to the same thing as
    'Docs-Ready' and all fail should_analyse(). Nothing here writes the gate value,
    so a row spelled a hair differently is not a bug this code can prevent — but a
    row sitting unprocessed with nobody aware of it is the failure mode this stage
    works hardest to avoid, which is why the run says so loudly rather than
    quietly skipping it.
    """
    return bool((status or "").strip()) and not should_analyse(status) \
        and _squash(status) in _SCOPE_SQUASHED


def already_detailed(row: dict) -> bool:
    """True when this row already carries a completed detailed analysis.

    Always False while ALREADY_DETAILED_FIELD is None (the check is disabled),
    so the caller can rely on it without special-casing the unconfigured state.
    """
    if not ALREADY_DETAILED_FIELD:
        return False
    return bool((row.get(ALREADY_DETAILED_FIELD, "") or "").strip())
