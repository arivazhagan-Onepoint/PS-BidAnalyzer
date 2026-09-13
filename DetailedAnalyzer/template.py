"""
The Bid Analyser reporting template — field model.

A faithful transcription of "Bid Analyser Reporting Template", the live template
named in ``project_config.json`` under ``google_sheets.Reporting_Template`` and
resolved to a file ID at run time (see report_writer.ReportWriter). The labels
here are VERBATIM from that sheet, because the rendered output has to line up
with the template a human reads.

The sheet is THREE columns, not two:

    A  Section       the row label
    B  Detail        the answer
    C  More Details  a second answer, for the rows that ask two questions

Column C is only used where the template's own header row says it is: a
milestone's real-time status assessment, and a fit-matrix domain's Onepoint
evidence. Everywhere else the brief writes B alone and leaves C untouched.

The one idea this module adds is that not every row is the same *kind* of field:

  SHEET     the tracker row already holds the answer. Filled deterministically.
            Handing a model a value and asking it to restate the value is a
            chance for it to restate it wrongly — and a hallucinated submission
            deadline is the single most expensive error this tool could make.
  COMPUTED  arithmetic or a rule (today's date, time remaining, urgency band).
            No judgement involved, so no model involved.
  DERIVED   genuine judgement against the tender text and Onepoint's evidence.
            This is what the LLM is actually for.

What changed when the template moved from Vn_1_0 (60 rows) to this one (88), and
why the code shape changed with it:

  * Section 3's fit assessment used to be generated per tender — the model named
    4-10 dimensions it thought mattered. This template fixes NINE domains in the
    sheet itself (Essential skills … Resource availability), so they are now
    ordinary label-matched rows. Nothing inserts rows or scans for the next
    numbered section any more, which is just as well: three separate sections in
    this template are numbered "3.", and that scan would have stopped at the
    first one.
  * The timeline gate became a milestone table, most of whose dates are not in
    the tracker at all. Those are asked of the model with an explicit instruction
    to say so rather than guess — see MILESTONE_DATES.
  * Vn_1_0 had rows for Route to Market, Security Clearance, Mandatory
    Requirement, Desirable Requirement and On-site presence. This template drops
    all five: Route to Market became a whole section, and the rest are absorbed
    by the Certifications domain and the red/amber flags. They are not modelled
    here, because a field with no row to write to is a field nobody reads.
"""

# Field kinds (see module docstring).
SHEET = "sheet"
COMPUTED = "computed"
DERIVED = "derived"

# --- Section 1 - Executive Summary -------------------------------------------
# New in this template, and deliberately first: the row a reader skims before
# deciding whether to read the other 80.
SECTION_1 = (
    ("An Executive Summary about the Opportunity", DERIVED, None),
)

# --- Section 2 - Opportunity Summary ----------------------------------------
SECTION_2 = (
    ("Client Name", SHEET, "Buyer Name"),
    ("Project Title", SHEET, "Name"),
    ("Public Sector Vertical", DERIVED, "CPV Description"),
    ("Budget (Max/Indicative)", SHEET, "Total Contract Value"),
    ("Is client Budget Approved or under discussion/review?", DERIVED, None),
    ("Contract Length", SHEET, "Contract Duration"),
    # The tracker names the portal a row was scraped from, so this is a lookup,
    # not a judgement. Route to Market's "Portal & Access" row below is the
    # judgement — whether Onepoint can actually transact on it.
    ("Procurement Portal", SHEET, "Portal Name"),
    ("Framework Alignment", DERIVED, None),
    # Judged against the corpus's partner tiers (Boomi Gold, Snowflake, …), not
    # from the tender text alone.
    ("Tech Vendor Alignment", DERIVED, None),
    ("Contract Type", DERIVED, None),
    ("RFP Submission Date", SHEET, "Tender Due Date"),
    ("Delivery Model", DERIVED, None),
    ("Evaluation Weighting", DERIVED, None),
    ("Location", SHEET, "Locality"),
    ("URL of a portal where Tender is Published", SHEET, "Direct URL"),
    ("Opportunity Reference", SHEET, "ID"),
    ("Opportunity Brief and Current state/status/stage", DERIVED, None),
)

# --- Section 3 - Tender Timeline Gate (Critical) -----------------------------
# A milestone table. Column B is the date, column C the real-time status the
# template's own header asks for ("Compare all dates against the current
# real-time date") — computed here, never asked of the model, because a
# countdown is arithmetic.
#
# Only four of these are in the tracker. The rest exist solely inside the
# buyer's documents, so they are DERIVED with an explicit instruction to answer
# "Not stated in the tender documents" rather than produce a plausible date.
SECTION_3_TIMELINE = (
    ("Current Date", COMPUTED, "run_date"),
    ("ITT Issue Date", SHEET, "Published On"),
    ("Clarification Deadline", SHEET, "Clarification Due Date"),
    ("Clarifications Response", DERIVED, None),
    ("Tender Submission Due", SHEET, "Tender Due Date"),
    ("Supplier Presentations", DERIVED, None),
    ("Evaluation Completion", DERIVED, None),
    ("Contract Commencement", DERIVED, None),
    ("Submission Deadline", SHEET, "Tender Due Date"),
    ("Time Remaining", COMPUTED, "time_remaining"),
    ("Urgency Status", COMPUTED, "urgency"),
)

# The milestone rows that hold an actual date, and so get a status in column C.
# "Current Date" is the reference point rather than a milestone, and the last two
# are already expressed relative to today, so none of the three is compared
# against itself.
MILESTONE_DATES = (
    "ITT Issue Date",
    "Clarification Deadline",
    "Clarifications Response",
    "Tender Submission Due",
    "Supplier Presentations",
    "Evaluation Completion",
    "Contract Commencement",
    "Submission Deadline",
)

SECTION_3_TIMELINE_VERDICT = (
    ("Timeline Assessment Verdict", DERIVED, None),
)

# --- Section 3 - Route to Market (Critical) ---------------------------------
# Vn_1_0 asked this as a single row. It is now six, because the answer that
# matters is rarely the procurement route on its own — it is whether Onepoint is
# on the vehicle that route runs through.
SECTION_3_ROUTE = (
    ("Portal & Access", DERIVED, None),
    ("Procurement route", DERIVED, None),
    ("Relevant framework", DERIVED, None),
    ("DPS / direct award / mini competition status", DERIVED, None),
    ("Whether Onepoint has access to the procurement vehicle", DERIVED, None),
    ("Summary", DERIVED, None),
)

# --- Section 3 - Fit Assessment (Matrix Check) ------------------------------
# Fixed in the sheet, so fixed here. Each domain asks two questions, which is why
# the template gives it two columns:
#   B  Required Capability                  — what THIS tender demands of it
#   C  Onepoint Evidence & Capability Status — what the corpus can evidence
# The rating travels in C alongside the evidence, because a status without the
# evidence behind it is an assertion.
FIT_DOMAINS = (
    "Essential skills",
    "Technical capabilities",
    "Industry experience",
    "Certifications",
    "Delivery methodology",
    "Public sector experience",
    "Security capability",
    "Partner ecosystem alignment",
    "Resource availability",
)

FIT_RATINGS = ("STRONG", "PARTIAL", "WEAK", "NONE")

# --- Section 3A / 3B / 3C - Flags -------------------------------------------
# The template writes these rows as instructions to the reader ("List all gaps
# where:" followed by four bullets), which makes poor prompt keys. Each is given
# a readable field name here and mapped to the row's own first line for matching.
SECTION_3_FLAGS = (
    ("Green Flags / Strengths", DERIVED, None),
    ("Amber Flags / Mitigations", DERIVED, None),
    ("Red Flags / Critical Risks", DERIVED, None),
)

# field label -> the first line of the row as the sheet actually writes it.
# Consulted by report_writer when a field's own label is not what column A says.
SHEET_LABEL_ALIASES = {
    "Green Flags / Strengths": "List all strong alignment indicators.",
    "Amber Flags / Mitigations": "List all gaps where mitigation may be possible.",
    "Red Flags / Critical Risks": "List all gaps where:",
}

# --- Section 4 - Qualifying Factors -----------------------------------------
# Five bolded sub-gates in the template. Kept as separate groups because each
# answers a different question and they are weighed differently in Section 5.
SECTION_4 = (
    ("4A. Is it real?", (
        ("Customer has intent to buy or intent to collect data like Expression "
         "of interest/ further research etc?", DERIVED, None),
        ("Customer has Defined procurement timeline and project start date?", DERIVED, None),
        ("Is there any incumbent supplier?", DERIVED, None),
    )),
    ("4B. Can we win it?", (
        ("Any type of certifications are required (e.g. ISO, Cyber security etc.)?", DERIVED, None),
        ("Does Onepoint have Competitive edge? If so, in which area of the "
         "tender requirement?", DERIVED, None),
        ("Whether Onepoint has Solution / tech capabilities to deliver the project?", DERIVED, None),
        ("Does Onepoint have Relevant experience / case studies / testimonials?", DERIVED, None),
    )),
    ("4C. Risks", (
        ("Commercial Risk", DERIVED, None),
        ("Technical Risk", DERIVED, None),
        ("Timeline Risk", DERIVED, None),
        ("Delivery Risk", DERIVED, None),
    )),
    ("4D. Can we deliver it?", (
        ("State specific deliverables mentioned in Tender & against each "
         "deliverable, share the strategy using which Onepoint can deliver it", DERIVED, None),
        ("What kind of Delivery team and Skill sets are required and for how "
         "long? - Preferred in tabular format", DERIVED, None),
        ("Any Contractual commitments / Insurances / SLAs", DERIVED, None),
    )),
    ("4E. Do we want it?", (
        ("Is it in Onepoint's sweet spot and aligns with the strategy and "
         "opportunity criteria?", DERIVED, None),
        ("Is it long term strategic work or One off client project?", DERIVED, None),
    )),
)

# --- Section 5 - Final Recommendation ---------------------------------------
SECTION_5 = (
    ("Likelihood of Winning", DERIVED, None),
    ("Explanation of Likelihood of Winning percentage", DERIVED, None),
    ("Recommendation", DERIVED, None),
)

# The template spells the bands out inside the "Likelihood of Winning" label.
# Transcribed here as (label, low, high), inclusive, so the rendered band is
# derived from the percentage rather than left to the model to name.
#
# HIGH's floor is 76, NOT the 75 written on the template. The template's bands
# (HIGH 75-89) disagreed with analyzer/config.py by exactly one point: there,
# score > 75 is Bid, so 75 is TBD. A tender landing on 75 would have been "HIGH
# likelihood — Bid" in this brief and "TBD" in the tracker at the same time.
# Reconciled here rather than in the analyzer, on the user's decision (2026-08-21),
# because moving the analyzer's threshold instead would have made every future
# qualification one point more generous — and Bid is the expensive direction to
# cross by mistake, whereas TBD stays recoverable via ReCheck.
#
# This template restates the same bands and leaves the same gap (its MEDIUM stops
# at 74, its HIGH starts at 75). The reconciliation is unchanged: 75 stays MEDIUM.
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

# Section headings, verbatim, in the sheet's own order. Three of them are
# numbered "3." — that is the template's own numbering, transcribed rather than
# corrected, so the rendered brief matches the document a human reads.
HEADING_1 = "1. Executive Summary"
HEADING_2 = "2. Opportunity Summary"
HEADING_3_TIMELINE = "3. Tender Timeline Gate (Critical)"
HEADING_3_ROUTE = "3. Route to Market (Critical)"
HEADING_3_FIT = "3. Fit Assessment (Matrix Check)"
HEADING_3A = "3A. Green Flags / Strengths"
HEADING_3B = "3B. Amber Flags / Mitigations"
HEADING_3C = "3C. Red Flags / Critical Risks"
HEADING_4 = "4. Qualifying Factors"
HEADING_5 = "5. Final Recommendation"


def band_for(percentage: float) -> str:
    """Return the template's likelihood band label for a 0-100 percentage."""
    pct = max(0.0, min(100.0, float(percentage)))
    for label, low, high in LIKELIHOOD_BANDS:
        if low <= pct <= high:
            return label
    return "LOW"


def sheet_label(field_label: str) -> str:
    """The column A text a field is matched against — its own label unless the
    sheet writes that row as an instruction (see SHEET_LABEL_ALIASES)."""
    return SHEET_LABEL_ALIASES.get(field_label, field_label)


def section_rows():
    """Yield ``(section_title, field_label, kind, source_field)`` for the whole
    template, in the sheet's own row order.

    The fit matrix yields no field rows here — its nine domains carry two values
    each and are walked through FIT_DOMAINS — but its heading is emitted so the
    renderer keeps the template's shape.
    """
    yield (HEADING_1, None, None, None)
    for label, kind, src in SECTION_1:
        yield (None, label, kind, src)

    yield (HEADING_2, None, None, None)
    for label, kind, src in SECTION_2:
        yield (None, label, kind, src)

    yield (HEADING_3_TIMELINE, None, None, None)
    for label, kind, src in SECTION_3_TIMELINE:
        yield (None, label, kind, src)
    for label, kind, src in SECTION_3_TIMELINE_VERDICT:
        yield (None, label, kind, src)

    yield (HEADING_3_ROUTE, None, None, None)
    for label, kind, src in SECTION_3_ROUTE:
        yield (None, label, kind, src)

    yield (HEADING_3_FIT, None, None, None)

    for heading, (label, kind, src) in zip(
            (HEADING_3A, HEADING_3B, HEADING_3C), SECTION_3_FLAGS):
        yield (heading, None, None, None)
        yield (None, label, kind, src)

    yield (HEADING_4, None, None, None)
    for group, fields in SECTION_4:
        yield (group, None, None, None)
        for label, kind, src in fields:
            yield (None, label, kind, src)

    yield (HEADING_5, None, None, None)
    for label, kind, src in SECTION_5:
        yield (None, label, kind, src)


def derived_fields() -> list:
    """Every single-answer field the model is responsible for — what the prompt
    must ask for. The fit matrix is excluded: it is asked for separately because
    each of its domains needs two answers and a rating."""
    return [
        label for _, label, kind, _ in section_rows()
        if label and kind == DERIVED
    ]


def derived_date_fields() -> list:
    """The milestone dates the model has to read out of the tender pack.

    Separated from the rest so the prompt can hold them to a different standard:
    every other derived field wants prose, these want a date or an admission that
    the documents do not give one.
    """
    return [
        label for label, kind, _ in SECTION_3_TIMELINE
        if kind == DERIVED
    ]


def deterministic_fields() -> list:
    """Every field filled from the tracker row or computed, never by the model."""
    return [
        (label, kind, src) for _, label, kind, src in section_rows()
        if label and kind in (SHEET, COMPUTED)
    ]
