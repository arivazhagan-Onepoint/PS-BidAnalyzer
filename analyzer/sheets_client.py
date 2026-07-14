"""
Google Sheets client for the Bid Analyzer.

Reads tenders from the "PS Tender Tracker" sheet referenced in project_config.json
and writes back the qualification produced by the analyzer:
  [Bid Qualification], [Bid Qualification Reason(System)], [Bid Qualification Date]
plus the control columns [Comments], [Processed Date], [Last Modified Date],
[Created Date].

Authentication reuses the shared service account (config.SERVICE_ACCOUNT_FILE),
matching adapters/adapter1/google_sheets_auth.py.
"""
import logging
import random
import time
from datetime import datetime

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import (
    SCOPES,
    SERVICE_ACCOUNT_FILE,
    SHEET_NAME,
    TARGET_FOLDER_ID,
    DATASET_FIELDS,
    UK_TIMEZONE,
    qualification_family,
)

logger = logging.getLogger(__name__)

RATE_LIMIT_STATUS_CODES = (429, 403)

# Sheet layout: row 1 = user summary, row 2 = headers, row 3+ = tender data.
HEADER_ROW = 2
FIRST_DATA_ROW = 3

# Columns the analyzer reads and writes.
TITLE_FIELD       = "Name"
DESCRIPTION_FIELD = "Tender Description"

# Row background colours — same palette as adapters/adapter1/sheets_writer.py.
ROW_COLORS = {
    'green':      {'red': 0.20, 'green': 0.74, 'blue': 0.39},
    'light_grey': {'red': 0.85, 'green': 0.85, 'blue': 0.85},
    'amber':      {'red': 1.00, 'green': 0.75, 'blue': 0.00},
    'red':        {'red': 0.90, 'green': 0.20, 'blue': 0.20},
    'yellow':     {'red': 1.00, 'green': 0.95, 'blue': 0.20},
    'white':      {'red': 1.00, 'green': 1.00, 'blue': 1.00},
}

# Qualification family -> row colour (matched by prefix, so Bid(AI)/Bid(Human)
# all share the Bid colour). Matches the scheme in sheets_writer.py: Bid=white,
# NoBid=red. TBD has no equivalent there, so it uses yellow (in-between).
FAMILY_COLORS = {
    "Bid":   ROW_COLORS['white'],
    "TBD":   ROW_COLORS['yellow'],
    "NoBid": ROW_COLORS['red'],
}


def status_color(status: str):
    """Return the background colour for a status by its family, or None."""
    return FAMILY_COLORS.get(qualification_family(status))


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
        self.data = data        # {field: value} for all DATASET_FIELDS

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
        self.sheet_tab_id = None  # numeric tab ID, needed for batchUpdate (colouring)
        self._field_idx = {}      # field name -> 0-based column index in the sheet
        self._num_cols = len(DATASET_FIELDS)  # columns to span when colouring a row

    # --- low level -----------------------------------------------------------
    def _execute_with_retry(self, request_fn, max_retries=6):
        """Execute a Sheets/Drive API call, retrying rate-limit errors with backoff."""
        for attempt in range(max_retries):
            try:
                return request_fn()
            except HttpError as e:
                if e.resp.status in RATE_LIMIT_STATUS_CODES and attempt < max_retries - 1:
                    wait = min(120, (2 ** attempt) * 5 + random.uniform(0, 2))
                    logger.warning(f"Rate limit hit (attempt {attempt + 1}/{max_retries}), retrying in {wait:.1f}s...")
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
                f"Sheet '{SHEET_NAME}' not found in folder {TARGET_FOLDER_ID}. "
                f"Run the scraper adapters first to populate it."
            )
        self.sheet_id = files[0]["id"]
        self._get_sheet_tab_id()
        logger.info(f"Opened sheet '{SHEET_NAME}' ({self.sheet_id})")
        return self.sheet_id

    def _get_sheet_tab_id(self):
        """Fetch the numeric tab ID for the named tab — required for batchUpdate."""
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
        """Read all tender rows from the sheet as a list of Tender objects."""
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
        self._num_cols = len(headers) or len(DATASET_FIELDS)

        for required in (TITLE_FIELD, DESCRIPTION_FIELD):
            if required not in self._field_idx:
                raise ValueError(f"Required column '{required}' not found in sheet headers: {headers}")

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

    def write_qualifications(self, updates: list) -> int:
        """Write qualification + control columns back to the sheet in one batch.

        ``updates`` is a list of (row_number, {field: value}) tuples. Only fields
        present in the sheet's header row are written. Returns the number of rows
        updated.
        """
        data = []
        for row, field_values in updates:
            for field, value in field_values.items():
                if field not in self._field_idx:
                    logger.warning(f"Column '{field}' not in sheet; skipping for row {row}")
                    continue
                data.append({"range": self._cell_range(field, row), "values": [[value]]})

        if not data:
            return 0

        self._execute_with_retry(
            lambda: self.sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()
        )
        logger.info(f"Wrote qualification updates to {len(updates)} row(s) ({len(data)} cells)")
        return len(updates)

    def apply_row_colors(self, row_color_map: dict) -> int:
        """Apply a background colour to entire rows in one batchUpdate call.

        ``row_color_map`` maps a 1-based sheet row number to a colour dict (see
        ROW_COLORS). Mirrors adapters/adapter1/sheets_writer.py._apply_row_colors.
        Returns the number of rows coloured.
        """
        if not row_color_map:
            return 0
        requests = []
        for row_num, color in row_color_map.items():
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": self.sheet_tab_id,
                        "startRowIndex": row_num - 1,
                        "endRowIndex": row_num,
                        "startColumnIndex": 0,
                        "endColumnIndex": self._num_cols,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": color}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })
        self._execute_with_retry(
            lambda: self.sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"requests": requests},
            ).execute()
        )
        logger.info(f"Applied row background colours to {len(requests)} row(s)")
        return len(requests)
