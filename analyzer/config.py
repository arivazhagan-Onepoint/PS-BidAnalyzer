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
from config import (  # explicit for linters
    CREDENTIALS_DIR,
    UK_TIMEZONE,
    NOBIDS_SHEET_NAME,
    BIDS_SHEET_NAME,
)

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

# --- Bid knowledge maintenance (analyzer.maintain_knowledge) ----------------
# A separate, scheduled maintenance flow (NOT the per-tender analysis) keeps the
# bid knowledge current in two steps, both driven by the KNOWLEDGE_SOURCES table
# below so each polarity is handled by identical code:
#   Step 1  extract: sync rows whose Bid Qualification exactly matches a source's
#           status from the main tab into that source's tab (deduped).
#   Step 2  distil : consolidate that tab's human reasons into general decision
#           heuristics written to the source's patterns file.
# Only human-set rows feed these tabs — never the analyzer's own NoBid(AI)/Bid(AI)
# — to avoid a self-reinforcing feedback loop of the analyzer learning from itself.
COPY_TO_NOBIDS_STATUS = "NoBid(Human)"
COPY_TO_BIDS_STATUS   = "Bid(Human)"

# Distilled heuristics artifacts (generated by Step 2, cached like
# ONEPOINT_CONTEXT_FILE). Absent/empty => analysis injects nothing.
# Both are injected into the analysis prompt as separate, fenced precedent blocks
# (analyzer.patterns loads them): NoBid heuristics calibrate a score DOWN, Bid
# heuristics calibrate it UP. Deleting a file cleanly disables its block.
NOBID_PATTERNS_FILE = os.path.join(KNOWLEDGE_DIR, "nobid_patterns.md")
BID_PATTERNS_FILE   = os.path.join(KNOWLEDGE_DIR, "bid_patterns.md")

# Distillation call settings. Runs rarely (scheduled), and emits longer markdown
# than the analyzer's tiny JSON, so it gets its own, larger output budget rather
# than reusing ANALYZER_MAX_TOKENS (700). Reuses ANALYZER_MODEL. Shared by both
# polarities — the distillation task is the same shape either way.
DISTILL_MAX_TOKENS      = 3000
DISTILL_TEMPERATURE     = 0.3
DISTILL_THINKING_BUDGET = 0

# Insufficient-data guard for Step 2: reasons containing any of these markers
# (case-insensitive) are treated as placeholder/test junk, not genuine signal.
# If fewer than a source's min_examples distinct genuine reasons remain, Step 2
# skips regeneration for that source and keeps its existing patterns file. The
# Bid minimum is lower because human Bid decisions are rarer than rejections.
JUNK_MARKERS       = ("test",)
NOBID_MIN_EXAMPLES = 5
BID_MIN_EXAMPLES   = 3

# One entry per polarity, consumed by both maintenance steps. Adding an entry
# here extends extraction AND distillation with no further code changes.
#   decision  : how the human decision reads in the distillation prompt
#   directive : what the model should produce from those reasons
#   verb      : the imperative each generated bullet must open with (see below)
#
# Why `verb` exists. The distilled file is written by one LLM call and read by
# another (the per-tender scoring call), so its GRAMMATICAL FORM is part of the
# analyzer's behaviour, not cosmetic. Measured on 2026-08-05, same rules and same
# tender, varying only the lead phrase:
#     "Decline tenders that mandate SC clearance…"     -> score 0-10
#     "Onepoint has tended not to pursue tenders…"     -> score 10-15
#     "Give low priority to tenders with…"             -> score 15 (75 on a
#         borderline tender — one point below the Bid threshold)
# An imperative reads as a constraint; a description reads as a tendency the model
# may trade off. Gemini chose imperatives unprompted (15/15 bullets over 5
# regenerations), but nothing guaranteed it: a change of examples or model could
# silently soften every future regeneration, weakening suppression across every
# tender scored afterwards with nothing in the log to show it. Pinning the verb
# makes the strength of the precedent a property of this config rather than a
# lucky default.
KNOWLEDGE_SOURCES = (
    {
        "polarity":      "NoBid",
        "status":        COPY_TO_NOBIDS_STATUS,
        "tab":           NOBIDS_SHEET_NAME,
        "patterns_file": NOBID_PATTERNS_FILE,
        "min_examples":  NOBID_MIN_EXAMPLES,
        "title":         "Onepoint NoBid Decision Heuristics",
        "decision":      "decided NOT to bid on",
        "verb":          "Decline",
        "directive": (
            "general NoBid decision heuristics: the recurring patterns and criteria "
            "that explain why Onepoint declines tenders"
        ),
    },
    {
        "polarity":      "Bid",
        "status":        COPY_TO_BIDS_STATUS,
        "tab":           BIDS_SHEET_NAME,
        "patterns_file": BID_PATTERNS_FILE,
        "min_examples":  BID_MIN_EXAMPLES,
        "title":         "Onepoint Bid Decision Heuristics",
        "decision":      "decided TO bid on",
        "verb":          "Pursue",
        "directive": (
            "general Bid decision heuristics: the recurring patterns and criteria "
            "that explain why Onepoint pursues tenders. These describe commercial "
            "appetite and winnable fit — never treat them as an extension of "
            "Onepoint's documented capabilities"
        ),
    },
)

# Step 1 pairs, derived from the table above (kept for call sites that only need
# the extract mapping).
EXTRACT_SOURCES = tuple((s["status"], s["tab"]) for s in KNOWLEDGE_SOURCES)


def genuine_reasons(reasons) -> list:
    """Distinct, non-junk human reasons from an iterable of raw reason strings.

    Polarity-agnostic — used for both the Bid and NoBid sides. Drops blanks and
    any reason containing a JUNK_MARKERS token (e.g. 'test'); de-duplicates
    case-insensitively while preserving first-seen order and the original casing.
    Used by the Step 2 guard and the distiller so both agree on what counts as a
    genuine reason.
    """
    seen, out = set(), []
    for raw in reasons:
        r = (raw or "").strip()
        if not r:
            continue
        low = r.lower()
        if any(marker in low for marker in JUNK_MARKERS):
            continue
        if low in seen:
            continue
        seen.add(low)
        out.append(r)
    return out


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
