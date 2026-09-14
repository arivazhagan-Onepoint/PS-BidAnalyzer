"""
Report writer — one completed brief per tender, as a copy of the template.

Copies the Bid Analyser reporting template into the reports folder, renames it
after the tender, and fills it in.

Why copy rather than build a sheet from scratch: the template carries formatting,
column widths and section styling that someone deliberately set up, and it is the
artifact the bid team recognises. Reproducing that in code would drift from it the
first time anyone adjusts the original. Copying inherits it for free.

**Values are addressed by ROW NUMBER, not by matching labels.** The structure was
read from this very template (``template_reader``) and the report is a copy of
it, so row N in the brief is row N in the report. That removes the whole business
of normalising labels, prefix-matching them and refusing ambiguous ones — along
with the failure it existed to prevent, a brief one row out of alignment.

Two things the copy needs beyond filling:

  * **The template is a worked example.** It ships with a previous tender's
    answers in column B. Any row this writer does not fill would keep them, so
    the brief would show another tender's figures as if they were this one's.
    Every data row that gets no value is therefore cleared. Safe to do, because
    each run creates a fresh timestamped copy — nothing is ever re-filled.
  * **Some tables are placeholders.** A table whose yellow instruction says to
    expand it ships two example rows; a real tender has as many as it has. Rows
    are inserted before anything is written, so everything below shifts once and
    the row numbers used for writing account for it.

Structural rows — section headings, table headers, the sheet's own header — are
never written to and never cleared.
"""
import logging

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import (
    SCOPES,
    SERVICE_ACCOUNT_FILE,
    REPORTS_FOLDER_ID,
    REPORT_NAME_PATTERN,
    REPORT_RUNTIME_FORMAT,
    REPORT_NAME_MAX_TITLE,
    REPORT_NAME_NO_PORTAL,
    REPORT_NAME_NO_ID,
    REPORT_NAME_UNSAFE_CHARS,
    RENAME_REPORT_TAB,
    TEMPLATE_DETAIL_COL,
    TEMPLATE_MORE_COL,
)
from . import template_reader as tr

logger = logging.getLogger(__name__)


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

    def template_id(self) -> str:
        """The configured template's file ID (resolved and cached in one place)."""
        return tr.template_id(self.drive)

    # --- copy -----------------------------------------------------------------
    def create_report(self, tender_data: dict, run_dt) -> tuple:
        """Copy the template into the reports folder. Returns (file_id, url)."""
        name = report_name(tender_data, run_dt)
        try:
            copied = self.drive.files().copy(
                fileId=self.template_id(),
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

    def _expand_tables(self, file_id: str, tab_id: int, brief) -> list:
        """Insert room for generated table rows. Returns [(after_row, count)].

        Done before anything is written so that every row number used afterwards
        can be shifted once, consistently. Rows inherit the formatting of the row
        above so an expanded table still looks like the table it belongs to.
        """
        inserts, requests = [], []
        for header_row, rows in sorted(brief.generated_rows.items()):
            table = brief.template.table_by_header_row.get(header_row)
            if table is None:
                continue
            available = len(table.rows)
            extra = len(rows) - available
            if extra <= 0:
                continue
            last = table.rows[-1].number
            inserts.append((last, extra))
            requests.append({
                "insertDimension": {
                    "range": {
                        "sheetId": tab_id,
                        "dimension": "ROWS",
                        "startIndex": last,          # 0-based: directly after `last`
                        "endIndex": last + extra,
                    },
                    "inheritFromBefore": True,
                }
            })

        if requests:
            self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=file_id, body={"requests": requests},
            ).execute()
            logger.info(
                f"Expanded {len(requests)} table(s) by "
                f"{sum(c for _, c in inserts)} row(s)"
            )
        return inserts

    @staticmethod
    def _shifter(inserts: list):
        """Map a template row number onto its row in the expanded copy."""
        def shift(n: int) -> int:
            return n + sum(c for at, c in inserts if n > at)
        return shift

    def fill_report(self, file_id: str, brief, report_title: str = "") -> dict:
        """Write the brief into the copied report. Returns a small stats dict.

        ``report_title`` is only used when RENAME_REPORT_TAB is on. Named to avoid
        shadowing the module-level report_name() in this scope.
        """
        model = brief.template
        if model is None:
            raise ValueError(
                "brief has no template structure attached; it cannot be written "
                "without knowing which row each value belongs to"
            )

        props = self._first_tab(file_id)
        tab_id, tab_name = props["sheetId"], props["title"]

        if RENAME_REPORT_TAB and report_title:
            tab_name = self._rename_tab(file_id, tab_id, report_title)

        inserts = self._expand_tables(file_id, tab_id, brief)
        shift = self._shifter(inserts)

        data = []

        def put(row: int, col: str, value):
            data.append({"range": f"'{tab_name}'!{col}{row}", "values": [[value]]})

        written = more_written = 0
        for row_number, value in brief.values.items():
            put(shift(row_number), TEMPLATE_DETAIL_COL, value)
            written += 1
        for row_number, value in brief.more.items():
            put(shift(row_number), TEMPLATE_MORE_COL, value)
            more_written += 1

        # Generated tables: column A is produced too, since the template's own
        # "Deliverable 1" / "Deliverable 2" are placeholders for a real list.
        generated = 0
        for header_row, rows in brief.generated_rows.items():
            table = model.table_by_header_row.get(header_row)
            if table is None:
                continue
            start = shift(table.rows[0].number)
            for offset, cells in enumerate(rows):
                data.append({
                    "range": f"'{tab_name}'!A{start + offset}",
                    "values": [cells],
                })
                generated += 1
            # A table that came back shorter than its placeholders leaves empty
            # rows behind; clear them rather than leaving "Deliverable 2" in a
            # brief that only has one deliverable.
            for offset in range(len(rows), len(table.rows)):
                data.append({
                    "range": (f"'{tab_name}'!A{start + offset}:"
                              f"{TEMPLATE_MORE_COL}{start + offset}"),
                    "values": [["", "", ""]],
                })

        # Clear the worked example. Any data row this brief has no value for would
        # otherwise keep the previous tender's answer, which reads as this
        # tender's — the one failure mode worse than a gap.
        generated_rows = {r.number for t in model.tables if t.generated for r in t.rows}
        cleared = 0
        for row in model.rows:
            if row.role not in (tr.ROLE_DATA, tr.ROLE_INSTRUCTION):
                continue
            if row.number in brief.values or row.number in generated_rows:
                continue
            put(shift(row.number), TEMPLATE_DETAIL_COL, "")
            put(shift(row.number), TEMPLATE_MORE_COL, "")
            cleared += 1

        if data:
            self.sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=file_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()

        logger.info(
            f"Filled report {file_id}: {written} value(s) in column "
            f"{TEMPLATE_DETAIL_COL}, {more_written} in column {TEMPLATE_MORE_COL}, "
            f"{generated} generated row(s), {cleared} unused row(s) cleared"
        )
        return {"fields_written": written, "details_written": more_written,
                "generated_written": generated, "cleared": cleared}

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

    docs = getattr(brief, "documents", None)
    if docs is not None:
        manifest = docs.manifest_lines()
        lines.append(f"## Evidence base — {docs.folder_name or 'no tender pack'}")
        lines.append("")
        lines += ([f"- {line}" for line in manifest] or
                  ["- _No tender documents; assessed on the tender summary alone._"])
        lines += [f"- ⚠️ {w}" for w in docs.warnings]

    model = brief.template
    if model is None:
        return "\n".join(lines) + "\n"

    generated_rows = {r.number for t in model.tables if t.generated for r in t.rows}

    for row in model.rows:
        if row.role == tr.ROLE_SECTION:
            lines += ["", f"## {row.label}", ""]
            continue
        if row.role == tr.ROLE_SUB_HEADING:
            lines += ["", f"### {row.first_line}", ""]
            continue
        if row.role == tr.ROLE_TABLE_HEADER:
            table = model.table_by_header_row.get(row.number)
            if table and table.generated:
                for cells in brief.generated_rows.get(row.number, []):
                    lines.append(f"- **{cells[0]}** — {' | '.join(cells[1:])}")
            continue
        if row.number in generated_rows:
            continue

        value = brief.values.get(row.number)
        if value is None:
            continue
        extra = brief.more.get(row.number, "")
        label = row.first_line or f"row {row.number}"
        lines.append(f"- **{label}:** {value}" + (f"  _({extra})_" if extra else ""))

    return "\n".join(lines) + "\n"
