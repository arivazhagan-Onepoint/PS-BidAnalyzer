"""
The tender's own document pack — the third evidence stream in a detailed brief.

``onepoint_context`` says what Onepoint can do and ``sources`` holds the evidence
behind it; both describe Onepoint and are identical for every tender. This module
supplies the other side: the buyer's published pack for ONE tender — the ITT, the
draft contract, the code of conduct — read from the Drive folder in
``config.TENDER_DOCS_FOLDER_ID``, which holds one subfolder per tender named
``<OCID>-<Tender Title>``.

It is what turns Section 1's "Mandatory Requirement" from an inference off a
notice summary into a statement of what the bid would actually be evaluated
against.

Deliberately not part of ``sources.py``. That corpus is tender-independent and
built on its own cadence; a pack belongs to its tender, so it is fetched during
the run, per row, and cached per OCID.

Three things here are decisions rather than plumbing, each measured on the real
CITB pack on 2026-08-23 — see the config block for the numbers:

  * Text comes from the DOWNLOADED BYTES, never from converting a copy in Drive.
    Conversion works but is ~10x slower and strands a temp file in the customer's
    own folder that the service account has no permission to delete.
  * Superseded versions are detected by word-shingle Jaccard, not difflib, which
    is both far cheaper and far better separated on this data.
  * Which copy wins is decided by the filename's version marker, never by
    createdTime/modifiedTime — both were measured useless or actively wrong here.

Public API:
    load_tender_documents(tender_data) -> TenderDocuments
"""
import hashlib
import io
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from .config import (
    SCOPES,
    SERVICE_ACCOUNT_FILE,
    TENDER_DOCS_ENABLED,
    TENDER_DOCS_FOLDER_ID,
    TENDER_DOCS_MATCH_FIELD,
    TENDER_DOCS_CACHE_DIR,
    TENDER_DOCS_SUPPORTED_MIMES,
    TENDER_DOCS_MAX_TOTAL_CHARS,
    TENDER_DOCS_MIN_DOC_CHARS,
    TENDER_DOCS_DEDUPE_ENABLED,
    TENDER_DOCS_SIMILARITY_THRESHOLD,
    TENDER_DOCS_SHINGLE_WORDS,
    TENDER_DOCS_VERSION_PATTERNS,
    TENDER_DOCS_IMPLIED_VERSION,
    TENDER_DOCS_WARN_UNRESOLVED,
    DOCX_MIME,
    PDF_MIME,
    GDOC_MIME,
    XLSX_MIME,
    GSHEET_MIME,
)

logger = logging.getLogger(__name__)

# Word namespace in a .docx's document.xml.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# SpreadsheetML namespaces, for .xlsx. Same trick as .docx — the file is a zip of
# XML, so the standard library is enough and no dependency is added.
_XL = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XL_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_VERSION_RES = tuple(re.compile(p, re.IGNORECASE) for p in TENDER_DOCS_VERSION_PATTERNS)


@dataclass
class Document:
    """One document from a tender's pack."""
    file_id: str
    name: str
    mime: str
    text: str = ""
    error: str = ""
    superseded_by: str = ""
    truncated_from: int = 0

    @property
    def used(self) -> bool:
        return bool(self.text) and not self.error and not self.superseded_by


@dataclass
class TenderDocuments:
    """Every document found for one tender, plus how they were treated."""
    ocid: str = ""
    folder_name: str = ""
    documents: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    from_cache: bool = False

    @property
    def used(self) -> list:
        return [d for d in self.documents if d.used]

    @property
    def total_chars(self) -> int:
        return sum(len(d.text) for d in self.used)

    def as_prompt_block(self) -> str:
        """The pack rendered for the prompt, one fenced section per document."""
        if not self.used:
            return ""
        parts = []
        for d in self.used:
            note = ""
            if d.truncated_from:
                note = (
                    f"\n\n[... this document was truncated to fit the analysis "
                    f"budget: {d.truncated_from - len(d.text):,} of "
                    f"{d.truncated_from:,} characters are not shown. Do not treat "
                    f"the omitted part as absent — say so if something material "
                    f"would have been in it ...]"
                )
            parts.append(f"=== DOCUMENT: {d.name} ===\n{d.text}{note}")
        return "\n\n".join(parts)

    def manifest_lines(self) -> list:
        """One human-readable line per document, for the log, email and report."""
        lines = []
        for d in self.documents:
            if d.error:
                lines.append(f"{d.name} — NOT READ ({d.error})")
            elif d.superseded_by:
                lines.append(f"{d.name} — superseded by {d.superseded_by}, not used")
            elif d.truncated_from:
                lines.append(
                    f"{d.name} — {len(d.text):,} of {d.truncated_from:,} chars "
                    f"(truncated to fit the analysis budget)"
                )
            else:
                lines.append(f"{d.name} — {len(d.text):,} chars")
        return lines


# --- Drive ------------------------------------------------------------------
_drive = None


def _drive_service():
    global _drive
    if _drive is None:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _drive = build("drive", "v3", credentials=creds)
    return _drive


_sheets = None


def _sheets_service():
    """Only built when a pack actually contains a native Google Sheet."""
    global _sheets
    if _sheets is None:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _sheets = build("sheets", "v4", credentials=creds)
    return _sheets


def find_tender_folder(ocid: str) -> dict:
    """The pack subfolder whose name starts with this OCID, or None.

    Matching on the OCID prefix alone is what lets the folder's title half be
    reworded freely, and what keeps the placeholder "Sample Tender #…" folders out
    without needing to name them in a skip-list.
    """
    if not ocid:
        return None
    # `name contains` is a substring match server-side; the prefix is then checked
    # in Python, so a folder merely mentioning the OCID cannot match.
    escaped = ocid.replace("'", "\\'")
    res = _drive_service().files().list(
        q=(f"'{TENDER_DOCS_FOLDER_ID}' in parents and trashed=false "
           f"and mimeType='application/vnd.google-apps.folder' "
           f"and name contains '{escaped}'"),
        fields="files(id,name)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    matches = [f for f in res.get("files", []) if f["name"].startswith(ocid)]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            f"{len(matches)} folders start with OCID {ocid}: "
            f"{[m['name'] for m in matches]}. Using the first."
        )
    return matches[0]


def list_documents(folder_id: str) -> list:
    """Every file directly in a tender's folder, with the metadata we fingerprint."""
    files, page = [], None
    while True:
        res = _drive_service().files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType,size,modifiedTime)",
            pageSize=200,
            pageToken=page,
            orderBy="name",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(res.get("files", []))
        page = res.get("nextPageToken")
        if not page:
            return files


def _download(file_id: str) -> bytes:
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buf, _drive_service().files().get_media(fileId=file_id, supportsAllDrives=True)
    )
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _export_gdoc(file_id: str) -> bytes:
    """Plain text of a native Google Doc. Export, not convert — nothing is created."""
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buf, _drive_service().files().export_media(fileId=file_id, mimeType="text/plain")
    )
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# --- Extraction -------------------------------------------------------------
def _docx_text(data: bytes) -> str:
    """Paragraph and table text from a .docx. Standard library only.

    A .docx is a zip of XML, so this needs no dependency. Headers and footers are
    included after the body: a tender document routinely carries its reference
    number, version and issue date there and nowhere else.
    """
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        ordered = ["word/document.xml"] + sorted(
            n for n in names if re.fullmatch(r"word/(?:header|footer)\d*\.xml", n)
        )
        for name in ordered:
            if name not in names:
                continue
            root = ET.fromstring(z.read(name))
            for para in root.iter(f"{_W}p"):
                text = "".join(node.text or "" for node in para.iter(f"{_W}t")).strip()
                if text:
                    out.append(text)
    return "\n".join(out)


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n".join(p for p in pages if p)


def _col_index(ref: str) -> int:
    """0-based column from a cell reference: A1 -> 0, B12 -> 1, AA3 -> 26."""
    letters = "".join(ch for ch in (ref or "") if ch.isalpha()).upper()
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return max(0, n - 1)


def _xlsx_shared_strings(z) -> list:
    """The workbook's string table. Cells reference it by index, not by value."""
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return [
        "".join(t.text or "" for t in si.iter(f"{_XL}t"))
        for si in root.iter(f"{_XL}si")
    ]


def _xlsx_sheets(z) -> list:
    """[(sheet title, part path)] in the workbook's own tab order.

    Resolved through the relationship table rather than assuming
    ``xl/worksheets/sheet1.xml``: part names need not match tab order, and a
    workbook whose first tab is sheet3.xml is perfectly legal.
    """
    names = z.namelist()
    if "xl/workbook.xml" not in names:
        return []
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
            rels[rel.get("Id")] = rel.get("Target") or ""
    out = []
    for sheet in ET.fromstring(z.read("xl/workbook.xml")).iter(f"{_XL}sheet"):
        target = rels.get(sheet.get(f"{_XL_REL}id"), "")
        target = target[1:] if target.startswith("/") else target
        if target and not target.startswith("xl/"):
            target = "xl/" + target
        if target in names:
            out.append((sheet.get("name") or "Sheet", target))
    return out


def _xlsx_rows(z, path: str, shared: list) -> list:
    """Rows of one worksheet, each a list of cell strings with gaps preserved.

    Blank columns are kept as empty strings rather than collapsed, so a value
    stays under the header it belongs to — a pricing schedule read with its
    columns shifted is worse than not reading it at all.
    """
    rows = []
    for row in ET.fromstring(z.read(path)).iter(f"{_XL}row"):
        cells = {}
        for cell in row.iter(f"{_XL}c"):
            kind = cell.get("t")
            value = cell.find(f"{_XL}v")
            if kind == "s" and value is not None:
                try:
                    text = shared[int(value.text)]
                except (TypeError, ValueError, IndexError):
                    text = ""
            elif kind == "inlineStr":
                inline = cell.find(f"{_XL}is")
                text = ("".join(t.text or "" for t in inline.iter(f"{_XL}t"))
                        if inline is not None else "")
            else:
                # Numbers come through as written. A date is a serial number here
                # and is left as one: guessing at the workbook's date system could
                # put a wrong date into a brief, and every date that matters is
                # already taken from the tracker and the computed timeline.
                text = value.text if value is not None and value.text else ""
            text = " ".join((text or "").split())
            if text:
                cells[_col_index(cell.get("r"))] = text
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return rows


def _render_sheet(title: str, rows: list) -> list:
    """One sheet as pipe-separated rows, gaps kept so columns stay aligned."""
    if not rows:
        return []
    out = ["--- SHEET: " + title + " ---"]
    for row in rows:
        filled = [c for c in row if c]
        # A row with a single value is a heading or a stray note rather than a
        # table row, so it is emitted on its own. Otherwise one value sitting out
        # in column AA renders as twenty-six empty separators — unreadable, and
        # paid for in tokens.
        out.append(filled[0] if len(filled) == 1
                   else " | ".join(row).rstrip(" |"))
    out.append("")
    return out


def _xlsx_text(data: bytes) -> str:
    """Every sheet of an .xlsx as pipe-separated rows. Standard library only.

    Tender packs ship their pricing schedule and requirements matrix as a
    spreadsheet, so without this the most structured document in the pack was the
    one thing that could not be read — and, being unreadable, it held its row in
    scope on every run.
    """
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = _xlsx_shared_strings(z)
        for title, path in _xlsx_sheets(z):
            out += _render_sheet(title, _xlsx_rows(z, path, shared))
    return "\n".join(out).strip()


def _gsheet_text(file_id: str) -> str:
    """Every tab of a native Google Sheet, read through the Sheets API.

    Converting a pack's .xlsx to a Google Sheet is a helpful act, and it should
    not turn a readable document into an unsupported one.
    """
    service = _sheets_service()
    meta = service.spreadsheets().get(
        spreadsheetId=file_id, fields="sheets.properties.title"
    ).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if not titles:
        return ""
    batch = service.spreadsheets().values().batchGet(
        spreadsheetId=file_id,
        ranges=["'" + t + "'" for t in titles],
        majorDimension="ROWS",
    ).execute()
    out = []
    for title, value_range in zip(titles, batch.get("valueRanges", [])):
        rows = [
            [" ".join((c or "").split()) for c in row]
            for row in value_range.get("values", [])
        ]
        out += _render_sheet(title, [r for r in rows if any(r)])
    return "\n".join(out).strip()


def extract_text(file_meta: dict) -> str:
    """Text of one Drive file, by mime type. Raises on an unreadable document."""
    mime, file_id = file_meta["mimeType"], file_meta["id"]
    if mime == DOCX_MIME:
        return _docx_text(_download(file_id))
    if mime == PDF_MIME:
        return _pdf_text(_download(file_id))
    if mime == GDOC_MIME:
        return _export_gdoc(file_id).decode("utf-8", errors="replace")
    if mime == XLSX_MIME:
        return _xlsx_text(_download(file_id))
    if mime == GSHEET_MIME:
        return _gsheet_text(file_id)
    raise ValueError(f"unsupported type {mime}")


# --- Superseded versions ----------------------------------------------------
def _shingles(text: str) -> set:
    """Word n-grams of a document, for order-aware similarity at linear cost."""
    words = text.lower().split()
    n = TENDER_DOCS_SHINGLE_WORDS
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _version_rank(name: str) -> float:
    """Version number parsed out of a filename; the implied 1 when there is none.

    The highest marker anywhere in the name wins, so "ITT v2 (rev 1).docx" ranks on
    its v2. A name with no marker is version 1, which is what makes "ITT v2" beat a
    plain "ITT".
    """
    best = None
    stem = os.path.splitext(name)[0]
    for pattern in _VERSION_RES:
        for match in pattern.finditer(stem):
            try:
                # "2.1" -> 2.1; a bare "3" -> 3.0. Only the first two parts matter.
                parts = match.group(1).split(".")[:2]
                value = float(".".join(parts))
            except ValueError:
                continue
            if best is None or value > best:
                best = value
    return TENDER_DOCS_IMPLIED_VERSION if best is None else best


def _cluster(docs: list) -> list:
    """Group documents that are the same document, by shingle overlap.

    Union-find over the pairwise matrix. Transitive on purpose: if v1~v2 and v2~v3
    then all three are one document's history, even where v1 and v3 have drifted
    below the threshold between them.
    """
    parent = list(range(len(docs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    shingles = [_shingles(d.text) for d in docs]
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            score = _jaccard(shingles[i], shingles[j])
            if score >= TENDER_DOCS_SIMILARITY_THRESHOLD:
                logger.info(
                    f"  '{docs[i].name}' and '{docs[j].name}' are {score:.2%} "
                    f"identical — same document"
                )
                parent[find(i)] = find(j)

    groups = {}
    for i in range(len(docs)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def resolve_versions(docs: list) -> list:
    """Mark superseded copies. Returns the warnings raised.

    Within a group of copies the highest filename version marker wins. Where no
    marker separates them the group is left alone and warned about, rather than
    resolved by guessing — the same refusal in report_writer._prefix_match.
    """
    warnings = []
    readable = [d for d in docs if d.text and not d.error]
    if not TENDER_DOCS_DEDUPE_ENABLED or len(readable) < 2:
        return warnings

    for group in _cluster(readable):
        if len(group) < 2:
            continue
        members = [readable[i] for i in group]
        ranked = sorted(members, key=lambda d: _version_rank(d.name), reverse=True)
        top = _version_rank(ranked[0].name)
        winners = [d for d in members if _version_rank(d.name) == top]

        if len(winners) > 1:
            if TENDER_DOCS_WARN_UNRESOLVED:
                names = ", ".join(f"'{d.name}'" for d in winners)
                msg = (
                    f"{len(winners)} near-identical documents with no version "
                    f"marker to separate them ({names}). All were kept — they may "
                    f"contradict each other. Rename the current one (e.g. 'v2') or "
                    f"remove the superseded copy from the folder."
                )
                warnings.append(msg)
                logger.warning(msg)
            continue

        keeper = winners[0]
        for doc in members:
            if doc is keeper:
                continue
            doc.superseded_by = keeper.name
            logger.info(
                f"  '{doc.name}' (v{_version_rank(doc.name):g}) superseded by "
                f"'{keeper.name}' (v{top:g}) — not sent to the model"
            )
    return warnings


# --- Budget -----------------------------------------------------------------
def apply_cap(docs: list) -> list:
    """Hold the pack under the character cap. Returns the warnings raised.

    Every document is capped to one common ceiling rather than whole documents
    being dropped, so a short code of conduct survives intact and only the largest
    are cut. A cut is recorded on the document and stated in the prompt text; it is
    never silent.
    """
    used = [d for d in docs if d.used]
    total = sum(len(d.text) for d in used)
    if total <= TENDER_DOCS_MAX_TOTAL_CHARS or not used:
        return []

    # Water-filling: raise a common ceiling until the budget is spent. Documents
    # already under the ceiling keep every character.
    lengths = sorted(len(d.text) for d in used)
    remaining, ceiling = TENDER_DOCS_MAX_TOTAL_CHARS, None
    for i, length in enumerate(lengths):
        if length * (len(lengths) - i) <= remaining:
            remaining -= length
            continue
        ceiling = remaining // (len(lengths) - i)
        break
    if ceiling is None:
        return []
    ceiling = max(ceiling, TENDER_DOCS_MIN_DOC_CHARS)

    for doc in used:
        if len(doc.text) > ceiling:
            doc.truncated_from = len(doc.text)
            doc.text = doc.text[:ceiling]

    cut = [d for d in used if d.truncated_from]
    msg = (
        f"Pack is {total:,} chars, over the {TENDER_DOCS_MAX_TOTAL_CHARS:,} cap; "
        f"{len(cut)} document(s) truncated to {ceiling:,} chars each: "
        f"{', '.join(d.name for d in cut)}"
    )
    logger.warning(msg)
    return [msg]


# --- Cache ------------------------------------------------------------------
def _fingerprint(files: list) -> str:
    """Changes when any document changes — or when the settings that shape the
    cached text change.

    The processing settings are part of the key, not just the file list. Raising
    the cap or the similarity threshold changes what the cached text contains
    while leaving every file in Drive untouched, so a files-only fingerprint would
    keep serving text truncated under the old cap and the new setting would appear
    to do nothing.
    """
    payload = {
        "files": sorted(
            (f["id"], str(f.get("size", "")), str(f.get("modifiedTime", "")))
            for f in files
        ),
        "settings": [
            TENDER_DOCS_MAX_TOTAL_CHARS,
            TENDER_DOCS_MIN_DOC_CHARS,
            TENDER_DOCS_DEDUPE_ENABLED,
            TENDER_DOCS_SIMILARITY_THRESHOLD,
            TENDER_DOCS_SHINGLE_WORDS,
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _cache_paths(ocid: str) -> tuple:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", ocid)
    return (os.path.join(TENDER_DOCS_CACHE_DIR, f"{safe}.md"),
            os.path.join(TENDER_DOCS_CACHE_DIR, f"{safe}.meta.json"))


def _read_cache(ocid: str, fingerprint: str):
    text_path, meta_path = _cache_paths(ocid)
    if not (os.path.exists(text_path) and os.path.exists(meta_path)):
        return None
    try:
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        if meta.get("fingerprint") != fingerprint:
            logger.info("  pack changed in Drive since it was cached — re-reading")
            return None
        with open(text_path, encoding="utf-8") as fh:
            fh.read()  # presence check; documents carry their own text below
        result = TenderDocuments(
            ocid=ocid,
            folder_name=meta.get("folder_name", ""),
            warnings=meta.get("warnings", []),
            from_cache=True,
        )
        result.documents = [Document(**d) for d in meta.get("documents", [])]
        return result
    except Exception as e:
        logger.warning(f"  could not read the cached pack ({e}); re-reading from Drive")
        return None


def _write_cache(result: TenderDocuments, fingerprint: str):
    text_path, meta_path = _cache_paths(result.ocid)
    os.makedirs(TENDER_DOCS_CACHE_DIR, exist_ok=True)
    # The .md is the exact text the model is given, kept readable so an assessment
    # can be checked against its evidence after the fact.
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write(f"# Tender pack — {result.folder_name}\n\n")
        fh.write("\n".join(f"- {line}" for line in result.manifest_lines()))
        fh.write("\n\n---\n\n")
        fh.write(result.as_prompt_block())
        fh.write("\n")
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "fingerprint": fingerprint,
                "folder_name": result.folder_name,
                "warnings": result.warnings,
                "documents": [vars(d) for d in result.documents],
            },
            fh, ensure_ascii=False, indent=2,
        )


# --- Entry point ------------------------------------------------------------
def load_tender_documents(tender_data: dict) -> TenderDocuments:
    """Read, de-duplicate and cap the document pack for one tender row.

    Never raises: a tender with no folder, an unreadable document or a Drive
    outage degrades to whatever was readable, with the shortfall recorded in the
    warnings and the manifest. A pack is extra evidence, and losing it must not
    cost the brief that the notice summary and the corpus would have produced.
    """
    ocid = (tender_data.get(TENDER_DOCS_MATCH_FIELD, "") or "").strip()
    result = TenderDocuments(ocid=ocid)

    if not TENDER_DOCS_ENABLED:
        return result
    if not ocid:
        logger.info(
            f"  row has no {TENDER_DOCS_MATCH_FIELD} — no tender pack can be located"
        )
        return result

    try:
        folder = find_tender_folder(ocid)
    except (HttpError, OSError) as e:
        msg = f"Could not search the tender documents folder: {e}"
        logger.warning(f"  {msg}")
        result.warnings.append(msg)
        return result

    if not folder:
        logger.info(f"  no document folder for {ocid} — analysing on the notice alone")
        return result

    result.folder_name = folder["name"]
    try:
        files = list_documents(folder["id"])
    except (HttpError, OSError) as e:
        msg = f"Could not list '{folder['name']}': {e}"
        logger.warning(f"  {msg}")
        result.warnings.append(msg)
        return result

    if not files:
        logger.info(f"  '{folder['name']}' is empty — analysing on the notice alone")
        return result

    fingerprint = _fingerprint(files)
    cached = _read_cache(ocid, fingerprint)
    if cached is not None:
        logger.info(
            f"  tender pack from cache: {len(cached.used)} document(s), "
            f"{cached.total_chars:,} chars"
        )
        return cached

    logger.info(f"  reading tender pack '{folder['name']}' ({len(files)} file(s))")
    for meta in files:
        doc = Document(file_id=meta["id"], name=meta["name"], mime=meta["mimeType"])
        if meta["mimeType"] not in TENDER_DOCS_SUPPORTED_MIMES:
            doc.error = f"unsupported type {meta['mimeType']}"
            logger.warning(
                f"  '{doc.name}': {doc.error}. Convert it to PDF, .docx or a Google "
                f"Doc, or extend tender_docs.extract_text to handle it."
            )
        else:
            try:
                doc.text = extract_text(meta).strip()
                if not doc.text:
                    # A scanned PDF with no text layer reaches here. Saying so is
                    # what stops it being mistaken for a document with nothing in it.
                    doc.error = "no extractable text (a scanned image with no text layer?)"
                    logger.warning(f"  '{doc.name}': {doc.error}")
                else:
                    logger.info(f"  '{doc.name}': {len(doc.text):,} chars")
            except Exception as e:
                doc.error = f"{type(e).__name__}: {e}"
                logger.warning(f"  '{doc.name}': could not be read — {doc.error}")
        result.documents.append(doc)

    result.warnings.extend(resolve_versions(result.documents))
    result.warnings.extend(apply_cap(result.documents))

    unreadable = [d for d in result.documents if d.error]
    if unreadable:
        result.warnings.append(
            f"{len(unreadable)} document(s) could not be read: "
            + ", ".join(f"'{d.name}' ({d.error})" for d in unreadable)
        )

    logger.info(
        f"  tender pack ready: {len(result.used)} of {len(result.documents)} "
        f"document(s), {result.total_chars:,} chars"
    )

    # Only a clean read is cached. A failure is not a property of the document —
    # it can be a missing dependency, an expired token or a transient 5xx — and the
    # fingerprint covers only the files and the settings, so a cached error would be
    # served on every later run until someone happened to edit the pack in Drive.
    # Measured the hard way: a run whose pypdf import failed cached "NOT READ" for a
    # perfectly readable PDF, and kept reporting it after the import worked again.
    # Re-reading a genuinely unreadable document costs one download per run, which
    # is far cheaper than silently dropping it from the evidence base for good.
    if unreadable:
        logger.info(
            f"  not caching this pack — {len(unreadable)} document(s) failed to "
            f"read, and the next run should retry them rather than inherit the "
            f"failure"
        )
    else:
        try:
            _write_cache(result, fingerprint)
        except OSError as e:
            logger.warning(f"  could not cache the tender pack: {e}")

    return result
