"""
Google Sheets client for the detailed analysis stage.

Reads tenders from the "PS Tender Tracker" sheet referenced in
project_config.json and writes this stage's output back to the columns mapped in
``config.OUTPUT_FIELD_MAP``, plus the control columns [Comments],
[Processed Date], [Last Modified Date].

Deliberately slimmer than ``analyzer/sheets_client.py``: it carries the read and
write it needs and nothing else. Row colouring and the collection-tab sync are
the analyzer's and the knowledge-maintenance flow's concerns — duplicating them
here would mean two implementations of the same sheet mutation, which is how the
two stages would start fighting over the same cells.

Authentication reuses the shared service account (config.SERVICE_ACCOUNT_FILE).
The sheet's structure (tabs, header row) is maintained by hand — this module
never creates a tab or a column.
"""
import logging
import random
import re
import time

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import (
    SCOPES,
    SERVICE_ACCOUNT_FILE,
    SHEET_NAME,
    TARGET_FOLDER_ID,
    DATASET_FIELDS,
)

logger = logging.getLogger(__name__)

RATE_LIMIT_STATUS_CODES = (429, 403)

# Sheet layout: row 1 = user summary, row 2 = headers, row 3+ = tender data.
HEADER_ROW = 2
FIRST_DATA_ROW = 3

# Columns every detailed analysis needs at minimum; the rest of the row is
# carried along in Tender.data for the prompt to draw on.
TITLE_FIELD       = "Name"
DESCRIPTION_FIELD = "Tender Description"


# URLs inside a log entry. Trailing punctuation is excluded from the match so a
# link at the end of a sentence does not swallow the full stop.
_URL_RE = re.compile(r"https?://[^\s<>\"]+[^\s<>\".,;:)\]]")


def _link_runs(text: str) -> list:
    """textFormatRuns turning every URL in ``text`` into a hyperlink.

    A run at index 0 with empty formatting is always emitted first: the API takes
    runs as "formatting from here on", so without it the first URL's link styling
    would bleed backwards over the text preceding it. Each link run is likewise
    closed by a plain run at the URL's end.
    """
    runs, cursor = [], 0
    for match in _URL_RE.finditer(text):
        start, end = match.span()
        if start > cursor or not runs:
            runs.append({"startIndex": cursor, "format": {}})
        runs.append({"startIndex": start, "format": {"link": {"uri": match.group(0)}}})
        cursor = end
    if runs and cursor < len(text):
        runs.append({"startIndex": cursor, "format": {}})
    # Runs must be strictly increasing; a URL at index 0 makes the opening plain
    # run and the link run collide, so drop the redundant one.
    deduped = []
    for run in runs:
        if deduped and deduped[-1]["startIndex"] == run["startIndex"]:
            deduped[-1] = run
        else:
            deduped.append(run)
    return deduped


def _col_letter(n: int) -> str:
    """Convert 1-based column number to a spreadsheet column letter (1=A, 27=AA)."""
    result = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


LAST_COL = _col_letter(len(DATASET_FIELDS))


class Tender:
    """A single tender row read from the sheet."""

    __slots__ = ("row", "data")

    def __init__(self, row: int, data: dict):
        self.row = row          # 1-based sheet row number
        self.data = data        # {field: value} for every named column present

    @property
    def title(self) -> str:
        return self.data.get(TITLE_FIELD, "")

    @property
    def description(self) -> str:
        return self.data.get(DESCRIPTION_FIELD, "")


class SheetsClient:
    def __init__(self):
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        self.sheets_service = build("sheets", "v4", credentials=creds)
        self.drive_service = build("drive", "v3", credentials=creds)
        logger.info("Authenticated Google service account (sheets v4, drive v3)")
        self.sheet_id = None
        self.sheet_tab_id = None  # numeric tab ID — used to deep-link the email
        self._field_idx = {}      # field name -> 0-based column index in the sheet

    # --- low level -----------------------------------------------------------
    def _execute_with_retry(self, request_fn, max_retries=6):
        """Execute a Sheets/Drive API call, retrying rate-limit errors with backoff."""
        for attempt in range(max_retries):
            try:
                return request_fn()
            except HttpError as e:
                if e.resp.status in RATE_LIMIT_STATUS_CODES and attempt < max_retries - 1:
                    wait = min(120, (2 ** attempt) * 5 + random.uniform(0, 2))
                    logger.warning(
                        f"Rate limit hit (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {wait:.1f}s..."
                    )
                    time.sleep(wait)
                else:
                    raise

    # --- sheet discovery -----------------------------------------------------
    def open_sheet(self) -> str:
        """Locate the PS Tender Tracker sheet in the target folder. Raises if absent."""
        query = (
            f"name='{SHEET_NAME}' and '{TARGET_FOLDER_ID}' in parents "
            f"and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
        )
        results = self._execute_with_retry(
            lambda: self.drive_service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
        )
        files = results.get("files", [])
        if not files:
            raise FileNotFoundError(
                f"Sheet '{SHEET_NAME}' not found in folder {TARGET_FOLDER_ID}."
            )
        self.sheet_id = files[0]["id"]
        self._get_sheet_tab_id()
        logger.info(f"Opened sheet '{SHEET_NAME}' ({self.sheet_id})")
        return self.sheet_id

    def _get_sheet_tab_id(self):
        """Fetch the numeric tab ID for the named tab (used for the email link)."""
        spreadsheet = self._execute_with_retry(
            lambda: self.sheets_service.spreadsheets().get(
                spreadsheetId=self.sheet_id,
                fields="sheets.properties",
            ).execute()
        )
        for sheet in spreadsheet.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == SHEET_NAME:
                self.sheet_tab_id = props["sheetId"]
                return
        self.sheet_tab_id = 0

    # --- read ----------------------------------------------------------------
    def read_tenders(self) -> list:
        """Read all tender rows from the sheet as a list of Tender objects.

        Columns are keyed by the sheet's own header row (row 2), so the whole row
        is available to the prompt regardless of column order.
        """
        result = self._execute_with_retry(
            lambda: self.sheets_service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id,
                range=f"'{SHEET_NAME}'!A:{LAST_COL}",
            ).execute()
        )
        values = result.get("values", [])
        if len(values) < HEADER_ROW:
            logger.warning("Sheet has no header row; nothing to analyse.")
            return []

        headers = values[HEADER_ROW - 1]
        self._field_idx = {h: i for i, h in enumerate(headers) if h}

        for required in (TITLE_FIELD, DESCRIPTION_FIELD):
            if required not in self._field_idx:
                raise ValueError(
                    f"Required column '{required}' not found in sheet headers: {headers}"
                )

        tenders = []
        for offset, row in enumerate(values[HEADER_ROW:], start=FIRST_DATA_ROW):
            data = {
                field: (row[idx] if idx < len(row) else "")
                for field, idx in self._field_idx.items()
            }
            tenders.append(Tender(row=offset, data=data))

        logger.info(f"Read {len(tenders)} tender rows from the sheet")
        return tenders

    # --- write ---------------------------------------------------------------
    def _cell_range(self, field: str, row: int) -> str:
        col = _col_letter(self._field_idx[field] + 1)
        return f"'{SHEET_NAME}'!{col}{row}"

    def write_updates(self, updates: list, link_fields=()) -> int:
        """Write this stage's output back to the sheet. Returns rows updated.

        ``updates`` is a list of (row_number, {field: value}) tuples. A field the
        sheet's header row does not contain is skipped with a warning rather than
        created — the sheet's structure is maintained by hand.

        ``link_fields`` names the fields whose text should have any URLs in it
        turned into working hyperlinks. Those cells go through updateCells with
        explicit rich-text runs rather than a plain value write, because Sheets
        does not auto-link a URL embedded in a longer block of text (measured, not
        assumed — under RAW and USER_ENTERED alike). Everything else is written as
        RAW, which is the safe mode for cells holding model-written prose: under
        USER_ENTERED a sentence opening with "=" would be parsed as a formula.
        """
        link_fields = set(link_fields)
        data, rich_requests = [], []

        for row, field_values in updates:
            for field, value in field_values.items():
                if field not in self._field_idx:
                    logger.warning(
                        f"Column '{field}' not in sheet; skipping for row {row}. "
                        f"Add the column to the tracker by hand, then map it in "
                        f"config.OUTPUT_FIELD_MAP."
                    )
                    continue
                if field in link_fields and _URL_RE.search(str(value)):
                    rich_requests.append(
                        self._rich_text_request(field, row, str(value))
                    )
                else:
                    data.append(
                        {"range": self._cell_range(field, row), "values": [[value]]}
                    )

        if not (data or rich_requests):
            return 0

        if data:
            self._execute_with_retry(
                lambda: self.sheets_service.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.sheet_id,
                    body={"valueInputOption": "RAW", "data": data},
                ).execute()
            )
        if rich_requests:
            self._execute_with_retry(
                lambda: self.sheets_service.spreadsheets().batchUpdate(
                    spreadsheetId=self.sheet_id,
                    body={"requests": rich_requests},
                ).execute()
            )

        logger.info(
            f"Wrote detailed analysis to {len(updates)} row(s) "
            f"({len(data)} value cell(s), {len(rich_requests)} linked cell(s))"
        )
        return len(updates)

    def _rich_text_request(self, field: str, row: int, text: str) -> dict:
        """An updateCells request writing ``text`` with every URL hyperlinked.

        EVERY URL in the cell is linked, not just this run's. These columns are
        append-only logs, and updateCells replaces the cell's formatting wholesale
        — so linking only the newest entry would strip the links off every report
        recorded by a previous run.
        """
        col = self._field_idx[field]
        return {
            "updateCells": {
                "range": {
                    "sheetId": self.sheet_tab_id,
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": col,
                    "endColumnIndex": col + 1,
                },
                "rows": [{
                    "values": [{
                        "userEnteredValue": {"stringValue": text},
                        "textFormatRuns": _link_runs(text),
                    }]
                }],
                "fields": "userEnteredValue,textFormatRuns",
            }
        }
