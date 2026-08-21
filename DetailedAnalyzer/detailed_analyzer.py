"""
Core detailed analysis — fills the Bid Analyser reporting template.

Second stage to ``analyzer.analyze_tender``. That call answers one question with
one number: should Onepoint bid at all. This one takes a tender that already
cleared that gate and completes the reporting template
(``DetailedAnalyzer/template.py``) — five sections, forty-odd rows, ending in a
Likelihood of Winning percentage and a recommendation.

Division of labour, and the reason for it:

  * The 12 deterministic fields (submission deadline, client name, reference,
    location, time remaining, urgency…) are filled from the tracker row and the
    clock, never by the model. A hallucinated submission deadline is the most
    expensive error this tool could make, and there is no reason to risk it on
    data already in hand.
  * The 29 derived fields are asked of the model as one JSON object, so a partial
    reply fails loudly on parse rather than half-filling a brief a human will
    read as complete.
  * Section 3's rows are generated per tender: the model names the capability
    dimensions that matter for THIS tender and rates each against the corpus.

Public API:
    analyse_tender_detail(tender_data, run_date=None) -> TenderBrief
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from google.genai import types

from .config import (
    DETAIL_MODEL,
    DETAIL_TEMPERATURE,
    DETAIL_MAX_TOKENS,
    DETAIL_THINKING_BUDGET,
    DETAIL_MAX_RETRIES,
    API_THROTTLE_SECONDS,
    UK_TIMEZONE,
)
from .gemini_client import get_client
from .onepoint_context import load_onepoint_context
from .sources import load_corpus
from . import template as tpl

logger = logging.getLogger(__name__)


@dataclass
class TenderBrief:
    """A completed reporting template for one tender.

    ``fields`` is keyed by the template's own verbatim row labels, holding both
    the deterministic and the derived answers, so the renderer can walk the
    template in order and never has to guess where a value came from.
    """
    fields: dict                      # {template row label: detail text}
    fit_dimensions: list              # Section 3: [{"dimension","assessment","rating"}]
    likelihood_pct: float             # 0-100
    likelihood_band: str              # VERY HIGH / HIGH / MEDIUM / LOW
    recommendation: str
    analysis_date: str
    raw: dict = field(default_factory=dict)
    analysis_failed: bool = False

    @property
    def likelihood_summary(self) -> str:
        """One cell for the tracker: '82% (HIGH)'."""
        if self.analysis_failed:
            return "not scored — analysis failed"
        return f"{self.likelihood_pct:.0f}% ({self.likelihood_band})"

    @property
    def qualification_family(self) -> str:
        """Bid / TBD / NoBid implied by the likelihood band."""
        return tpl.LIKELIHOOD_QUALIFICATION.get(self.likelihood_band, "TBD")


_SYSTEM_PROMPT = (
    "You are a Bid Manager for Onepoint completing an internal bid qualification "
    "brief for a tender that has already passed initial qualification. The brief "
    "decides whether the bid team spends weeks of effort, so be specific and "
    "honest: an overstated fit costs the team more than a missed opportunity. "
    "Ground every claim about Onepoint's capability, experience or accreditation "
    "strictly in the documented evidence provided — never on assumptions beyond "
    "it. Where the tender or the evidence is silent on something material, say so "
    "plainly instead of filling the gap."
)

# Row fields offered to the prompt as context beyond title and description.
# Deliberately excludes the fields template.py fills deterministically — the
# model does not need to see a value it is not being asked to produce.
CONTEXT_FIELDS = (
    "Buyer Name",
    "Published On",
    "Clarification Due Date",
    "Tender Due Date",
    "Procurement Stage",
    "Total Contract Value",
    "Contract Duration",
    "Annual Contract Value",
    "CPV Code",
    "CPV Description",
    "Country",
    "Locality",
    "SC_Flag",
    "SME_Flag",
    "PME_Flag",
)


def _format_tender_facts(tender_data: dict) -> str:
    """Render the CONTEXT_FIELDS present on this row as a label: value list.

    Blank fields are omitted rather than sent as empty labels — a wall of
    "Total Contract Value: " lines invites the model to comment on data that was
    never there.
    """
    lines = []
    for f in CONTEXT_FIELDS:
        value = (tender_data.get(f, "") or "").strip()
        if value:
            lines.append(f"{f}: {value}")
    return "\n".join(lines) if lines else "(No further structured fields on this row.)"


def _timeline_block(deterministic: dict) -> str:
    """The computed timeline, stated to the model in words it cannot misread.

    Without this the model sees a due date and no notion of today, so it cannot
    tell a live tender from a closed one — the first run produced a brief reading
    "Deadline passed 22 days ago" beside "Proceed with bid", because the countdown
    is computed after the call and was never shown to it. The template calls
    Section 1 a critical gate; this is what makes it one.
    """
    lines = [
        f"Today's date: {deterministic.get('Current Date', 'unknown')}",
        f"Time remaining: {deterministic.get('Time Remaining', 'unknown')}",
        f"Urgency: {deterministic.get('Urgency Status', 'unknown')}",
    ]
    expired = "deadline has passed" in deterministic.get("Urgency Status", "").lower()
    if expired:
        lines.append(
            "THIS TENDER'S SUBMISSION DEADLINE HAS ALREADY PASSED. It cannot be "
            "bid. Say so plainly in the recommendation, and set the likelihood of "
            "winning to 0 — a closed tender cannot be won, however good the fit "
            "would have been. Still complete the rest of the brief: it is useful "
            "as a record of what was missed and of Onepoint's fit for work of "
            "this kind."
        )
    return "\n".join(lines)


def _build_prompt(title: str, description: str, facts: str, context: str,
                  corpus: str = "", timeline: str = "") -> str:
    """Assemble the prompt asking for every derived field in the template."""
    context_block = context if context else "(No Onepoint capability context provided.)"

    corpus_block = ""
    if corpus:
        corpus_block = f"""
Onepoint documented evidence (ingested from Onepoint's own source records —
capability matrix, supplier readiness questionnaire, past performance). This is
the detailed record behind the capability context above; cite from it when it
evidences a requirement. Two rules about its gaps, which are real and must not be
papered over: a value shown as "(not provided)" means the source was blank — do
NOT infer a figure for it — and a value marked "(unconfirmed)" was flagged
uncertain by its author, so it cannot be presented to a buyer as established
fact. Where a field is withheld as a contact detail, that is a redaction, not a
gap in Onepoint's evidence:
---
{corpus}
---
"""

    # The derived field labels are emitted from template.py rather than retyped,
    # so the prompt cannot drift out of step with the template it fills.
    derived = tpl.derived_fields()
    field_list = "\n".join(f'  "{label}": "<your answer>",' for label in derived)

    return f"""Onepoint capability context (use ONLY this to judge capability):
---
{context_block}
---
{corpus_block}
Tender under review:
Title: {title}
Description: {description}

Tender facts:
{facts}

Timeline (computed — authoritative, use this rather than inferring dates):
{timeline}

Complete Onepoint's bid qualification brief for this tender.

Section 3 of the brief is a fit assessment against the capability dimensions that
matter for THIS tender specifically — for a weather-data tender those might be
"Data Ingestion & Integration", "Python (Scientific processing)",
"Weather/Geospatial Data"; for another tender they would be entirely different.
Name between {tpl.SECTION_3_MIN_DIMENSIONS} and {tpl.SECTION_3_MAX_DIMENSIONS}
dimensions drawn from the tender's own requirements, and for each give a short
assessment plus a rating of STRONG, PARTIAL, WEAK or NONE based only on the
documented evidence.

For "Likelihood of Winning" give an integer percentage 0-100. Do not name the
band — it is derived from your percentage. Calibrate honestly: a tender Onepoint
can evidence against most requirements and has comparable past performance for
sits high; one where key requirements are unevidenced sits low, however
attractive the work looks.

Respond with ONLY a JSON object, no markdown fence, no preamble, in exactly this
shape. Every key must be present; use "Not stated in the tender" or "No
documented evidence" rather than omitting one:
{{
{field_list}
  "Likelihood of Winning": <integer 0-100>,
  "fit_dimensions": [
    {{"dimension": "<name>", "assessment": "<1-2 sentences>", "rating": "STRONG|PARTIAL|WEAK|NONE"}}
  ]
}}"""


def _deterministic_fields(tender_data: dict, run_dt: datetime) -> dict:
    """Fill the template rows that come from the tracker row or the clock.

    Nothing here is a judgement, so nothing here goes near the model. Missing
    source values render as an explicit marker rather than a blank cell: a brief
    with an empty Submission Deadline reads as "no deadline", which is a very
    different claim from "the tracker does not hold one".
    """
    out = {}
    deadline_raw = (tender_data.get("Tender Due Date", "") or "").strip()

    for label, kind, src in tpl.deterministic_fields():
        if kind == tpl.SHEET:
            value = (tender_data.get(src, "") or "").strip()
            # Location reads better as Locality + Country than either alone.
            if label == "Location":
                parts = [(tender_data.get(f, "") or "").strip()
                         for f in ("Locality", "Country")]
                value = ", ".join(p for p in parts if p)
            # Fall back to OCID when the tracker has no ID for the row.
            if label == "Opportunity Reference" and not value:
                value = (tender_data.get("OCID", "") or "").strip()
            # Budget: prefer total, fall back to annual, and say which.
            if label == "Budget (Max/Indicative)":
                total = (tender_data.get("Total Contract Value", "") or "").strip()
                annual = (tender_data.get("Annual Contract Value", "") or "").strip()
                if total and total not in ("0", "£0", "0.00"):
                    value = f"{total} (total contract value)"
                elif annual and annual not in ("0", "£0", "0.00"):
                    value = f"{annual} (annual contract value)"
                else:
                    value = ""
            out[label] = value or "Not recorded in the tracker"

        elif kind == tpl.COMPUTED:
            if src == "run_date":
                out[label] = run_dt.strftime("%d/%m/%Y")
            elif src == "time_remaining":
                out[label] = _time_remaining(deadline_raw, run_dt)
            elif src == "urgency":
                out[label] = _urgency(deadline_raw, run_dt)

    return out


def _parse_deadline(raw: str, run_dt: datetime):
    """Best-effort parse of a tracker deadline. Returns a date or None.

    The tracker's dates are entered by several upstream processes and by hand, so
    the format varies. Returning None (rather than guessing) is what keeps a
    misread date from becoming a confident countdown in the brief.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    # Trim a time or timezone suffix; only the date matters for a countdown.
    candidate = re.split(r"[T ]", raw)[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    logger.warning(f"Could not parse deadline {raw!r}; countdown omitted")
    return None


def _time_remaining(deadline_raw: str, run_dt: datetime) -> str:
    deadline = _parse_deadline(deadline_raw, run_dt)
    if deadline is None:
        return "Unknown — no parseable submission deadline in the tracker"
    days = (deadline - run_dt.date()).days
    if days < 0:
        return f"Deadline passed {abs(days)} day(s) ago ({deadline:%d/%m/%Y})"
    if days == 0:
        return f"Closes today ({deadline:%d/%m/%Y})"
    return f"{days} calendar day(s) ({deadline:%d/%m/%Y})"


# Urgency thresholds in calendar days. A rule, not a judgement — so it lives here
# where it can be read and changed, not in a prompt where it drifts per call.
URGENCY_BANDS = (
    (0,  "EXPIRED — deadline has passed"),
    (3,  "CRITICAL — 3 days or fewer"),
    (7,  "URGENT — within a week"),
    (14, "TIGHT — within two weeks"),
    (30, "COMFORTABLE — within a month"),
)


def _urgency(deadline_raw: str, run_dt: datetime) -> str:
    deadline = _parse_deadline(deadline_raw, run_dt)
    if deadline is None:
        return "Unknown — no parseable submission deadline in the tracker"
    days = (deadline - run_dt.date()).days
    if days < 0:
        return URGENCY_BANDS[0][1]
    for limit, label in URGENCY_BANDS[1:]:
        if days <= limit:
            return label
    return "AMPLE — more than a month"


def analyse_tender_detail(tender_data: dict, run_date: datetime = None) -> TenderBrief:
    """Complete the reporting template for one tender row.

    ``tender_data`` is the whole sheet row as {column: value} (Tender.data). The
    caller always gets a deterministic result: an empty row, or an API failure
    after every retry, returns a brief flagged ``analysis_failed`` rather than
    raising, so one bad row cannot take down a run — main.py counts it as an
    error, and a failed brief is never written as though it were an assessment.
    """
    if run_date is None:
        run_date = datetime.now(UK_TIMEZONE)
    date_str = run_date.strftime("%Y-%m-%d")

    title = (tender_data.get("Name", "") or "").strip()
    description = (tender_data.get("Tender Description", "") or "").strip()

    deterministic = _deterministic_fields(tender_data, run_date)

    if not title and not description:
        return TenderBrief(
            fields=deterministic, fit_dimensions=[], likelihood_pct=0.0,
            likelihood_band="LOW",
            recommendation="No tender title or description available to analyse.",
            analysis_date=date_str, analysis_failed=True,
        )

    context = load_onepoint_context()
    corpus = load_corpus()
    prompt = _build_prompt(title, description, _format_tender_facts(tender_data),
                           context, corpus, _timeline_block(deterministic))

    last_error = None
    for attempt in range(1, DETAIL_MAX_RETRIES + 1):
        try:
            response = get_client().models.generate_content(
                model=DETAIL_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM_PROMPT,
                    temperature=DETAIL_TEMPERATURE,
                    max_output_tokens=DETAIL_MAX_TOKENS,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=DETAIL_THINKING_BUDGET
                    ),
                ),
            )

            candidate = response.candidates[0] if response.candidates else None
            finish_reason = getattr(candidate, "finish_reason", None)
            try:
                raw = (response.text or "").strip()
            except Exception:
                raw = ""

            # MAX_TOKENS here almost always means DETAIL_MAX_TOKENS is too low for
            # a brief this size — raise it rather than trimming the template.
            finish_name = getattr(finish_reason, "name", None)
            if (finish_name not in ("STOP", None)) or not raw:
                raise ValueError(
                    f"incomplete response from model "
                    f"(finish_reason={finish_name!r}, {len(raw)} chars)"
                )

            result = _parse_response(raw)
            return _to_brief(result, deterministic, date_str)

        except Exception as e:
            last_error = e
            logger.warning(
                f"Detailed analysis attempt {attempt}/{DETAIL_MAX_RETRIES} failed "
                f"for title='{title[:60]}': {e}"
            )
            if attempt < DETAIL_MAX_RETRIES:
                time.sleep(API_THROTTLE_SECONDS)

    logger.error(
        f"Detailed analysis failed for title='{title[:60]}' after "
        f"{DETAIL_MAX_RETRIES} attempts: {last_error}"
    )
    time.sleep(API_THROTTLE_SECONDS)
    return TenderBrief(
        fields=deterministic, fit_dimensions=[], likelihood_pct=0.0,
        likelihood_band="LOW",
        recommendation=(
            f"Detailed analysis could not be completed after {DETAIL_MAX_RETRIES} "
            f"attempts ({last_error}). No assessment was produced — this is not a "
            f"judgement on the opportunity."
        ),
        analysis_date=date_str, analysis_failed=True,
    )


def _to_brief(result: dict, deterministic: dict, date_str: str) -> TenderBrief:
    """Assemble a TenderBrief from the model's reply plus the filled-in facts.

    Every derived label the template expects is accounted for: a key the model
    omitted becomes an explicit "not addressed" rather than a silently absent
    row, because a brief with a quietly missing row reads as complete.
    """
    fields = dict(deterministic)
    missing = []
    for label in tpl.derived_fields():
        value = result.get(label)
        if value is None or not str(value).strip():
            missing.append(label)
            fields[label] = "Not addressed by the analysis."
        else:
            fields[label] = str(value).strip()

    if missing:
        logger.warning(
            f"{len(missing)} template field(s) missing from the model reply: "
            f"{missing[:4]}{'…' if len(missing) > 4 else ''}"
        )

    try:
        pct = float(result.get("Likelihood of Winning", 0))
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    band = tpl.band_for(pct)

    # The band is derived here, never taken from the model — the label has to
    # follow the number, or the brief contradicts itself.
    fields["Likelihood of Winning"] = f"{pct:.0f}% — {band}"

    dims = []
    for d in (result.get("fit_dimensions") or []):
        if not isinstance(d, dict):
            continue
        name = str(d.get("dimension", "")).strip()
        if not name:
            continue
        dims.append({
            "dimension": name,
            "assessment": str(d.get("assessment", "")).strip(),
            "rating": str(d.get("rating", "")).strip().upper() or "NONE",
        })

    if len(dims) < tpl.SECTION_3_MIN_DIMENSIONS:
        logger.warning(
            f"Section 3 has only {len(dims)} fit dimension(s); the template "
            f"expects at least {tpl.SECTION_3_MIN_DIMENSIONS}"
        )

    recommendation = fields.get("Recommendation", "").strip()
    logger.info(
        f"Brief complete: likelihood {pct:.0f}% ({band}), "
        f"{len(dims)} fit dimension(s), {len(tpl.derived_fields()) - len(missing)}"
        f"/{len(tpl.derived_fields())} fields answered"
    )
    return TenderBrief(
        fields=fields, fit_dimensions=dims, likelihood_pct=pct,
        likelihood_band=band, recommendation=recommendation,
        analysis_date=date_str, raw=result,
    )


def _parse_response(raw: str) -> dict:
    """Parse the model's JSON reply, tolerating markdown code-fence wrapping."""
    text = raw
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)
