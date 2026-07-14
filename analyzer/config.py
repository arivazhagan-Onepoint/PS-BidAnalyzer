"""
Analyzer configuration.

Shared settings (DATASET_FIELDS, Google Sheet target, credentials paths, UK
timezone) live in the project root ``config.py``. This module re-exports those
and layers on the analyzer-specific settings: the LLM provider/model, the score
thresholds that map an analysis score to Bid / NoBid / TBD, and the location of
the Onepoint capability context used to ground the analysis.
"""
import os

# Re-export all shared project configuration (DATASET_FIELDS, SHEET_NAME,
# TARGET_FOLDER_ID, SCOPES, SERVICE_ACCOUNT_FILE, UK_TIMEZONE, CREDENTIALS_DIR…)
from config import *          # noqa: F401, F403
from config import CREDENTIALS_DIR, UK_TIMEZONE  # explicit for linters

# --- Paths ------------------------------------------------------------------
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
LOG_FILE       = os.path.join(BASE_DIR, "analyzer.log")
KNOWLEDGE_DIR  = os.path.join(BASE_DIR, "knowledge")

# Onepoint capability context injected into the analysis prompt. Populate this
# file from the NotebookLM sources listed in Requirements.md.
ONEPOINT_CONTEXT_FILE = os.path.join(KNOWLEDGE_DIR, "onepoint_capabilities.md")

# --- Gemini model (active provider) -----------------------------------------
# The analyzer talks to Google's Gemini API directly (native google-genai SDK).
# There is intentionally NO fallback: if Gemini fails, the call fails and the
# tender is recorded as a NoBid pending manual review.
GEMINI_CREDENTIALS_FILE = os.path.join(CREDENTIALS_DIR, "gemini_credentials.json")
GEMINI_API_KEY_FIELD    = "gemini_api_key"
# Native Gemini model id (note: no "google/" prefix, unlike OpenRouter). Using
# the flash-lite tier to keep token usage/cost down for this batch scoring task.
GEMINI_MODEL            = "gemini-3.1-flash-lite"

# Model actually used by the analyzer.
ANALYZER_MODEL       = GEMINI_MODEL
ANALYZER_TEMPERATURE = 0.2
ANALYZER_MAX_TOKENS  = 700

# Gemini 3.x are "thinking" models: reasoning tokens are drawn from the same
# max_output_tokens budget, so with thinking on the whole budget can be consumed
# before any JSON is emitted (finish_reason=MAX_TOKENS). This is a short, well-
# specified scoring task, so thinking is disabled (0) for reliable, cheap, fully
# formed JSON. Raise it (e.g. 512, or -1 for dynamic) if richer reasoning is
# wanted — but then also raise ANALYZER_MAX_TOKENS so output still fits.
ANALYZER_THINKING_BUDGET = 0

# A model call can fail transiently (network blip, upstream 5xx, an empty or
# truncated reply that fails JSON parsing). These are usually transient, so retry
# the call a few times before giving up and falling back to NoBid.
ANALYZER_MAX_RETRIES = 3

# Seconds to sleep after each API call to stay within provider rate limits.
API_THROTTLE_SECONDS = 10

# --- OpenRouter model (BACKUP — kept for future reference, not used) ---------
# The project previously routed the analyzer through OpenRouter (OpenAI SDK
# against openrouter.ai). That path is retained in openrouter_client.py and the
# settings below purely as a reference/backup; nothing in the active analyzer
# imports them. To switch back, point analyzer.py at openrouter_client.get_client()
# and use OPENROUTER_MODEL below.
OPENROUTER_CREDENTIALS_FILE = os.path.join(CREDENTIALS_DIR, "openrouter_credentials.json")
OPENROUTER_API_KEY_FIELD    = "openrouter_api_key"
OPENROUTER_BASE_URL         = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL            = "google/gemini-2.5-flash"

# --- Score → qualification thresholds ---------------------------------------
# Bid    : score  > 75
# TBD    : 51 <= score <= 75
# NoBid  : score <= 50
BID_THRESHOLD = 75   # strictly above -> Bid
TBD_THRESHOLD = 51   # at or above (and <= BID_THRESHOLD) -> TBD; below -> NoBid

QUALIFICATION_BID   = "Bid(AI)"
QUALIFICATION_TBD   = "TBD(AI)"
QUALIFICATION_NOBID = "NoBid(AI)"

# --- Which rows to analyse --------------------------------------------------
# Only tenders whose [Bid Qualification] is one of these system-assigned values
# are analysed. This is intentional:
#   * 'PreQualified' — written by the scraper's automated qualification step.
#   * 'ReCheck'      — set automatically when a previously-NoBid tender changed
#                      and needs re-evaluation.
# Both are system values, so filtering on them (a) restricts the analyzer to
# PreQualified/ReCheck tenders and (b) skips manual overrides — any human-set
# decision (Bid, Won, Lost, a hand-typed NoBid, …) is by definition not in this
# set. It also skips rows this analyzer already processed, since those become
# Bid/TBD/NoBid. Compared case-insensitively after trimming.
PROCESS_STATUSES = {"PreQualified", "ReCheck"}

# Column holding the qualification status used for the filter above.
STATUS_FIELD = "Bid Qualification"


def should_analyse(status: str) -> bool:
    """True for system PreQualified/ReCheck statuses (skips manual overrides)."""
    return (status or "").strip().lower() in {s.lower() for s in PROCESS_STATUSES}


# --- One-day window ---------------------------------------------------------
# The analyzer only processes rows dated within a single day. The window is
# anchored on this column. 'Last Modified Date' is used because the scraper
# stamps it to the run time both for newly-created PreQualified rows and for
# ReCheck rows it re-flags (whose Created Date is older) — so it captures every
# row worth analysing on a given day. Change to 'Created Date', 'Published On',
# etc. in one place if a different anchor is wanted.
WINDOW_DATE_FIELD = "Last Modified Date"


def in_day_window(cell_value: str, target_date: str) -> bool:
    """True if the cell's date falls on target_date (a 'YYYY-MM-DD' string).

    Tolerates both ISO timestamps ('2026-07-03T13:16:00+01:00') and plain dates
    ('2026-07-03') by comparing only the leading date portion. Empty/unparseable
    cells return False so undated rows are excluded from the window.
    """
    value = (cell_value or "").strip()
    return len(value) >= 10 and value[:10] == target_date


def score_to_qualification(score: float) -> str:
    """Map an analysis score (0-100) to a Bid / TBD / NoBid qualification."""
    if score > BID_THRESHOLD:
        return QUALIFICATION_BID
    if score >= TBD_THRESHOLD:
        return QUALIFICATION_TBD
    return QUALIFICATION_NOBID


# --- Qualification families (prefix-based) ----------------------------------
# Statuses carry a suffix (e.g. 'Bid(AI)', 'Bid(Human)', 'TBD(AI)', 'NoBid'), so
# classification and row-colouring match on the PREFIX rather than an exact word.
# Ordered longest-first so 'NoBid…' is never mis-classified as 'Bid…'.
QUALIFICATION_FAMILIES = ("NoBid", "TBD", "Bid")


def qualification_family(status: str) -> str:
    """Return the family ('Bid' / 'TBD' / 'NoBid') a status belongs to by prefix.

    'Bid(AI)', 'Bid(Human)', 'Bid' -> 'Bid'; 'NoBid(AI)' -> 'NoBid'; etc.
    Returns None for statuses in no family (PreQualified, ReCheck, blank…).
    """
    s = (status or "").strip().lower()
    for family in QUALIFICATION_FAMILIES:
        if s.startswith(family.lower()):
            return family
    return None
