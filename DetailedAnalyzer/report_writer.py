"""
Report writer — one completed brief per tender, as a copy of the template.

Copies the Bid Analyser reporting template into the reports folder, renames it
after the tender, and fills columns B and C against the labels already in
column A.

The template is found by NAME (project_config.json's
google_sheets.Reporting_Template) inside the Reporting_Templates folder, not by a
hardcoded file ID. The folder holds several versions side by side, so the ID a
brief is built from is a thing the bid team changes, and a config edit is the
right size of change for it. The lookup insists on exactly one match: two
same-named templates would otherwise be chosen between by Drive's listing order,
and every brief afterwards would be built from whichever it happened to return
first — a difference nobody would see in the output.

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
    REPORTING_TEMPLATE_NAME,
    REPORTING_TEMPLATES_FOLDER_ID,
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
    TEMPLATE_MORE_COL,
)
from . import template as tpl

logger = logging.getLogger(__name__)


def _normalise(label: str) -> str:
    """Loose key for matching a value to a template row.

    Whitespace, case and trailing punctuation vary between the template and the
    labels transcribed in template.py, and a human editing the sheet will not
    preserve them. Matching on a normalised form keeps a reworded space or a
    stray colon from silently dropping a row.
    """
    return re.sub(r"[^a-z0-9]+", " ", (label or "").lower()).strip()


def _first_line(label: str) -> str:
    """Normalised key for just the first line of a template row.

    Several rows in this template are a field name followed by guidance on
    continuation lines — "Summary" then "< please provide a summary of
    Route-To-Market… >", "Likelihood of Winning" then all four band definitions,
    each flag row then the bullets describing what to list. Normalising the whole
    cell buries the field name in the guidance, and the prefix match below
    refuses short keys, so "Summary" matched nothing at all. Keying on the first
    line as well finds these without loosening the prefix rule for everything.
    """
    return _normalise((label or "").replace("\r", "\n").split("\n")[0])


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
        self._template_id = None

    # --- resolve --------------------------------------------------------------
    def template_id(self) -> str:
        """The configured reporting template's file ID, looked up once per run.

        Resolved lazily rather than at import so that nothing which merely imports
        this module pays for a Drive call, and cached on the instance so a run
        analysing forty tenders still only asks once.
        """
        if self._template_id:
            return self._template_id

        query = (
            f"name='{REPORTING_TEMPLATE_NAME}' and "
            f"'{REPORTING_TEMPLATES_FOLDER_ID}' in parents and "
            f"mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
        )
        files = self.drive.files().list(
            q=query, spaces="drive", fields="files(id,name)", pageSize=10,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute().get("files", [])

        if not files:
            raise FileNotFoundError(
                f"Reporting template {REPORTING_TEMPLATE_NAME!r} was not found in "
                f"Drive folder {REPORTING_TEMPLATES_FOLDER_ID}. Check "
                f"google_sheets.Reporting_Template in project_config.json matches "
                f"the file name exactly, and that the service account can see the "
                f"folder."
            )
        if len(files) > 1:
            raise RuntimeError(
                f"{len(files)} spreadsheets in folder {REPORTING_TEMPLATES_FOLDER_ID} "
                f"are named {REPORTING_TEMPLATE_NAME!r} ({', '.join(f['id'] for f in files)}). "
                f"Rename or remove all but one — picking between them would make "
                f"every brief depend on Drive's listing order."
            )

        self._template_id = files[0]["id"]
        logger.info(
            f"Reporting template {REPORTING_TEMPLATE_NAME!r} resolved to "
            f"{self._template_id}"
        )
        return self._template_id

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

        by_label, by_first_line = {}, {}
        for i, label in enumerate(col_a, start=1):
            key = _normalise(label)
            if key and key not in by_label:
                by_label[key] = i
            head = _first_line(label)
            if head and head not in by_first_line:
                by_first_line[head] = i

        def find_row(field_label: str):
            """The report row a field belongs in, or None.

            Tried in order of how much is being assumed: the label exactly, the
            label the template.py alias says the sheet uses, the row's first line,
            then a unique prefix. Each step is looser than the last, which is why
            they are not collapsed — an exact hit should never be overridden by a
            fuzzy one.
            """
            for candidate in (field_label, tpl.sheet_label(field_label)):
                key = _normalise(candidate)
                if key in by_label:
                    return by_label[key]
                if key in by_first_line:
                    return by_first_line[key]
            # Fall back to a prefix match. Some template rows carry guidance
            # after the field name — "Likelihood of Winning" is followed by all
            # four band definitions on continuation lines — so an exact match
            # misses them. Without this the most important row in the brief
            # (the likelihood itself) silently stayed empty.
            return _prefix_match(_normalise(field_label), by_label)

        data, written, unmatched = [], 0, []
        for label, value in brief.fields.items():
            row = find_row(label)
            if not row:
                unmatched.append(label)
                continue
            data.append({
                "range": f"'{tab_name}'!{TEMPLATE_DETAIL_COL}{row}",
                "values": [[value]],
            })
            written += 1

        # Column C, for the rows whose header asks a second question. Sparse by
        # design: a row absent from brief.details is skipped rather than cleared,
        # so nothing this tool writes wipes a note somebody typed there.
        more_written = 0
        for label, value in (brief.details or {}).items():
            row = find_row(label)
            if not row:
                if label not in unmatched:
                    unmatched.append(label)
                continue
            data.append({
                "range": f"'{tab_name}'!{TEMPLATE_MORE_COL}{row}",
                "values": [[value]],
            })
            more_written += 1

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

        logger.info(
            f"Filled report {file_id}: {written} field(s) in column "
            f"{TEMPLATE_DETAIL_COL}, {more_written} in column {TEMPLATE_MORE_COL}"
        )
        return {"fields_written": written, "unmatched": unmatched,
                "details_written": more_written,
                "dimensions_written": len(brief.fit_dimensions or [])}

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

    details = getattr(brief, "details", None) or {}

    for section, label, kind, _src in tpl.section_rows():
        if section:
            lines += ["", f"## {section}", ""]
            if section == tpl.HEADING_3_FIT:
                # The matrix is a table in the sheet, so it renders as one here —
                # its two columns are two different questions and collapsing them
                # into a sentence loses which answer is the tender's and which is
                # Onepoint's.
                lines += ["| Domain | Required capability | Onepoint evidence | Rating |",
                          "| --- | --- | --- | --- |"]
                for d in brief.fit_dimensions:
                    lines.append(
                        f"| {d['dimension']} | {d.get('required','')} | "
                        f"{d.get('evidence','')} | {d.get('rating','')} |"
                    )
                if not brief.fit_dimensions:
                    lines.append("| _No fit assessment produced._ | | | |")
            continue
        if not label:
            continue
        value = brief.fields.get(label, "")
        more = details.get(label, "")
        lines.append(f"- **{label}:** {value}" + (f"  _({more})_" if more else ""))
    return "\n".join(lines) + "\n"
