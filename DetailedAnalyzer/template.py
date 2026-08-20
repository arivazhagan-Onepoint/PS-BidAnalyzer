"""
The Bid Analyser reporting template — field model.

A faithful transcription of "Bid Analyser Reporting Template_Current_Version"
(Google Sheet `1ImvX_fN7UHfgLFXV1to5V2pTZrGSJStPw3rHnBn6FLA`), which is a
two-column brief: column A the section/field label, column B the detail. The
labels here are VERBATIM from that sheet, because the rendered output has to line
up with the template a human reads.

The one idea this module adds is that not every row is the same *kind* of field:

  SHEET     the tracker row already holds the answer. Filled deterministically.
            Handing a model a value and asking it to restate the value is a
            chance for it to restate it wrongly — and a hallucinated submission
            deadline is the single most expensive error this tool could make.
  COMPUTED  arithmetic or a rule (today's date, time remaining, urgency band).
            No judgement involved, so no model involved.
  DERIVED   genuine judgement against the tender text and Onepoint's evidence.
            This is what the LLM is actually for.

Section 3 is special: its rows are not fixed. In the Met Office example they are
"Weather/Geospatial Data", "Umbraco / Web Build" and so on — dimensions drawn
from that tender's requirements. So the section is generated per tender rather
than filled in, and its length varies.
"""

# Field kinds (see module docstring).
SHEET = "sheet"
COMPUTED = "computed"
DERIVED = "derived"

# --- Section 1 - Tender Timeline Gate (Critical) -----------------------------
# Called "Critical" in the template and treated as a gate: if the deadline has
# passed or clearance is mandatory and absent, the rest of the brief is academic.
SECTION_1 = (
    ("URL of a portal where Tender is Published", SHEET, "Direct URL"),
    ("Submission Deadline", SHEET, "Tender Due Date"),
    ("Current Date", COMPUTED, "run_date"),
    ("Time Remaining", COMPUTED, "time_remaining"),
    ("Urgency Status", COMPUTED, "urgency"),
    # Procurement Stage is the closest tracker field, but route to market
    # (open procedure / framework / DPS / direct award) usually has to be read
    # out of the notice text, so this is derived with the field as a hint.
    ("Route to Market", DERIVED, "Procurement Stage"),
    # SC_Flag is a boolean hint; the actual clearance level and whether it is
    # mandatory or desirable comes from the text.
    ("Security Clearance", DERIVED, "SC_Flag"),
    ("Opportunity Brief and Current state/status/stage", DERIVED, None),
    ("Mandatory Requirement", DERIVED, None),
    ("Desirable Requirement", DERIVED, None),
    ("On-site presence required?", DERIVED, None),
)

# --- Section 2 - Opportunity Summary ----------------------------------------
SECTION_2 = (
    ("Client Name", SHEET, "Buyer Name"),
    ("Project Title", SHEET, "Name"),
    ("Budget (Max/Indicative)", SHEET, "Total Contract Value"),
    ("Is client Budget Approved or under discussion/review?", DERIVED, None),
    ("Contract Length", SHEET, "Contract Duration"),
    ("Opportunity Reference", SHEET, "ID"),
    ("Public Sector Vertical", DERIVED, "CPV Description"),
    # Judged against the corpus's partner tiers (Boomi Gold, Snowflake, …), not
    # from the tender text alone.
    ("Tech Vendor Alignment", DERIVED, None),
    ("Contract Type", DERIVED, None),
    ("RFP Submission Date", SHEET, "Tender Due Date"),
    ("Location", SHEET, "Locality"),
)

# --- Section 3 - Fit Assessment (Matrix Check) ------------------------------
# Rows are generated per tender: the model reads the requirements, names the
# capability dimensions that matter for THIS tender, and assesses each against
# the ingested corpus. MIN/MAX bound it so a brief is neither thin nor a list of
# forty near-duplicates.
SECTION_3_MIN_DIMENSIONS = 4
SECTION_3_MAX_DIMENSIONS = 10

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
    """Return the template's likelihood band label for a 0-100 percentage."""
    pct = max(0.0, min(100.0, float(percentage)))
    for label, low, high in LIKELIHOOD_BANDS:
        if low <= pct <= high:
            return label
    return "LOW"


def section_rows():
    """Yield ``(section_title, field_label, kind, source_field)`` for the whole
    template, in the sheet's own row order.

    Section 3 yields no field rows — its rows are generated per tender — but its
    heading is emitted so the renderer keeps the template's shape.
    """
    yield ("1. Tender Timeline Gate (Critical)", None, None, None)
    for label, kind, src in SECTION_1:
        yield (None, label, kind, src)

    yield ("2. Opportunity Summary", None, None, None)
    for label, kind, src in SECTION_2:
        yield (None, label, kind, src)

    yield ("3. Fit Assessment (Matrix Check)", None, None, None)

    yield ("4. Qualifying Factors", None, None, None)
    for group, fields in SECTION_4:
        yield (group, None, None, None)
        for label, kind, src in fields:
            yield (None, label, kind, src)

    yield ("5. Final Recommendation", None, None, None)
    for label, kind, src in SECTION_5:
        yield (None, label, kind, src)


def derived_fields() -> list:
    """Every field the model is responsible for — what the prompt must ask for."""
    return [
        label for _, label, kind, _ in section_rows()
        if label and kind == DERIVED
    ]


def deterministic_fields() -> list:
    """Every field filled from the tracker row or computed, never by the model."""
    return [
        (label, kind, src) for _, label, kind, src in section_rows()
        if label and kind in (SHEET, COMPUTED)
    ]
