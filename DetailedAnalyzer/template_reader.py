"""
Reads the published reporting template and works out what the brief must answer.

The template is a live document the bid team edits — it went 60 rows, then 88,
then 114 in under a month, renumbering its sections on the way. Transcribing it
into Python meant a commit per revision, and a transcription that silently fell
behind the sheet in between. So the structure is READ instead, once per run,
from the template the config names.

What makes that possible is that the template already encodes its own structure
in colour, and the bid team maintains those colours deliberately:

    #9B3642  the sheet's own header row (Section / Detail / More Details)
    #980000  a numbered section heading
    #EB7C7C  a table header, or a lettered sub-heading
    #FFFF00  an INSTRUCTION — prose telling whoever fills the brief what to put
             in the adjacent column
    white / #F6F8F9 / no fill   an ordinary data row

Classification is by luminance rather than by exact hex, so restyling the
template a shade lighter or darker does not silently turn section headings into
questions. Only yellow is matched on hue, because yellow is the one colour the
template uses to mean something specific.

The two shapes a yellow row can take, and how they are told apart:

  * An ANSWERABLE instruction — "Perform the detailed assessment on Commercial
    perspective… <in the Adjacent Column>". The answer goes in column B of that
    same row, and the instruction text is what the model is asked.
  * A TABLE PREAMBLE — an instruction sitting directly above a table header,
    describing the block beneath rather than asking for an answer beside itself
    ("Summarize the risks under the following sub-categories as listed below").
    Nothing is written into a preamble row.

A table header is a structural row carrying two or more columns of text; a
lettered sub-heading like "4B. Amber Flags / Mitigations" carries one, which is
what keeps the instruction beneath it from being mistaken for a preamble.

A table whose preamble says to EXPAND it is generated rather than filled: the
template ships "Deliverable 1" and "Deliverable 2" as placeholders, and a real
tender has as many deliverables as it has. Any other table's column A is the
real list and is left exactly as it is.
"""
import logging
import re
from dataclasses import dataclass, field

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from .config import (
    SCOPES,
    SERVICE_ACCOUNT_FILE,
    REPORTING_TEMPLATE_NAME,
    REPORTING_TEMPLATES_FOLDER_ID,
    TEMPLATE_TAB_NAME,
)

logger = logging.getLogger(__name__)

# Row roles, in the template's own visual language (see module docstring).
ROLE_SHEET_HEADER = "sheet_header"
ROLE_SECTION = "section"
ROLE_TABLE_HEADER = "table_header"
ROLE_SUB_HEADING = "sub_heading"
ROLE_INSTRUCTION = "instruction"
ROLE_DATA = "data"

# A background this bright is the page, not a label. The template's data rows are
# #FFFFFF and #F6F8F9 (0.97); its palest structural colour is #EB7C7C (0.58).
_DATA_LUMINANCE = 0.85

# The marker a preamble uses to say its table is a placeholder to be grown.
_EXPAND_MARKER = "expand"


# --- the published template: find it, read it, once per run -----------------
# Both the analysis stage and the report writer need the same document — one to
# know what to ask, the other to know where to write it — so resolution and
# reading live here rather than on either of them, and are cached for the run.
_SERVICES = {}
_TEMPLATE_ID = {}
_MODEL = {}


def _services():
    if not _SERVICES:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _SERVICES["sheets"] = build("sheets", "v4", credentials=creds)
        _SERVICES["drive"] = build("drive", "v3", credentials=creds)
    return _SERVICES["sheets"], _SERVICES["drive"]


def template_id(drive=None) -> str:
    """The configured reporting template's file ID, looked up once per run.

    Resolved by NAME inside the configured templates folder, so moving to a new
    version of the template is a config edit. Exactly one match is required: two
    same-named templates would otherwise be chosen between by Drive's listing
    order, and every brief afterwards would be built from whichever it returned
    first — a difference nobody would see in the output.
    """
    if _TEMPLATE_ID:
        return _TEMPLATE_ID["id"]
    if drive is None:
        _, drive = _services()

    query = (
        f"name='{REPORTING_TEMPLATE_NAME}' and "
        f"'{REPORTING_TEMPLATES_FOLDER_ID}' in parents and "
        f"mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    )
    files = drive.files().list(
        q=query, spaces="drive", fields="files(id,name)", pageSize=10,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get("files", [])

    if not files:
        raise FileNotFoundError(
            f"Reporting template {REPORTING_TEMPLATE_NAME!r} was not found in "
            f"Drive folder {REPORTING_TEMPLATES_FOLDER_ID}. Check "
            f"google_sheets.Reporting_Template in project_config.json matches the "
            f"file name exactly, and that the service account can see the folder."
        )
    if len(files) > 1:
        raise RuntimeError(
            f"{len(files)} spreadsheets in folder {REPORTING_TEMPLATES_FOLDER_ID} "
            f"are named {REPORTING_TEMPLATE_NAME!r} ({', '.join(f['id'] for f in files)}). "
            f"Rename or remove all but one — picking between them would make every "
            f"brief depend on Drive's listing order."
        )

    _TEMPLATE_ID["id"] = files[0]["id"]
    logger.info(
        f"Reporting template {REPORTING_TEMPLATE_NAME!r} resolved to {files[0]['id']}"
    )
    return _TEMPLATE_ID["id"]


def load_template_model():
    """The structure of the published template, read once and cached per run."""
    if not _MODEL:
        sheets, drive = _services()
        _MODEL["model"] = read_template(sheets, template_id(drive), TEMPLATE_TAB_NAME)
    return _MODEL["model"]


def _luminance(bg: dict) -> float:
    if not bg:
        return 1.0
    return (0.2126 * bg.get("red", 0.0)
            + 0.7152 * bg.get("green", 0.0)
            + 0.0722 * bg.get("blue", 0.0))


def _is_yellow(bg: dict) -> bool:
    """Yellow as the template means it — red and green up, blue down.

    Matched on hue rather than an exact hex so a slightly different yellow still
    reads as an instruction, while nothing else in the palette can drift into it.
    """
    if not bg:
        return False
    return (bg.get("red", 0.0) > 0.8
            and bg.get("green", 0.0) > 0.8
            and bg.get("blue", 0.0) < 0.4)


@dataclass
class Row:
    number: int                 # 1-based sheet row
    cells: list                 # text per column, column A first
    role: str
    section: str = ""           # the section heading this row sits under

    @property
    def label(self) -> str:
        return self.cells[0] if self.cells else ""

    @property
    def first_line(self) -> str:
        return (self.label or "").replace("\r", "\n").split("\n")[0].strip()

    @property
    def filled_cells(self) -> int:
        return sum(1 for c in self.cells if (c or "").strip())


@dataclass
class Table:
    header: Row
    rows: list = field(default_factory=list)
    preamble: str = ""
    generated: bool = False

    @property
    def columns(self) -> list:
        return [c for c in self.header.cells if (c or "").strip()]

    @property
    def has_more_column(self) -> bool:
        """True when the table has a third column to fill."""
        return len(self.columns) >= 3


@dataclass
class TemplateModel:
    rows: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    # Rows the model is asked about: ordinary data rows, answerable instruction
    # rows, and every row of every table.
    questions: list = field(default_factory=list)

    def row(self, number: int) -> Row:
        for r in self.rows:
            if r.number == number:
                return r
        return None

    @property
    def table_by_header_row(self) -> dict:
        return {t.header.number: t for t in self.tables}

    def table_of(self, row_number: int) -> Table:
        for t in self.tables:
            if any(r.number == row_number for r in t.rows):
                return t
        return None


def _classify(row_values: list) -> tuple:
    """Return (cells, role) for one row of the grid."""
    cells, bgs = [], []
    for v in row_values:
        cells.append((v.get("formattedValue") or "").strip())
        bgs.append((v.get("effectiveFormat") or {}).get("backgroundColor") or {})

    # Trailing empties carry no meaning and make the column counts misleading.
    while cells and not cells[-1]:
        cells.pop()
        bgs.pop()

    if not cells:
        return [], ROLE_DATA

    bg = bgs[0]
    filled = sum(1 for c in cells if c)

    if _is_yellow(bg):
        return cells, ROLE_INSTRUCTION
    if _luminance(bg) < _DATA_LUMINANCE:
        # Structural. Two or more columns of text is a table's header; one is a
        # section or a lettered sub-heading.
        if filled >= 2:
            return cells, ROLE_TABLE_HEADER
        if re.match(r"^\d+\.\s", cells[0]):
            return cells, ROLE_SECTION
        return cells, ROLE_SUB_HEADING
    return cells, ROLE_DATA


def read_template(sheets, file_id: str, tab_name: str = None) -> TemplateModel:
    """Read the published template and return its structure.

    One API call. ``tab_name`` defaults to the first tab, which is what the
    report writer copies and fills.
    """
    meta = sheets.spreadsheets().get(
        spreadsheetId=file_id,
        includeGridData=True,
        fields=("sheets.properties.title,"
                "sheets.data.rowData.values("
                "formattedValue,effectiveFormat.backgroundColor)"),
    ).execute()

    sheet = meta["sheets"][0]
    if tab_name:
        for s in meta["sheets"]:
            if s["properties"]["title"] == tab_name:
                sheet = s
                break

    grid = (sheet.get("data") or [{}])[0].get("rowData", [])

    model = TemplateModel()
    section = ""
    first_structural_seen = False

    for i, raw in enumerate(grid, start=1):
        cells, role = _classify(raw.get("values") or [])
        if not cells:
            continue
        # The sheet's own header row is the first structural row with columns,
        # before any section has started.
        if role == ROLE_TABLE_HEADER and not first_structural_seen and not section:
            role = ROLE_SHEET_HEADER
        if role in (ROLE_SECTION, ROLE_TABLE_HEADER, ROLE_SUB_HEADING,
                    ROLE_SHEET_HEADER):
            first_structural_seen = True
        if role == ROLE_SECTION:
            section = cells[0]
        model.rows.append(Row(number=i, cells=cells, role=role, section=section))

    _build_tables(model)
    _build_questions(model)

    logger.info(
        f"Template structure: {len(model.rows)} row(s), "
        f"{sum(1 for r in model.rows if r.role == ROLE_SECTION)} section(s), "
        f"{len(model.tables)} table(s), "
        f"{sum(1 for r in model.rows if r.role == ROLE_INSTRUCTION)} instruction(s)"
    )
    return model


def _build_tables(model: TemplateModel):
    """Attach the data rows under each table header, and its preamble above it."""
    by_index = {r.number: n for n, r in enumerate(model.rows)}

    for n, row in enumerate(model.rows):
        if row.role != ROLE_TABLE_HEADER:
            continue
        table = Table(header=row)

        # Data rows run until anything structural interrupts them.
        for later in model.rows[n + 1:]:
            if later.role != ROLE_DATA:
                break
            table.rows.append(later)

        # A yellow row immediately above is this table's preamble.
        if n > 0 and model.rows[n - 1].role == ROLE_INSTRUCTION:
            preamble_row = model.rows[n - 1]
            table.preamble = preamble_row.label
            table.generated = _EXPAND_MARKER in preamble_row.label.lower()

        if table.rows:
            model.tables.append(table)

    _ = by_index  # kept for clarity of intent; lookups above are positional


def _build_questions(model: TemplateModel):
    """Every row the model may be asked about, in the sheet's own order.

    Table preambles are excluded — they describe the block below rather than
    asking for anything beside themselves — and so is every structural row.
    """
    preamble_rows = set()
    for t in model.tables:
        if t.preamble:
            # The preamble is the instruction row directly above the header.
            preamble_rows.add(t.header.number - 1)

    for row in model.rows:
        if row.role == ROLE_DATA:
            model.questions.append(row)
        elif row.role == ROLE_INSTRUCTION and row.number not in preamble_rows:
            model.questions.append(row)
