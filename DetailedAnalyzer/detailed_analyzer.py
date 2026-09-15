"""
Core detailed analysis — fills the Bid Analyser reporting template.

Second stage to ``analyzer.analyze_tender``. That call answers one question with
one number: should Onepoint bid at all. This one takes a tender that already
cleared that gate and completes the reporting template, ending in a Likelihood of
Winning percentage and a recommendation.

**The template drives this module, not the other way round.** Its structure is
read from the published document at run time (``template_reader``), so the
questions asked of the model are whatever the sheet currently asks — including
its YELLOW instruction cells, whose prose is handed to the model verbatim as the
instruction for that row. Add a section to the template and the next run fills
it; reword a yellow cell and the next run obeys the new wording. Nothing here
has a list of questions in it.

Division of labour, and the reason for it:

  * Rows the tracker or the clock already answers are filled in code, never by
    the model — see ``template.DETERMINISTIC``. A hallucinated submission
    deadline is the most expensive error this tool could make, and there is no
    reason to risk it on data already in hand.
  * Everything else is asked of the model as one JSON object keyed by ROW
    NUMBER, so a partial reply fails loudly on parse rather than half-filling a
    brief a human will read as complete. Row numbers also mean the answer's
    destination is exact: the report is a copy of the template that was read, so
    row N here is row N there.
  * Milestone dates the tracker does not hold are asked under a stricter
    instruction than the prose rows: a date the documents state, or the words
    "Not stated in the tender documents", never a plausible guess.
  * A table whose instruction says to EXPAND it is generated rather than filled
    — the template ships "Deliverable 1" and "Deliverable 2" as placeholders,
    and a real tender has as many deliverables as it has.

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
from . import template_reader as tr

logger = logging.getLogger(__name__)


@dataclass
class TenderBrief:
    """A completed reporting template for one tender.

    Keyed by ROW NUMBER rather than by label, because the report is a copy of the
    very template these numbers were read from — so there is nothing to match and
    nothing to mis-match. ``values`` is column B, ``more`` column C.

    ``more`` is deliberately sparse: only rows whose table header asks a second
    question appear in it. ``generated_rows`` carries the tables the template
    asked to have expanded, keyed by their header row.
    """
    values: dict = field(default_factory=dict)        # {row number: column B}
    more: dict = field(default_factory=dict)          # {row number: column C}
    generated_rows: dict = field(default_factory=dict)  # {header row: [[A,B,C]…]}
    likelihood_pct: float = 0.0
    likelihood_band: str = "LOW"
    recommendation: str = ""
    analysis_date: str = ""
    raw: dict = field(default_factory=dict)
    analysis_failed: bool = False
    # The document pack this brief was built from. Carried on the result so the
    # run log, the email and the report can all state the evidence base — an
    # assessment whose sources are invisible cannot be checked.
    documents: TenderDocuments = field(default_factory=TenderDocuments)
    # The template this brief was built against, so the writer and the renderer
    # walk exactly the document that was read rather than re-reading it.
    template: object = None

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

    @property
    def answered(self) -> int:
        return len(self.values)


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
# Deliberately excludes the fields filled deterministically — the model does not
# need to see a value it is not being asked to produce.
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

# How many rows a generated table may grow to. A bound rather than a target: the
# template ships two placeholder rows, and a tender with twenty deliverables
# would otherwise push the rest of the brief down the sheet indefinitely.
MAX_GENERATED_ROWS = 12

# One date format for the whole brief (requested 2026-09-15). It is enforced in
# BOTH directions, which is the only way it holds: the prompt asks the model for
# it, and every date this code fills in — the tracker's own dates, today's date,
# the countdown — is rendered through it too. Stating it only in the prompt would
# have left "2026-09-18" from the tracker sitting beside "18-Sep-2026" from the
# model in the same table.
#
# Time is included ONLY where a time is genuinely known. The tracker holds dates
# without times, so inventing "12:00 AM" for them would be fabricating precision
# the source never had — the same rule the rest of this module follows about
# never filling a gap with something plausible.
# What a code-filled row says when the tracker column is empty. Named because
# the precedence logic has to recognise it: a PACK_FIRST row whose tracker value
# is only this placeholder has no fallback worth having, and saying "not recorded
# in the tracker" would pin the gap on the tracker when the documents were silent
# too.
NOT_IN_TRACKER = "Not recorded in the tracker"

REPORT_DATE_FORMAT = "%d-%b-%Y"
REPORT_DATETIME_FORMAT = "%d-%b-%Y %I:%M %p"
REPORT_DATE_EXAMPLE = "DD-MMM-YYYY (e.g. 18-Sep-2026)"
REPORT_DATETIME_EXAMPLE = "DD-MMM-YYYY HH:MM AM/PM (e.g. 18-Sep-2026 12:00 PM)"

# Formats a tracker or model date may arrive in. Ordered longest-first so a value
# carrying a time is not truncated to its date by an earlier date-only match.
# "%d-%b-%Y" is in the list because it is what this module now asks for — without
# it, a correctly-formatted reply would fail to parse and its countdown would
# silently vanish from the report's third column.
_DATE_INPUT_FORMATS = (
    ("%Y-%m-%d %H:%M", True), ("%Y-%m-%dT%H:%M", True),
    ("%d/%m/%Y %H:%M", True), ("%d-%b-%Y %I:%M %p", True),
    ("%d-%b-%Y %H:%M", True),
    ("%Y-%m-%d", False), ("%d/%m/%Y", False), ("%d-%m-%Y", False),
    ("%d/%m/%y", False), ("%m/%d/%Y", False),
    ("%d-%b-%Y", False), ("%d %b %Y", False), ("%d-%B-%Y", False),
    ("%d %B %Y", False),
)


def _parse_datetime(raw: str):
    """Parse a date that may or may not carry a time. Returns (datetime, has_time)."""
    text = (raw or "").strip().replace("T", " ")
    if not text:
        return None, False
    # Trim a trailing timezone or seconds fragment the formats below do not take.
    for fmt, has_time in _DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(text, fmt), has_time
        except ValueError:
            continue
    return None, False


# Splits an inline numbered list onto separate lines. The prompt asks for this
# and the model does not reliably comply — measured 2026-09-15, where the
# clarification questions came back as "1. Can you provide…? 2. Are there any…"
# on one line — so it is enforced here instead of hoped for.
#
# A split point is whitespace followed by "<n>. " — the space after the dot is
# what keeps version numbers and decimals ("Umbraco 13.2", "£1.5m") out of it.
# The result is used only when the numbers run 1, 2, 3… in order, so prose that
# merely mentions "… by 2. " is left alone.
_NUMBERED_ITEM = re.compile(r"(?<=\s)(?=\d{1,2}\.\s)")


def _normalise_numbering(text: str) -> str:
    """Put each item of an inline numbered list on its own line."""
    if not text or "\n" in text:
        return text                      # already laid out; leave it as the model set it
    parts = _NUMBERED_ITEM.split(text.strip())
    if len(parts) < 2:
        return text

    numbers = []
    for part in parts:
        match = re.match(r"^(\d{1,2})\.\s", part)
        numbers.append(int(match.group(1)) if match else None)

    # The first part may be a lead-in sentence before "1."; everything after it
    # must be the numbered run itself.
    body = numbers[1:] if numbers[0] is None else numbers
    if not body or None in body or body != list(range(body[0], body[0] + len(body))):
        return text
    if body[0] != 1:
        return text

    return "\n".join(p.strip() for p in parts if p.strip())


def _format_date_value(raw: str) -> str:
    """Render a date in the brief's format, or "" when it is not a date.

    Returning "" rather than the input is deliberate: the caller keeps whatever
    it already had, so a range ("18/09/2026 to 02/10/2026") or a phrase ("Not
    stated in the tender documents") passes through untouched instead of being
    mangled into something that looks like a date and is not.
    """
    when, has_time = _parse_datetime(raw)
    if when is None:
        return ""
    return when.strftime(REPORT_DATETIME_FORMAT if has_time else REPORT_DATE_FORMAT)


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


def _timeline_block(computed: dict) -> str:
    """The computed timeline, stated to the model in words it cannot misread.

    Without this the model sees a due date and no notion of today, so it cannot
    tell a live tender from a closed one — the first run produced a brief reading
    "Deadline passed 22 days ago" beside "Proceed with bid", because the countdown
    is computed after the call and was never shown to it. The template calls the
    timeline gate critical; this is what makes it one.
    """
    lines = [
        f"Today's date: {computed.get('current date', 'unknown')}",
        f"Time remaining: {computed.get('time remaining', 'unknown')}",
        f"Urgency: {computed.get('urgency status', 'unknown')}",
    ]
    if "deadline has passed" in computed.get("urgency status", "").lower():
        lines.append(
            "THIS TENDER'S SUBMISSION DEADLINE HAS ALREADY PASSED. It cannot be "
            "bid. Say so plainly in the recommendation, and set the likelihood of "
            "winning to 0 — a closed tender cannot be won, however good the fit "
            "would have been. Still complete the rest of the brief: it is useful "
            "as a record of what was missed and of Onepoint's fit for work of "
            "this kind."
        )
    return "\n".join(lines)


# --- what to ask -------------------------------------------------------------

def _plan(model, filled_rows: set) -> tuple:
    """Work out what the model must answer. Returns (asks, generated_tables).

    ``asks`` is a list of (row, table) for every row still needing an answer;
    ``generated_tables`` are the tables whose instruction said to expand them,
    whose rows are produced rather than filled.
    """
    generated = [t for t in model.tables if t.generated]
    generated_row_numbers = {r.number for t in generated for r in t.rows}

    asks = []
    for row in model.questions:
        if row.number in filled_rows or row.number in generated_row_numbers:
            continue
        # The likelihood rows are written from the single percentage the model
        # gives, not asked for individually — the template currently asks for the
        # score twice, and two separately-worded answers could disagree.
        if tpl.is_likelihood_row(row.label):
            continue
        asks.append((row, model.table_of(row.number)))
    return asks, generated


def _questions_block(asks: list, generated: list, fallbacks: dict = None) -> str:
    """Render the template's own questions, grouped the way the sheet groups them.

    Walked in row order, generated tables included in their proper place — the
    sheet's sequence is how a reader understands the brief, and a question asked
    out of order invites an answer written for the wrong context.
    """
    fallbacks = fallbacks or {}
    entries = [(row.number, "ask", (row, table)) for row, table in asks]
    entries += [(t.header.number, "table", t) for t in generated]
    entries.sort(key=lambda e: e[0])

    out, section, table_seen = [], None, set()

    for _, kind, payload in entries:
        if kind == "table":
            table = payload
            if table.header.section != section:
                section = table.header.section
                out.append(f"\n### {section}")
            cols = table.columns
            out.append(
                f"t{table.header.number} -> a LIST you generate, "
                f"{len(table.rows)}-{MAX_GENERATED_ROWS} items, each "
                + '{"name": …, "detail": …'
                + (', "more": …' if table.has_more_column else "") + "}"
            )
            out.append(
                f"  name = {cols[0]!r}, detail = {cols[1]!r}"
                + (f", more = {cols[2]!r}" if len(cols) >= 3 else "")
            )
            if table.preamble:
                out.append(f"Instruction for this table:\n{table.preamble}")
            continue

        row, table = payload
        if row.section != section:
            section = row.section
            out.append(f"\n### {section}")

        if table is not None and table.header.number not in table_seen:
            table_seen.add(table.header.number)
            cols = table.columns
            out.append(
                f"\nTable — columns: B = {cols[1]!r}"
                + (f", C = {cols[2]!r}" if len(cols) >= 3 else "")
            )
            if table.preamble:
                out.append(f"Instruction for this table:\n{table.preamble}")

        key = f"r{row.number}"
        # Checked before the table branch: these rows sit inside the milestone
        # table, whose third column this code computes. Asking for the pair would
        # invite a countdown the model is in no position to work out.
        if tpl.is_derived_date(row.label):
            out.append(
                f"{key} = {row.first_line!r}  -> a date as {REPORT_DATE_EXAMPLE}, "
                f'or exactly "Not stated in the tender documents"'
            )
        elif table is not None and table.has_more_column:
            out.append(f'{key} = {row.first_line!r}  -> {{"detail": …, "more": …}}')
        elif row.role == tr.ROLE_INSTRUCTION:
            # The yellow cell's own words, verbatim. This is the instruction the
            # template author wrote for whoever fills the brief, and it is what
            # the model is held to.
            out.append(f"{key} ->\n{row.label}")
        else:
            out.append(f"{key} = {row.first_line!r}")

        # Where the tracker has an answer the pack outranks, state it as the
        # fallback rather than as the answer — so a silent pack still produces
        # a filled row, and a pack that speaks is never overruled by a scrape.
        if row.number in fallbacks:
            out.append(
                f"      (answer from the tender documents. Only if they are "
                f"silent, use the tracker's value: {fallbacks[row.number]!r})"
            )

    return "\n".join(out)


def _build_prompt(model, asks, generated, title, description, facts, context,
                  corpus="", timeline="", pack="", pack_absent_note="",
                  fallbacks=None) -> str:
    """Assemble the prompt asking for every row the template still needs."""
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
insurance and SLA terms — rather than describing them in general terms. This
holds for dates as well: where the pack states a deadline, the pack's date is the
answer. The only thing it cannot tell you is today's date and the resulting
countdown, which are given below. One limit: a document shown as truncated is
incomplete, so do not read the absence of something in it as evidence that the
pack is silent on that point:
---
{pack}
---
"""
    elif pack_absent_note:
        pack_block = f"\n{pack_absent_note}\n"

    # In row order, and with the generated tables shown as the arrays they are.
    # Rendered as a bare "t88": … alongside 74 scalar keys, the one table key was
    # simply omitted from the reply (measured 2026-09-15, leaving Section 7 empty)
    # — the shape has to say it is a list, or it reads as one more prose answer.
    shapes = [(row.number, f'  "r{row.number}": …,') for row, _ in asks]
    shapes += [
        (t.header.number,
         f'  "t{t.header.number}": [{{"name": …, "detail": …'
         + (', "more": …' if t.has_more_column else '')
         + '}, …],   <-- REQUIRED: a list, never omitted')
        for t in generated
    ]
    keys = [line for _, line in sorted(shapes, key=lambda x: x[0])]

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

Today, and the countdown (computed from the clock — the tender documents cannot
state these, so take them from here and nowhere else). The deadline the countdown
is measured from comes from Onepoint's tracker; if the tender documents state a
different submission deadline, ANSWER WITH THE DOCUMENTS' DATE — the countdown is
recomputed from it afterwards — and say in the timeline verdict that the two
disagree:
{timeline}

You are completing Onepoint's bid qualification brief. Below is every question the
brief currently asks, taken from the reporting template itself and grouped by its
sections. Each is identified by a key like "r42".

Where a question is written as an instruction, that instruction is the template
author's own wording — follow it exactly, including any sub-headings or bullet
structure it asks for. "<in the Adjacent Column>" simply means the answer belongs
in the cell beside it, which is where it will be written; it is not part of the
question.

Rules that apply throughout:
- SOURCE PRECEDENCE for anything about the client, the contract or the
  procurement. The tender pack comes FIRST: it is the buyer's own published
  documents and the thing a bid is actually evaluated against. Onepoint's
  tracker comes second, and only where the pack is silent — it is a scraped
  abstract of the notice, so where the two disagree, the pack is right and the
  tracker is out of date. Superseded versions have already been removed from the
  pack above, so every document you can see is current; if two of them still
  disagree, prefer the later-issued one and say that they differ.
  Where a question below names the tracker's value, that value is the FALLBACK
  for a silent pack, never an answer to prefer over the documents.
- Judge capability ONLY from the documented evidence above. Where there is no
  evidence, say "No documented evidence" rather than softening it.
- Where the tender is silent, say "Not stated in the tender documents". For the
  date questions this matters most: a plausible-looking date is worse than an
  admission, because the bid team will plan against whatever this brief says.
- Answers are cells in a spreadsheet. Keep them self-contained and readable.
- Do not restate the question in the answer.
- Where a question takes two parts, "detail" fills the first column and "more"
  the second, named above for that table. Leave "more" as an empty string where
  the second column genuinely adds nothing to that row.
- DATES AND TIMES follow one format throughout this brief: {REPORT_DATE_EXAMPLE}
  for a date, and {REPORT_DATETIME_EXAMPLE} where the documents actually state a
  time. Never invent a time for a date that does not carry one, and never fall
  back to another format — the same date must not appear two ways in one brief.
- WHERE AN ANSWER HAS MORE THAN ONE POINT, number them "1.", "2.", "3." and put
  each on its OWN LINE, separated by a newline. Do not run numbered points
  together inside a paragraph. A single-point answer needs no number.

QUESTIONS
{_questions_block(asks, generated, fallbacks)}

Also give "likelihood_pct": an integer 0-100 for Onepoint's likelihood of winning
this bid. Do not name the band — it is derived from your percentage, and written
into every row of the brief that asks for it. Calibrate honestly: a tender
Onepoint can evidence against most requirements and has comparable past
performance for sits high; one where key requirements are unevidenced sits low,
however attractive the work looks.

Respond with ONLY a JSON object, no markdown fence, no preamble. Every key below
must be present:
{{
{chr(10).join(keys)}
  "likelihood_pct": <integer 0-100>
}}"""


# --- what the code fills ------------------------------------------------------

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


def _deterministic_value(label: str, kind: str, src: str, tender_data: dict,
                         run_dt: datetime) -> str:
    """One row's answer, from the tracker row or the clock."""
    if kind == tpl.COMPUTED:
        deadline_raw = (tender_data.get("Tender Due Date", "") or "").strip()
        if src == "run_date":
            # The one date that legitimately carries a time: the run clock knows
            # it, so the brief states it in full.
            return run_dt.strftime(REPORT_DATETIME_FORMAT)
        if src == "time_remaining":
            return _time_remaining(deadline_raw, run_dt)
        if src == "urgency":
            return _urgency(deadline_raw, run_dt)
        return ""

    key = tpl._key(label)
    value = (tender_data.get(src, "") or "").strip()

    # Location reads better as Locality + Country than either alone.
    if key == tpl._key("Location"):
        parts = [(tender_data.get(f, "") or "").strip()
                 for f in ("Locality", "Country")]
        value = ", ".join(p for p in parts if p)
    # Fall back to the OCID when the tracker has no ID for the row.
    elif key == tpl._key("Opportunity Reference") and not value:
        value = (tender_data.get("OCID", "") or "").strip()
    # Budget: prefer the total, fall back to the annual figure, and say which.
    elif key == tpl._key("Budget (Max/Indicative)"):
        total = (tender_data.get("Total Contract Value", "") or "").strip()
        annual = (tender_data.get("Annual Contract Value", "") or "").strip()
        if _has_money_figure(total):
            value = f"{total} (total contract value)"
        elif _has_money_figure(annual):
            value = f"{annual} (annual contract value)"
        else:
            value = ""

    # A date column holds the tracker's storage format ("2026-09-18"); the brief
    # states every date its own way. Left alone when it will not parse, so a
    # hand-typed note in a date column survives rather than being discarded.
    if tpl.is_date_value(label):
        value = _format_date_value(value) or value

    return value or NOT_IN_TRACKER


def _fill_deterministic(model, tender_data: dict, run_dt: datetime) -> tuple:
    """Fill every row the tracker or the clock answers.

    Returns (values, more, computed_by_label, fallbacks). ``fallbacks`` holds the
    tracker's answer for rows the PACK outranks it on: those are asked of the
    model instead of filled, and the tracker's value is offered as what to fall
    back on if the documents turn out to be silent.
    """
    values, more, by_label, fallbacks = {}, {}, {}, {}

    for row in model.questions:
        found = tpl.deterministic_for(row.label)
        if not found:
            continue
        kind, src = found
        text = _deterministic_value(row.label, kind, src, tender_data, run_dt)
        by_label[tpl._key(row.label)] = text

        if tpl.is_pack_first(row.label):
            # Only a real tracker value is worth falling back to. Where the
            # tracker is empty too, the model's own "not stated" answer is the
            # honest one and is left to stand.
            if text != NOT_IN_TRACKER:
                fallbacks[row.number] = text
            continue
        values[row.number] = text

        # The milestone table's third column asks for each date to be read
        # against today. That is arithmetic, so it is done here.
        table = model.table_of(row.number)
        if table is not None and table.has_more_column and tpl.is_milestone_date(row.label):
            status = _milestone_status(text, run_dt)
            if status:
                more[row.number] = status

    return values, more, by_label, fallbacks


def _parse_deadline(raw: str, run_dt: datetime):
    """Best-effort parse of a tracker deadline. Returns a date or None.

    The tracker's dates are entered by several upstream processes and by hand, so
    the format varies. Returning None (rather than guessing) is what keeps a
    misread date from becoming a confident countdown in the brief.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    when, _ = _parse_datetime(raw)
    if when is not None:
        return when.date()
    # Retry on the date alone, for a value carrying a time in a shape the format
    # table does not list (seconds, a timezone suffix). Only the date matters for
    # a countdown, so dropping the remainder loses nothing.
    when, _ = _parse_datetime(re.split(r"[T ]", raw)[0])
    if when is not None:
        return when.date()
    logger.warning(f"Could not parse deadline {raw!r}; countdown omitted")
    return None


def _time_remaining(deadline_raw: str, run_dt: datetime) -> str:
    deadline = _parse_deadline(deadline_raw, run_dt)
    if deadline is None:
        return "Unknown — no parseable submission deadline in the tracker"
    days = (deadline - run_dt.date()).days
    shown = deadline.strftime(REPORT_DATE_FORMAT)
    if days < 0:
        return f"Deadline passed {abs(days)} day(s) ago ({shown})"
    if days == 0:
        return f"Closes today ({shown})"
    return f"{days} calendar day(s) ({shown})"


def _milestone_status(raw: str, run_dt: datetime) -> str:
    """Column C for one milestone row: where that date sits relative to today.

    Returns "" when the value is not a parseable date — which covers both a blank
    tracker cell and the model answering "Not stated in the tender documents".
    An empty string means the cell is left alone rather than filled with a
    countdown computed from nothing, and the date the reader can see in column B
    already says what is known.
    """
    # Screened for a digit before parsing, so the placeholders that legitimately
    # sit in these cells ("Not recorded in the tracker", "Not stated in the tender
    # documents") do not each log a failed-to-parse warning on every run.
    if not re.search(r"\d", raw or ""):
        return ""
    when = _parse_deadline(raw, run_dt)
    if when is None:
        return ""
    days = (when - run_dt.date()).days
    if days < 0:
        return f"Passed — {abs(days)} day(s) ago"
    if days == 0:
        return "Today"
    return f"Upcoming — in {days} day(s)"


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


# --- the run ------------------------------------------------------------------

def analyse_tender_detail(tender_data: dict, run_date: datetime = None,
                          model=None) -> TenderBrief:
    """Complete the reporting template for one tender row.

    ``tender_data`` is the whole sheet row as {column: value} (Tender.data).
    ``model`` is the template structure; read and cached for the run when the
    caller does not supply it. The caller always gets a deterministic result: an
    empty row, or an API failure after every retry, returns a brief flagged
    ``analysis_failed`` rather than raising, so one bad row cannot take down a
    run — main.py counts it as an error, and a failed brief is never written as
    though it were an assessment.
    """
    if run_date is None:
        run_date = datetime.now(UK_TIMEZONE)
    if model is None:
        model = tr.load_template_model()
    date_str = run_date.strftime("%Y-%m-%d")

    title = (tender_data.get("Name", "") or "").strip()
    description = (tender_data.get("Tender Description", "") or "").strip()

    det_values, det_more, computed, fallbacks = _fill_deterministic(
        model, tender_data, run_date)

    if not title and not description:
        return TenderBrief(
            values={**fallbacks, **det_values}, more=det_more, likelihood_pct=0.0,
            likelihood_band="LOW",
            recommendation="No tender title or description available to analyse.",
            analysis_date=date_str, analysis_failed=True, template=model,
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

    asks, generated = _plan(model, set(det_values))
    logger.info(
        f"  template asks {len(asks)} question(s) + "
        f"{len(generated)} generated table(s); {len(det_values)} row(s) filled "
        f"from the tracker"
    )

    prompt = _build_prompt(
        model, asks, generated, title, description,
        _format_tender_facts(tender_data), context, corpus,
        _timeline_block(computed), pack_docs.as_prompt_block(), pack_absent_note,
        fallbacks,
    )

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
            return _to_brief(result, model, asks, generated, det_values, det_more,
                             date_str, run_date, pack_docs, fallbacks,
                             (tender_data.get('Tender Due Date', '') or '').strip())

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
        values={**fallbacks, **det_values}, more=det_more, likelihood_pct=0.0,
        likelihood_band="LOW",
        recommendation=(
            f"Detailed analysis could not be completed after {DETAIL_MAX_RETRIES} "
            f"attempts ({last_error}). No assessment was produced — this is not a "
            f"judgement on the opportunity."
        ),
        analysis_date=date_str, analysis_failed=True, documents=pack_docs,
        template=model,
    )


# Answers that mean "the documents do not say", for the precedence fallback.
_SILENT_ANSWER = re.compile(
    r"^(not stated|not specified|not mentioned|not provided|not available|"
    r"no documented evidence|not addressed|unknown|n/?a|tbc|tbd)\b", re.I)


def _reads_as_silent(text: str) -> bool:
    """True when an answer says the documents are silent rather than answering."""
    return not text.strip() or bool(_SILENT_ANSWER.match(text.strip()))


def _to_brief(result: dict, model, asks, generated, det_values: dict,
              det_more: dict, date_str: str, run_dt: datetime,
              pack_docs: TenderDocuments = None, fallbacks: dict = None,
              tracker_deadline: str = "") -> TenderBrief:
    """Assemble a TenderBrief from the model's reply plus the filled-in facts.

    Every row the template asked about is accounted for: a key the model omitted
    becomes an explicit "not addressed" rather than a silently absent row,
    because a brief with a quietly missing row reads as complete.
    """
    values, more = dict(det_values), dict(det_more)
    fallbacks = fallbacks or {}
    missing = []

    for row, table in asks:
        answer = result.get(f"r{row.number}")
        detail, extra = "", ""

        if isinstance(answer, dict):
            detail = _normalise_numbering(str(answer.get("detail", "") or "").strip())
            extra = _normalise_numbering(str(answer.get("more", "") or "").strip())
        elif answer is not None:
            detail = _normalise_numbering(str(answer).strip())

        # A pack-first row the documents turned out to be silent on falls back to
        # the tracker, which is the second half of the precedence rule — stated
        # in the prompt and enforced here, so a model that answers "not stated"
        # still leaves the tracker's value in the brief rather than a blank.
        if row.number in fallbacks and _reads_as_silent(detail):
            detail = fallbacks[row.number]
        elif not detail:
            missing.append(row.first_line or f"row {row.number}")
            detail = "Not addressed by the analysis."

        values[row.number] = detail
        if extra:
            more[row.number] = extra

        # A milestone date the model supplied gets the same column C treatment
        # the tracker's own dates got — one rule for every row in that table.
        if table is not None and table.has_more_column and tpl.is_derived_date(row.label):
            status = _milestone_status(detail, run_dt)
            if status:
                more[row.number] = status

        # Re-render a model-supplied date in the brief's format. The prompt asks
        # for it, this makes sure of it — and leaves anything that is not a single
        # date ("18-Sep-2026 to 02-Oct-2026", "Not stated in the tender documents")
        # exactly as it came.
        if tpl.is_date_value(row.label):
            formatted = _format_date_value(values[row.number])
            if formatted:
                values[row.number] = formatted

    if missing:
        logger.warning(
            f"{len(missing)} template row(s) missing from the model reply: "
            f"{missing[:4]}{'…' if len(missing) > 4 else ''}"
        )

    # Generated tables — the ones whose instruction said to expand them.
    generated_rows = {}
    for table in generated:
        items = result.get(f"t{table.header.number}") or []
        rows = []
        for item in items[:MAX_GENERATED_ROWS]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "") or "").strip()
            if not name:
                continue
            cells = [name, _normalise_numbering(str(item.get("detail", "") or "").strip())]
            if table.has_more_column:
                cells.append(
                    _normalise_numbering(str(item.get("more", "") or "").strip())
                )
            rows.append(cells)
        if rows:
            generated_rows[table.header.number] = rows
        else:
            logger.warning(
                f"Generated table at row {table.header.number} came back empty; "
                f"its placeholder rows are left as the template has them"
            )

    try:
        pct = float(result.get("likelihood_pct", 0))
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    band = tpl.band_for(pct)

    # The band is derived here, never taken from the model — the label has to
    # follow the number, or the brief contradicts itself. The current template
    # asks for the score in two places; both get the same string, so they cannot
    # disagree.
    stated = f"{pct:.0f}% — {band}"
    for row in model.questions:
        if tpl.is_likelihood_row(row.label):
            values[row.number] = stated

    # The countdown was computed before the call, from the tracker's deadline,
    # because the model needed to know how long was left in order to answer. Now
    # that the pack has spoken, recompute it from whichever deadline actually
    # won — otherwise a brief could state the ITT's date beside a countdown
    # measured from a different one, which is worse than either alone.
    _recompute_countdown(model, values, tracker_deadline, run_dt)

    recommendation = ""
    for row in model.questions:
        if tpl._key(row.label) == "recommendation":
            recommendation = values.get(row.number, "")
            break

    pack_docs = pack_docs if pack_docs is not None else TenderDocuments()

    # The evidence base goes in the report only when the template has a row for
    # it — the sheet's structure is maintained by hand, so an unconfigured label
    # would otherwise be asked of the model as though it were a question.
    if TENDER_DOCS_MANIFEST_FIELD:
        wanted = tpl._key(TENDER_DOCS_MANIFEST_FIELD)
        for row in model.questions:
            if tpl._key(row.label) == wanted:
                lines = pack_docs.manifest_lines()
                values[row.number] = (
                    "\n".join(lines) if lines
                    else "No tender documents were available; assessed on the "
                         "tender summary alone."
                )
                break

    logger.info(
        f"Brief complete: likelihood {pct:.0f}% ({band}), "
        f"{len(values)} row(s) answered ({len(asks) - len(missing)}/{len(asks)} "
        f"from the model), {len(more)} second-column value(s), "
        f"{sum(len(v) for v in generated_rows.values())} generated row(s), "
        f"{len(pack_docs.used)} document(s) in evidence"
    )
    return TenderBrief(
        values=values, more=more, generated_rows=generated_rows,
        likelihood_pct=pct, likelihood_band=band, recommendation=recommendation,
        analysis_date=date_str, raw=result, documents=pack_docs, template=model,
    )


def _recompute_countdown(model, values: dict, tracker_deadline: str,
                         run_dt: datetime):
    """Re-derive Time Remaining and Urgency Status from the winning deadline.

    Mutates ``values``. Does nothing when the brief's submission deadline does not
    parse — a countdown measured from a phrase would be invented, and the row
    already computed from the tracker is the better of the two answers available.
    """
    stated = ""
    for row in model.questions:
        if tpl._key(row.label) == tpl._key("Submission Deadline"):
            stated = values.get(row.number, "")
            break
    if not stated:
        return

    when, _ = _parse_datetime(stated)
    if when is None:
        return

    tracker_when = _parse_deadline(tracker_deadline, run_dt)
    if tracker_when and tracker_when != when.date():
        logger.warning(
            f"Submission deadline differs between sources: tender documents say "
            f"{when.date():%d-%b-%Y}, tracker says {tracker_when:%d-%b-%Y}. The "
            f"documents win; the countdown is measured from their date."
        )

    authoritative = when.strftime("%Y-%m-%d")
    for row in model.questions:
        found = tpl.deterministic_for(row.label)
        if not found or found[0] != tpl.COMPUTED:
            continue
        if found[1] == "time_remaining":
            values[row.number] = _time_remaining(authoritative, run_dt)
        elif found[1] == "urgency":
            values[row.number] = _urgency(authoritative, run_dt)


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
