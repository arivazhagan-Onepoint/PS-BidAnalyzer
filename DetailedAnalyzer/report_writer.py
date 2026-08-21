"""
Report writer — one completed brief per tender, as a copy of the template.

Copies the Bid Analyser reporting template into the reports folder, renames it
after the tender, and fills column B against the labels already in column A.

Why copy rather than build a sheet from scratch: the template carries formatting,
column widths and section styling that someone deliberately set up, and it is the
artifact the bid team recognises. Reproducing that in code would drift from it the
first time anyone adjusts the original. Copying inherits it for free.

Why the labels are read back from the copy rather than assumed: the template is
a live document. If someone inserts a row, appends a Section 6 or rewords a
question, this writer follows it — values are matched to the labels actually
present, and anything it cannot place is reported rather than written to the wrong
row. A brief silently one row out of alignment is worse than one with a gap.
"""
import logging
import re

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import (
    SCOPES,
    SERVICE_ACCOUNT_FILE,
    TEMPLATE_SPREADSHEET_ID,
    REPORTS_FOLDER_ID,
    REPORT_NAME_PATTERN,
    REPORT_RUNTIME_FORMAT,
    REPORT_NAME_MAX_TITLE,
    REPORT_NAME_NO_PORTAL,
    REPORT_NAME_NO_ID,
    REPORT_NAME_UNSAFE_CHARS,
    RENAME_REPORT_TAB,
    TEMPLATE_LABEL_COL,
    TEMPLATE_DETAIL_COL,
)
from . import template as tpl

logger = logging.getLogger(__name__)

# Section 3's rows do not exist in the template — they are generated per tender —
# so they are appended under its heading. This is the heading to find.
SECTION_3_HEADING = "3. Fit Assessment (Matrix Check)"


def _normalise(label: str) -> str:
    """Loose key for matching a value to a template row.

    Whitespace, case and trailing punctuation vary between the template and the
    labels transcribed in template.py, and a human editing the sheet will not
    preserve them. Matching on a normalised form keeps a reworded space or a
    stray colon from silently dropping a row.
    """
    return re.sub(r"[^a-z0-9]+", " ", (label or "").lower()).strip()


# A label must be at least this long before a prefix match is trusted. Short keys
# like "location" would prefix-match half a dozen unrelated rows; a long one that
# matches from the first character is the field, followed by its guidance text.
_PREFIX_MATCH_MIN_LEN = 12


def _prefix_match(key: str, by_label: dict):
    """Row whose label starts with ``key``, when there is exactly one.

    Ambiguity is refused rather than guessed: writing a value to the wrong row
    produces a brief that looks complete and states something false, which is
    worse than the gap plus the warning the caller logs.
    """
    if len(key) < _PREFIX_MATCH_MIN_LEN:
        return None
    hits = [row for label, row in by_label.items() if label.startswith(key)]
    return hits[0] if len(hits) == 1 else None


def _sanitise(value: str) -> str:
    """Make one name segment safe for a file name, on any OS."""
    text = " ".join((value or "").split())
    for ch in REPORT_NAME_UNSAFE_CHARS:
        text = text.replace(ch, " ")
    # Collapse whatever the replacements left behind.
    return " ".join(text.split()).strip(" .-")


def report_name(tender_data: dict, run_dt) -> str:
    """PortalName-TenderID-TenderTitle-Report-RunTime for one tender row.

    Module level, and needing no credentials, so a dry run can report the exact
    name a real run would create without authenticating anything.
    """
    portal = _sanitise(tender_data.get("Portal Name", "")) or REPORT_NAME_NO_PORTAL
    # OCID first: it is the OCDS global identifier (ocds-h6vhtk-…), traceable back
    # to the source record and unique across portals, whereas ID is a portal-local
    # notice number that two portals could in principle both issue. Both were 100%
    # populated and fully distinct across all 529 tracker rows when checked
    # (2026-08-21), so the ID fallback is belt-and-braces rather than expected.
    tender_id = (_sanitise(tender_data.get("OCID", ""))
                 or _sanitise(tender_data.get("ID", ""))
                 or REPORT_NAME_NO_ID)
    title = _sanitise(tender_data.get("Name", "")) or "Untitled tender"
    if len(title) > REPORT_NAME_MAX_TITLE:
        # Trim on a word boundary where there is one — a name cut mid-word reads
        # as corrupted rather than shortened.
        trimmed = title[:REPORT_NAME_MAX_TITLE]
        title = (trimmed.rsplit(" ", 1)[0] if " " in trimmed else trimmed).rstrip(" .-")
    return REPORT_NAME_PATTERN.format(
        portal=portal,
        tender_id=tender_id,
        title=title,
        runtime=run_dt.strftime(REPORT_RUNTIME_FORMAT),
    )


class ReportWriter:
    def __init__(self):
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        self.sheets = build("sheets", "v4", credentials=creds)
        self.drive = build("drive", "v3", credentials=creds)

    # --- copy -----------------------------------------------------------------
    def create_report(self, tender_data: dict, run_dt) -> tuple:
        """Copy the template into the reports folder. Returns (file_id, url)."""
        name = report_name(tender_data, run_dt)
        try:
            copied = self.drive.files().copy(
                fileId=TEMPLATE_SPREADSHEET_ID,
                body={"name": name, "parents": [REPORTS_FOLDER_ID]},
                fields="id,webViewLink",
                supportsAllDrives=True,
            ).execute()
        except HttpError as e:
            raise RuntimeError(
                f"Could not copy the reporting template into folder "
                f"{REPORTS_FOLDER_ID}: HTTP {e.resp.status} {e.reason}. Check the "
                f"service account has Editor on that folder and can read the "
                f"template."
            ) from e

        file_id = copied["id"]
        url = copied.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
        logger.info(f"Created report '{name}' ({file_id})")
        return file_id, url

    # --- fill -----------------------------------------------------------------
    def _first_tab(self, file_id: str) -> dict:
        meta = self.sheets.spreadsheets().get(
            spreadsheetId=file_id, fields="sheets.properties"
        ).execute()
        return meta["sheets"][0]["properties"]

    def _rename_tab(self, file_id: str, tab_id: int, new_title: str):
        """Rename the copied tab (off by default — see RENAME_REPORT_TAB)."""
        new_title = new_title[:100]
        self.sheets.spreadsheets().batchUpdate(
            spreadsheetId=file_id,
            body={"requests": [{
                "updateSheetProperties": {
                    "properties": {"sheetId": tab_id, "title": new_title},
                    "fields": "title",
                }
            }]},
        ).execute()
        return new_title

    def fill_report(self, file_id: str, brief, report_title: str = "") -> dict:
        """Write the brief into the copied report. Returns a small stats dict.

        ``report_title`` is only used when RENAME_REPORT_TAB is on. Named to avoid
        shadowing the module-level report_name() in this scope.
        """
        props = self._first_tab(file_id)
        tab_id, tab_name = props["sheetId"], props["title"]

        if RENAME_REPORT_TAB and report_title:
            tab_name = self._rename_tab(file_id, tab_id, report_title)

        # Read column A of the copy — the labels as they actually are, not as
        # template.py remembers them.
        labels_res = self.sheets.spreadsheets().values().get(
            spreadsheetId=file_id,
            range=f"'{tab_name}'!{TEMPLATE_LABEL_COL}:{TEMPLATE_LABEL_COL}",
        ).execute()
        col_a = [(r[0] if r else "") for r in labels_res.get("values", [])]

        by_label = {}
        for i, label in enumerate(col_a, start=1):
            key = _normalise(label)
            if key and key not in by_label:
                by_label[key] = i

        data, written, unmatched = [], 0, []
        for label, value in brief.fields.items():
            key = _normalise(label)
            row = by_label.get(key)
            if not row:
                # Fall back to a prefix match. Some template rows carry guidance
                # after the field name — "Likelihood of Winning" is followed by all
                # four band definitions on continuation lines — so an exact match
                # misses them. Without this the most important row in the brief
                # (the likelihood itself) silently stayed empty.
                row = _prefix_match(key, by_label)
            if not row:
                unmatched.append(label)
                continue
            data.append({
                "range": f"'{tab_name}'!{TEMPLATE_DETAIL_COL}{row}",
                "values": [[value]],
            })
            written += 1

        if unmatched:
            # Not fatal, but it means the template and template.py have diverged —
            # surface it rather than quietly shipping an incomplete brief.
            logger.warning(
                f"{len(unmatched)} field(s) had no matching row in the report and "
                f"were not written: {unmatched[:4]}"
                f"{'…' if len(unmatched) > 4 else ''}"
            )

        if data:
            self.sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=file_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()

        dims_written = self._write_fit_dimensions(
            file_id, tab_name, col_a, brief.fit_dimensions
        )

        logger.info(
            f"Filled report {file_id}: {written} field(s), "
            f"{dims_written} fit dimension(s)"
        )
        return {"fields_written": written, "unmatched": unmatched,
                "dimensions_written": dims_written}

    def _write_fit_dimensions(self, file_id: str, tab_name: str, col_a: list,
                              dimensions: list) -> int:
        """Insert Section 3's per-tender rows under its heading.

        The template ships Section 3 pre-filled with the Met Office tender's
        dimensions. Those belong to a different tender, so the block is rewritten:
        rows between the Section 3 heading and the next numbered section are
        cleared, then this tender's dimensions are written in their place. Rows are
        inserted when there is not enough room, so a tender with nine dimensions
        does not overwrite Section 4.
        """
        if not dimensions:
            return 0

        heading_row = None
        for i, label in enumerate(col_a, start=1):
            if _normalise(label) == _normalise(SECTION_3_HEADING):
                heading_row = i
                break
        if heading_row is None:
            logger.warning(
                f"Section 3 heading {SECTION_3_HEADING!r} not found in the report; "
                f"{len(dimensions)} fit dimension(s) not written"
            )
            return 0

        # Find where the next section starts, so the block's extent is known.
        next_section = None
        for i in range(heading_row, len(col_a)):
            label = (col_a[i] or "").strip()
            if re.match(r"^\d+\.\s", label) and i + 1 > heading_row:
                next_section = i + 1
                break
        if next_section is None:
            next_section = heading_row + 1

        slots = next_section - heading_row - 1        # blank/example rows available
        needed = len(dimensions)

        if needed > slots:
            # Make room rather than writing over Section 4.
            self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=file_id,
                body={"requests": [{
                    "insertDimension": {
                        "range": {
                            "sheetId": self._first_tab(file_id)["sheetId"],
                            "dimension": "ROWS",
                            "startIndex": heading_row,          # 0-based: after heading
                            "endIndex": heading_row + (needed - slots),
                        },
                        "inheritFromBefore": False,
                    }
                }]},
            ).execute()

        start = heading_row + 1
        rows = [
            [d["dimension"], f"{d['rating']} — {d['assessment']}"]
            for d in dimensions
        ]
        # Clear any leftover template dimensions below what we are writing.
        if slots > needed:
            self.sheets.spreadsheets().values().clear(
                spreadsheetId=file_id,
                range=f"'{tab_name}'!A{start + needed}:B{start + slots - 1}",
            ).execute()

        self.sheets.spreadsheets().values().update(
            spreadsheetId=file_id,
            range=f"'{tab_name}'!A{start}",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()
        return len(rows)

    # --- one call -------------------------------------------------------------
    def write(self, brief, tender_data: dict, run_dt) -> tuple:
        """Create and fill a report for one tender. Returns (name, file_id, url)."""
        name = report_name(tender_data, run_dt)
        file_id, url = self.create_report(tender_data, run_dt)
        self.fill_report(file_id, brief, name)
        return name, file_id, url


def render_markdown(brief, heading: str = "") -> str:
    """Render a brief as markdown — for --dry-run and for the run log.

    Walks the template in its own order so the text and the spreadsheet agree.
    """
    lines = [f"# {heading or 'Detailed analysis'}", ""]
    for section, label, kind, _src in tpl.section_rows():
        if section:
            lines += ["", f"## {section}", ""]
            if section == SECTION_3_HEADING:
                for d in brief.fit_dimensions:
                    lines.append(f"- **{d['dimension']}** — {d['rating']}: {d['assessment']}")
                if not brief.fit_dimensions:
                    lines.append("_No fit dimensions produced._")
            continue
        if not label:
            continue
        value = brief.fields.get(label, "")
        lines.append(f"- **{label}:** {value}")
    return "\n".join(lines) + "\n"
