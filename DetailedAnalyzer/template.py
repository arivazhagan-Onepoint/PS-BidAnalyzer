"""
What the code knows about the reporting template that the sheet cannot say.

The template's *structure* — its sections, tables, data rows and the yellow
instruction cells — is read from the published document at run time by
``template_reader``. This module holds only what a spreadsheet has no way to
express:

  * which rows the tracker or the clock already answers, so the model is never
    asked to restate a value that is already in hand;
  * how a percentage maps to the template's likelihood bands, including the one
    point where the template and the analyzer had to be reconciled;
  * which rows are the likelihood itself, so the band label follows the number
    rather than being written twice by hand.

Everything else about the document is the document's business.

Why the deterministic rows are matched by LABEL rather than by row number: the
template renumbers its sections and moves rows between revisions (Fit Assessment
was section 3, then 4), but "Client Name" has meant the buyer's name in every
version. A label survives a reshuffle; a row number does not.
"""
import re

# Field kinds.
SHEET = "sheet"        # the tracker row already holds the answer
COMPUTED = "computed"  # arithmetic or a rule — today's date, a countdown
DERIVED = "derived"    # judgement, which is what the model is for


def _key(label: str) -> str:
    """Normalised lookup key — the first line of a label, punctuation stripped.

    Template rows often carry guidance on continuation lines ("Timeline
    Assessment Verdict" then "( Based on the Dates give a detaled analysis
    here )"), so only the first line identifies the field.
    """
    first = (label or "").replace("\r", "\n").split("\n")[0]
    return re.sub(r"[^a-z0-9]+", " ", first.lower()).strip()


# Rows filled from the tracker row or the clock, never by the model. A
# hallucinated submission deadline is the single most expensive error this tool
# could make, and there is no reason to risk it on data already in hand.
#
# Keyed by _key(label). The value is (kind, tracker column or rule name). Some
# have extra handling in detailed_analyzer._deterministic_value — Location joins
# Locality and Country, Budget prefers the total and says which figure it used,
# Opportunity Reference falls back to the OCID.
DETERMINISTIC = {
    _key("Client Name"):            (SHEET, "Buyer Name"),
    _key("Project Title"):          (SHEET, "Name"),
    _key("Budget (Max/Indicative)"): (SHEET, "Total Contract Value"),
    _key("Contract Length"):        (SHEET, "Contract Duration"),
    # The tracker names the portal a row was scraped from, so this is a lookup.
    # Route to Market's "Portal & Access" row is the judgement — whether Onepoint
    # can actually transact on it — and stays with the model.
    _key("Procurement Portal"):     (SHEET, "Portal Name"),
    _key("RFP Submission Date"):    (SHEET, "Tender Due Date"),
    _key("Location"):               (SHEET, "Locality"),
    _key("URL of a portal where Tender is Published"): (SHEET, "Direct URL"),
    _key("Opportunity Reference"):  (SHEET, "ID"),

    # The milestone table. Four of its dates are in the tracker; the rest exist
    # only inside the buyer's documents and are asked of the model under a
    # stricter instruction (see MILESTONE_DATES and the prompt).
    _key("Current Date"):           (COMPUTED, "run_date"),
    _key("ITT Issue Date"):         (SHEET, "Published On"),
    _key("Clarification Deadline"): (SHEET, "Clarification Due Date"),
    _key("Tender Submission Due"):  (SHEET, "Tender Due Date"),
    _key("Submission Deadline"):    (SHEET, "Tender Due Date"),
    _key("Time Remaining"):         (COMPUTED, "time_remaining"),
    _key("Urgency Status"):         (COMPUTED, "urgency"),
}

# Milestone rows holding an actual date, which therefore get a real-time status
# in the table's third column. "Current Date" is the reference point rather than
# a milestone, and the last two are already expressed relative to today, so none
# of the three is compared against itself.
MILESTONE_DATES = frozenset({
    _key("ITT Issue Date"),
    _key("Clarification Deadline"),
    _key("Clarifications Response"),
    _key("Tender Submission Due"),
    _key("Supplier Presentations"),
    _key("Evaluation Completion"),
    _key("Contract Commencement"),
    _key("Submission Deadline"),
})

# Rows whose value IS a date, wherever it comes from — so the code can render
# them in the brief's own date format rather than passing the tracker's storage
# format through. The milestone rows plus the one date that sits outside that
# table.
DATE_VALUED = frozenset({
    _key("ITT Issue Date"),
    _key("Clarification Deadline"),
    _key("Clarifications Response"),
    _key("Tender Submission Due"),
    _key("Supplier Presentations"),
    _key("Evaluation Completion"),
    _key("Contract Commencement"),
    _key("Submission Deadline"),
    _key("RFP Submission Date"),
})

# Milestone rows the tracker cannot answer. Asked of the model as dates, with an
# explicit instruction to admit silence rather than produce a plausible one.
DERIVED_DATES = frozenset({
    _key("Clarifications Response"),
    _key("Supplier Presentations"),
    _key("Evaluation Completion"),
    _key("Contract Commencement"),
})

# The template spells the bands out inside its own labels. Transcribed here as
# (label, low, high), inclusive, so the rendered band follows the percentage
# rather than being named by the model.
#
# HIGH's floor is 76, NOT the 75 the template prints. The template's bands
# disagreed with analyzer/config.py by exactly one point: there, score > 75 is
# Bid, so 75 is TBD. A tender landing on 75 would have read "HIGH likelihood —
# Bid" in this brief and "TBD" in the tracker at the same time. Reconciled here
# rather than in the analyzer, on the user's decision (2026-08-21), because
# moving the analyzer's threshold would have made every future qualification one
# point more generous — and Bid is the expensive direction to cross by mistake,
# whereas TBD stays recoverable via ReCheck.
LIKELIHOOD_BANDS = (
    ("VERY HIGH", 90, 100),
    ("HIGH",      76, 89),
    ("MEDIUM",    51, 75),
    ("LOW",        0, 50),
)
LIKELIHOOD_QUALIFICATION = {
    "VERY HIGH": "Bid",
    "HIGH":      "Bid",
    "MEDIUM":    "TBD",
    "LOW":       "NoBid",
}


def band_for(percentage: float) -> str:
    """The template's likelihood band label for a 0-100 percentage."""
    pct = max(0.0, min(100.0, float(percentage)))
    for label, low, high in LIKELIHOOD_BANDS:
        if low <= pct <= high:
            return label
    return "LOW"


def deterministic_for(label: str):
    """(kind, source) when this row is filled without the model, else None."""
    return DETERMINISTIC.get(_key(label))


def is_derived_date(label: str) -> bool:
    return _key(label) in DERIVED_DATES


def is_milestone_date(label: str) -> bool:
    return _key(label) in MILESTONE_DATES


def is_date_value(label: str) -> bool:
    """True when this row's value is a date and should be rendered as one."""
    return _key(label) in DATE_VALUED


def is_likelihood_row(label: str) -> bool:
    """True for the rows that state the likelihood itself.

    The current template asks for it twice — once as its own section ("Give us a
    likelihood score… | VERY HIGH | 90%-100% |") and again in Final
    Recommendation. Both are filled from the one percentage the model gives, so
    the two cannot contradict each other. The explanation row alongside them is
    excluded: it is prose about the number, not the number.
    """
    text = (label or "").lower()
    if "explanation" in text:
        return False
    return "likelihood" in text and ("score" in text or "of winning" in text)
