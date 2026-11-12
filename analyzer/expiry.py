"""
Expiry pass — closes Bid / TBD tenders whose Tender Due Date has passed.

Runs at the start of every analysis run, before row selection, over the rows
already read from the sheet (no extra read, no LLM call). A tender that is still
marked Bid or TBD — any family member: Bid(AI), TBD(AI), Bid(Human), TBD(Human),
bare Bid / TBD — can no longer be bid on once its submission deadline is behind
us, so it is moved to NoBid(AI) with a dated note in Bid Qualification
Reason(System) and Comments, exactly as a scored row is.

What it deliberately leaves alone:
  * a blank Tender Due Date — the scraper cannot always populate it, and a
    missing deadline is not a passed one;
  * a date it cannot parse — logged and skipped, never expired on a guess;
  * a tender due TODAY — still open until the day is over;
  * NoBid, PreQualified, ReCheck, Analysis-Complete and any other status —
    only the two families the team is tracking as live opportunities.

Public API:
    find_expired(tenders, run_dt) -> list[Expired]
    build_expiry_update(expired, run_dt) -> dict
"""
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

from .config import (
    DUE_DATE_FIELD,
    EXPIRE_FAMILIES,
    STATUS_FIELD,
    QUALIFICATION_NOBID,
    qualification_family,
)

logger = logging.getLogger(__name__)

# Formats the tracker has actually been seen to hold (ISO from the scraper, UK
# day-first from hand entry). Any time-of-day part is dropped before parsing —
# only the date matters for "has the deadline passed".
_DUE_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y")


@dataclass
class Expired:
    """One tender the expiry pass is closing."""
    row: int                # 1-based sheet row number
    tender: object          # the sheets_client.Tender it came from
    name: str
    previous_status: str    # the Bid/TBD value being replaced, as written in the sheet
    family: str             # 'Bid' or 'TBD' — what the summary counts by
    due_date: date


def parse_due_date(raw: str):
    """Best-effort parse of a Tender Due Date cell. Returns a date or None.

    Returning None rather than guessing is what keeps a misread cell from
    closing a live tender.
    """
    text = (raw or "").strip()
    if not text:
        return None
    text = re.split(r"[T ]", text, maxsplit=1)[0]
    for fmt in _DUE_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def find_expired(tenders: list, run_dt: datetime) -> list:
    """Return the Bid/TBD tenders whose due date is strictly before the run date."""
    today = run_dt.date()
    expired = []
    for tender in tenders:
        status = (tender.data.get(STATUS_FIELD, "") or "").strip()
        family = qualification_family(status)
        if family not in EXPIRE_FAMILIES:
            continue
        raw_due = (tender.data.get(DUE_DATE_FIELD, "") or "").strip()
        if not raw_due:
            continue
        due = parse_due_date(raw_due)
        if due is None:
            logger.warning(
                f"Row {tender.row}: cannot parse {DUE_DATE_FIELD} {raw_due!r}; "
                f"left as {status}"
            )
            continue
        if due >= today:
            continue
        expired.append(Expired(
            row=tender.row,
            tender=tender,
            name=(tender.title or "").strip(),
            previous_status=status,
            family=family,
            due_date=due,
        ))
    return expired


def build_expiry_update(expired: Expired, run_dt: datetime) -> dict:
    """Assemble the field->value map that closes one expired tender.

    Mirrors main._build_row_update: the dated entry is prepended to Bid
    Qualification Reason(System) (newest on top, history kept) and appended to
    Comments (oldest first). Bid Qualification Reason(Human) is never written.
    """
    now_iso = run_dt.isoformat()
    ts = run_dt.strftime("%Y-%m-%d %H:%M")
    due = expired.due_date.isoformat()
    reason = (
        f"Tender due date {due} has passed (run date {run_dt.strftime('%Y-%m-%d')}); "
        f"previously {expired.previous_status}. Set to {QUALIFICATION_NOBID} "
        f"automatically because the opportunity is closed."
    )
    entry = (
        f"[{ts}] Bid Qualification: {QUALIFICATION_NOBID} "
        f"(not scored — due date passed) | {reason}"
    )
    data = expired.tender.data

    prior_reason = data.get("Bid Qualification Reason(System)", "")
    combined_reason = f"{entry}\n{prior_reason}" if prior_reason else entry

    prior_comments = data.get("Comments", "")
    combined_comments = f"{prior_comments}\n{entry}" if prior_comments else entry

    created = data.get("Created Date", "") or now_iso

    return {
        "Bid Qualification": QUALIFICATION_NOBID,
        "Bid Qualification Reason(System)": combined_reason,
        "Bid Qualification Date": run_dt.strftime("%Y-%m-%d"),
        "Comments": combined_comments,
        "Processed Date": now_iso,
        "Last Modified Date": now_iso,
        "Created Date": created,
    }
