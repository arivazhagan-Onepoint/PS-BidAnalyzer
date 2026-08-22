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
    TENDER_DOCS_MANIFEST_FIELD,
)
from .gemini_client import get_client
from .onepoint_context import load_onepoint_context
from .sources import load_corpus
from .tender_docs import load_tender_documents, TenderDocuments
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
    # The document pack this brief was built from. Carried on the result so the
    # run log, the email and the report can all state the evidence base — an
    # assessment whose sources are invisible cannot be checked.
    documents: TenderDocuments = field(default_factory=TenderDocuments)

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
                  corpus: str = "", timeline: str = "", pack: str = "",
                  pack_absent_note: str = "") -> str:
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

    # The tender's own published pack. Framed as authoritative over the notice
    # summary — the tracker row is a scraped abstract, the pack is the document a
    # bid is actually evaluated against — but explicitly NOT over the computed
    # timeline, or the model starts reading dates out of the ITT's own text and the
    # bug fixed by stating the timeline separately comes straight back.
    pack_block = ""
    if pack:
        pack_block = f"""
The tender pack for THIS tender — the buyer's own published documents, read from
Onepoint's Drive. This is the authoritative statement of what is being asked for:
where it and the tender summary above disagree about requirements, scope, or
evaluation, THE PACK WINS and the summary is treated as an abstract of it. Quote
specifics from it — mandatory requirements, evaluation weightings, certifications,
insurance and SLA terms — rather than describing them in general terms. Two
limits: the computed timeline below remains authoritative for dates, and a
document shown as truncated is incomplete, so do not read the absence of something
in it as evidence that the pack is silent on that point:
---
{pack}
---
"""
    elif pack_absent_note:
        pack_block = f"\n{pack_absent_note}\n"

    # The derived field labels are emitted from template.py rather than retyped,
    # so the prompt cannot drift out of step with the template it fills.
    derived = tpl.derived_fields()
    field_list = "\n".join(f'  "{label}": "<your answer>",' for label in derived)

    return f"""Onepoint capability context (use ONLY this to judge capability):
---
{context_block}
---
{corpus_block}{pack_block}
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


def _has_money_figure(value: str) -> bool:
    """True when a money cell holds a real, non-zero amount.

    The tracker's unfilled contract-value columns are not all written "0": the CITB
    row holds "GBP 0.00", which an equality test against ("0", "£0", "0.00") let
    straight through, so the brief reported the budget as "GBP 0.00 (total contract
    value)" — a genuine zero-value contract rather than a column nobody filled in.
    The same trap MONEY_ZERO_VALUES guards in the corpus, defended the same way:
    strip the currency and separators, then look at the number.

    A placeholder with no digits at all ("TBC", "N/A") is likewise not a figure,
    matching how PLACEHOLDER_VALUES treats it during ingestion.
    """
    number = re.sub(r"[^\d.]", "", (value or "").strip())
    if not number:
        return False
    try:
        return float(number) != 0.0
    except ValueError:
        return False


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
                if _has_money_figure(total):
                    value = f"{total} (total contract value)"
                elif _has_money_figure(annual):
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

    # The tender's own pack, if one has been uploaded for it. Fetched per row —
    # unlike the corpus, it belongs to this tender rather than to Onepoint.
    pack_docs = load_tender_documents(tender_data)
    pack_absent_note = "" if pack_docs.used else (
        "No tender pack was available for this tender — no documents have been "
        "uploaded for it, so this assessment rests on the tender summary above "
        "alone. Say so where a question can only be answered from the tender "
        "documents (evaluation weightings, mandatory requirements, contractual "
        "terms) rather than inferring an answer."
    )

    prompt = _build_prompt(title, description, _format_tender_facts(tender_data),
                           context, corpus, _timeline_block(deterministic),
                           pack_docs.as_prompt_block(), pack_absent_note)

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
            return _to_brief(result, deterministic, date_str, pack_docs)

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
        analysis_date=date_str, analysis_failed=True, documents=pack_docs,
    )


def _to_brief(result: dict, deterministic: dict, date_str: str,
              pack_docs: TenderDocuments = None) -> TenderBrief:
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

    pack_docs = pack_docs if pack_docs is not None else TenderDocuments()

    # The evidence base goes in the report only when the template has a row for it
    # — the sheet's structure is maintained by hand, so an unconfigured label would
    # warn on every run about a row nobody has added.
    if TENDER_DOCS_MANIFEST_FIELD:
        lines = pack_docs.manifest_lines()
        fields[TENDER_DOCS_MANIFEST_FIELD] = (
            "\n".join(lines) if lines
            else "No tender documents were available; assessed on the tender summary alone."
        )

    recommendation = fields.get("Recommendation", "").strip()
    logger.info(
        f"Brief complete: likelihood {pct:.0f}% ({band}), "
        f"{len(dims)} fit dimension(s), {len(tpl.derived_fields()) - len(missing)}"
        f"/{len(tpl.derived_fields())} fields answered, "
        f"{len(pack_docs.used)} document(s) in evidence"
    )
    return TenderBrief(
        fields=fields, fit_dimensions=dims, likelihood_pct=pct,
        likelihood_band=band, recommendation=recommendation,
        analysis_date=date_str, raw=result, documents=pack_docs,
    )


def _parse_response(raw: str) -> dict:
    """Parse the model's JSON reply, tolerating fencing and trailing commentary.

    ``raw_decode`` takes the first complete JSON value and ignores whatever
    follows, rather than failing the whole reply the way ``json.loads`` does. The
    model does sometimes append a sentence after the closing brace — measured on
    the first pack-fed run, which failed with "Extra data: line 40 column 1" and
    only succeeded on retry. With a tender pack in the prompt a retry re-sends
    ~100k tokens, so salvaging a reply that is complete but chatty is worth more
    here than it was before.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in the reply")
    result, end = json.JSONDecoder().raw_decode(text, start)
    trailing = text[end:].strip()
    if trailing:
        logger.warning(
            f"Ignored {len(trailing)} char(s) of commentary after the JSON reply: "
            f"{trailing[:120]!r}"
        )
    return result
