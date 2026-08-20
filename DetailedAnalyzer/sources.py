"""
Source corpus ingestion for the detailed analysis stage.

Reads Onepoint's own evidence — the capability matrix, the supplier readiness
questionnaire, past performance — out of the Drive folder in
``config.SOURCES_FOLDER_ID``, normalises it, and renders one markdown corpus that
every analysis run injects into its prompt.

Why this exists rather than reading the NotebookLM links in Requirements.md:
those are consumer notebooks behind a Google sign-in wall and cannot be read by
any service account (the official API is Gemini Notebook Enterprise only). A
notebook is only a wrapper over source documents, so this ingests the documents.

Two separable jobs, deliberately kept apart:

  build_corpus()  hits Drive + Sheets, normalises, renders, writes the cache.
                  Run on its own cadence — the corpus changes when someone edits
                  a source sheet, not when a tender arrives.
  load_corpus()   reads the cache. What an analysis run calls. No API calls, no
                  network, and the exact text sent to the model stays on disk to
                  be audited afterwards.

Normalisation is not cosmetic. The sources are hand-maintained working documents
containing a dummy example row, a duplicated tab, unfilled money columns and ~175
provisional cells; every filter here corresponds to something verified present in
the data. See the ingestion filter block in config.py for what each one is for.

Run:  python -m DetailedAnalyzer.sources            (build/refresh the corpus)
      python -m DetailedAnalyzer.sources --dry-run  (render to stdout, write nothing)
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sys

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from .config import (
    SCOPES,
    SERVICE_ACCOUNT_FILE,
    SOURCES_FOLDER_ID,
    CORPUS_FILE,
    KNOWLEDGE_DIR,
    LOG_FILE,
    UK_TIMEZONE,
    PII_REDACTION,
    PII_COLUMN_MARKERS,
    PII_LABEL_MARKERS,
    PII_EMAIL_PATTERN,
    PII_PHONE_PATTERN,
    PII_NAME_MARKERS,
    SENSITIVE_ROW_MARKERS,
    RESPONSE_COLUMN_MARKERS,
    EXAMPLE_ROW_MARKERS,
    PLACEHOLDER_VALUES,
    NOT_PROVIDED_RENDER,
    UNCONFIRMED_SUFFIX,
    MONEY_COLUMN_MARKERS,
    MONEY_ZERO_VALUES,
    DEDUPE_IDENTICAL_TABS,
    SKIP_TABS,
)

logger = logging.getLogger(__name__)

SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"

_EMAIL_RE = re.compile(PII_EMAIL_PATTERN)
_PHONE_RE = re.compile(PII_PHONE_PATTERN)

# Counters for one ingestion run, reported at the end so the filtering is visible
# rather than silent. A filter that starts dropping everything should be obvious
# from the log, not discovered later in a bad score.
_STATS_KEYS = (
    "files", "tabs", "tabs_skipped", "tabs_deduped", "rows_in", "rows_out",
    "rows_example_dropped", "cols_pii_dropped", "rows_pii_dropped",
    "cells_pii_scrubbed", "rows_sensitive_withheld", "cells_blank_omitted",
    "cells_unconfirmed", "cells_money_zeroed",
)


def _new_stats() -> dict:
    return {k: 0 for k in _STATS_KEYS}


# --- Drive / Sheets access --------------------------------------------------
def _services():
    """Return (drive, sheets) clients on the shared service account."""
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return (build("drive", "v3", credentials=creds),
            build("sheets", "v4", credentials=creds))


def list_source_files(drive) -> list:
    """List the Google Sheets in the sources folder, oldest name order.

    Only native spreadsheets are returned. A binary upload (.xlsx, .pdf) is
    reported and skipped rather than silently ignored — it means someone added a
    source this layer cannot read yet, which is worth knowing about.
    """
    files, page = [], None
    while True:
        res = drive.files().list(
            q=f"'{SOURCES_FOLDER_ID}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType,modifiedTime)",
            pageSize=200,
            pageToken=page,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            orderBy="name",
        ).execute()
        files.extend(res.get("files", []))
        page = res.get("nextPageToken")
        if not page:
            break

    sheets_files = [f for f in files if f["mimeType"] == SPREADSHEET_MIME]
    for f in files:
        if f["mimeType"] != SPREADSHEET_MIME:
            logger.warning(
                f"Skipping '{f['name']}' ({f['mimeType']}): this layer reads Google "
                f"Sheets only. Convert it in Drive (File > Save as Google Sheets), "
                f"or extend sources.py to handle the format."
            )
    if not sheets_files:
        logger.warning(
            f"No Google Sheets found in folder {SOURCES_FOLDER_ID}. The corpus "
            f"will be empty and analysis will fall back to the capability context "
            f"alone."
        )
    return sheets_files


def read_spreadsheet(sheets, file_id: str) -> list:
    """Return [(tab_title, rows)] for every tab, in one batched read."""
    meta = sheets.spreadsheets().get(
        spreadsheetId=file_id, fields="sheets.properties.title"
    ).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if not titles:
        return []
    batch = sheets.spreadsheets().values().batchGet(
        spreadsheetId=file_id,
        ranges=[f"'{t}'" for t in titles],
        majorDimension="ROWS",
    ).execute()
    out = []
    for title, vr in zip(titles, batch.get("valueRanges", [])):
        out.append((title, vr.get("values", [])))
    return out


# --- Normalisation ----------------------------------------------------------
def _clean(cell) -> str:
    """Collapse a raw cell to a single-line trimmed string."""
    return " ".join((cell or "").split())


def _trim_rows(rows: list) -> list:
    """Drop trailing all-blank rows, and pad nothing."""
    out = [[_clean(c) for c in row] for row in rows]
    while out and not any(out[-1]):
        out.pop()
    return out


def _scrub_pii(text: str, stats: dict) -> str:
    """Redact email addresses, phone numbers and named individuals in free text."""
    scrubbed, n_mail = _EMAIL_RE.subn(PII_REDACTION, text)
    scrubbed, n_tel = _PHONE_RE.subn(PII_REDACTION, scrubbed)
    n_name = 0
    for name in PII_NAME_MARKERS:
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        scrubbed, hits = pattern.subn(PII_REDACTION, scrubbed)
        n_name += hits
    stats["cells_pii_scrubbed"] += (1 if (n_mail or n_tel or n_name) else 0)
    return scrubbed


def _is_example_row(cells: list) -> bool:
    """True for the sheets' filled-in dummy example rows."""
    joined = " ".join(cells).lower()
    return any(marker in joined for marker in EXAMPLE_ROW_MARKERS)


def _is_sensitive_row(cells: list) -> bool:
    """True when a row's question is a personal-data disclosure (PSC, DOB…)."""
    joined = " ".join(cells).lower()
    return any(marker in joined for marker in SENSITIVE_ROW_MARKERS)


def _is_response_column(name: str) -> bool:
    """True for the column holding the answer that a sensitive row withholds."""
    low = (name or "").lower()
    return any(m in low for m in RESPONSE_COLUMN_MARKERS)


def _find_header_row(rows: list) -> int:
    """Index of the row that looks like column headers, or -1 if there is none.

    Heuristic, because the tabs disagree: some open with a title row, some repeat
    their headers mid-tab, and some (the label:value tabs) have no header at all.
    A row qualifies when it has at least three labels and the rows below it
    actually populate those same columns — which is what separates a real header
    from a one-cell title or a section banner. Returning -1 is a normal outcome,
    not a failure: the caller falls back to a layout that needs no headers.
    """
    for i, row in enumerate(rows[:8]):
        labels = [j for j, c in enumerate(row) if c]
        if len(labels) < 3:
            continue
        below = rows[i + 1:i + 8]
        if not below:
            continue
        filled = sum(
            1 for r in below
            if sum(1 for j in labels if j < len(r) and r[j]) >= 2
        )
        if filled >= max(1, len(below) // 2):
            return i
    return -1


def _column_filter(header: list, stats: dict) -> list:
    """Indices of columns to keep — drops the referee/contact columns."""
    keep = []
    for j, name in enumerate(header):
        low = name.lower()
        if name and any(m in low for m in PII_COLUMN_MARKERS):
            stats["cols_pii_dropped"] += 1
            continue
        keep.append(j)
    return keep


def _render_value(value: str, column: str, stats: dict) -> str:
    """Normalise one value for output, preserving how settled it is.

    Provisional data is relabelled rather than dropped or tidied: an unanswered
    question must not read as a clean absence, and 'Yes?' must not become 'Yes'.
    """
    low = value.lower()
    if not value or low in PLACEHOLDER_VALUES:
        stats["cells_blank_omitted"] += 1
        return NOT_PROVIDED_RENDER

    col_low = (column or "").lower()
    if any(m in col_low for m in MONEY_COLUMN_MARKERS):
        if value.replace(",", "") in MONEY_ZERO_VALUES:
            stats["cells_money_zeroed"] += 1
            return NOT_PROVIDED_RENDER

    # Trailing "?" means the author was unsure. Keep the value, keep the doubt.
    if value.endswith("?") and len(value) < 40:
        stats["cells_unconfirmed"] += 1
        return value.rstrip("?").strip() + UNCONFIRMED_SUFFIX

    return value


def render_tab(title: str, rows: list, stats: dict) -> str:
    """Render one tab to markdown, applying every ingestion filter.

    Table-shaped tabs become one labelled block per row rather than a markdown
    table. The data is sparse — most cells are empty — so a table would spend the
    bulk of its tokens on empty pipes, and an 18-column table wraps into
    unreadability. A labelled block skips blanks entirely and keeps each value
    next to the column it belongs to.
    """
    rows = _trim_rows(rows)
    stats["rows_in"] += len(rows)
    if not rows:
        return ""

    out = [f"### {title.strip()}", ""]
    header_idx = _find_header_row(rows)

    if header_idx < 0:
        # No header row: a label:value or prose tab (Part 5, Read Me, Part 3's
        # section-and-answer layout). Render line by line, dropping the rows whose
        # label marks them as contact details.
        for row in rows:
            cells = [c for c in row if c]
            if not cells:
                continue
            if _is_example_row(cells):
                stats["rows_example_dropped"] += 1
                continue
            label = cells[0].rstrip(":").lower()
            if any(m in label for m in PII_LABEL_MARKERS):
                stats["rows_pii_dropped"] += 1
                out.append(f"- **{cells[0].rstrip(':')}:** {PII_REDACTION}")
                stats["rows_out"] += 1
                continue
            # A personal-data disclosure: keep the question, withhold the answer.
            if _is_sensitive_row(cells):
                stats["rows_sensitive_withheld"] += 1
                out.append(f"- **{cells[0].rstrip(':')}:** {PII_REDACTION}")
                stats["rows_out"] += 1
                continue
            cells = [_scrub_pii(c, stats) for c in cells]
            if len(cells) == 1:
                out.append(f"{cells[0]}")
            else:
                out.append(f"- **{cells[0].rstrip(':')}:** " + " | ".join(cells[1:]))
            stats["rows_out"] += 1
        out.append("")
        return "\n".join(out)

    header = rows[header_idx]
    keep = _column_filter(header, stats)
    names = [header[j] if j < len(header) else "" for j in keep]

    for row in rows[header_idx + 1:]:
        cells = [c for c in row if c]
        if not cells:
            continue
        if _is_example_row(cells):
            stats["rows_example_dropped"] += 1
            continue

        # A row that repeats the header (these tabs restate headers mid-tab) or
        # that holds a single cell is a section banner, not a record.
        if row == header:
            continue
        if len(cells) == 1:
            out.append(f"**{_scrub_pii(cells[0], stats)}**")
            stats["rows_out"] += 1
            continue

        # A personal-data disclosure question: keep the question so the corpus
        # still evidences that Onepoint answered it, withhold the answer itself.
        sensitive = _is_sensitive_row(cells)
        if sensitive:
            stats["rows_sensitive_withheld"] += 1

        parts = []
        for j, name in zip(keep, names):
            raw = row[j] if j < len(row) else ""
            if not raw:
                continue                      # skip blanks entirely
            if sensitive and _is_response_column(name):
                parts.append(f"{name}: {PII_REDACTION}")
                continue
            value = _render_value(_scrub_pii(raw, stats), name, stats)
            if value == NOT_PROVIDED_RENDER:
                continue                      # nothing to say about an empty cell
            parts.append(f"{name or 'value'}: {value}" if name else value)
        if not parts:
            continue
        # Safety net: on a sensitive row where no column was recognised as the
        # answer, withhold the whole row rather than risk emitting the disclosure.
        # Failing closed is the only safe direction for personal data.
        if sensitive and not any(PII_REDACTION in p for p in parts):
            out.append(f"- {parts[0]}; {PII_REDACTION}")
            stats["rows_out"] += 1
            continue
        out.append("- " + "; ".join(parts))
        stats["rows_out"] += 1

    out.append("")
    return "\n".join(out)


def build_corpus(dry_run: bool = False) -> str:
    """Ingest every source sheet and return the rendered corpus markdown.

    Writes it to ``CORPUS_FILE`` unless ``dry_run``. Tabs whose content is
    byte-identical to one already ingested are skipped, so a sheet duplicated
    across two files is not counted as two pieces of evidence.
    """
    from datetime import datetime

    stats = _new_stats()
    drive, sheets = _services()
    files = list_source_files(drive)
    stats["files"] = len(files)

    seen_hashes = {}
    chunks = [
        "# Onepoint source corpus",
        "",
        f"Ingested {datetime.now(UK_TIMEZONE).strftime('%Y-%m-%d %H:%M %Z')} from "
        f"Drive folder `{SOURCES_FOLDER_ID}`.",
        "",
        "Personal contact details (named referees, direct phone numbers, personal "
        f"email addresses) are withheld as `{PII_REDACTION}` — they carry no "
        "capability signal. Values shown as "
        f"`{NOT_PROVIDED_RENDER}` were blank or unfilled in the source; values "
        f"marked `{UNCONFIRMED_SUFFIX.strip()}` were flagged uncertain by their "
        "author and must not be treated as established fact.",
        "",
    ]

    for f in files:
        logger.info(f"Ingesting '{f['name']}' (modified {f.get('modifiedTime')})")
        chunks.append(f"## {f['name']}")
        chunks.append("")
        for title, rows in read_spreadsheet(sheets, f["id"]):
            stats["tabs"] += 1
            if title.strip().lower() in SKIP_TABS:
                logger.info(f"  tab '{title}': skipped (SKIP_TABS)")
                stats["tabs_skipped"] += 1
                continue

            digest = hashlib.sha256(
                json.dumps(rows, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if DEDUPE_IDENTICAL_TABS and digest in seen_hashes:
                logger.info(
                    f"  tab '{title}': skipped — byte-identical to "
                    f"'{seen_hashes[digest]}' already ingested"
                )
                stats["tabs_deduped"] += 1
                continue
            seen_hashes[digest] = f"{f['name']}::{title}"

            rendered = render_tab(title, rows, stats)
            if rendered.strip():
                chunks.append(rendered)
                logger.info(f"  tab '{title}': {len(rows)} row(s) ingested")
            else:
                logger.info(f"  tab '{title}': empty after normalisation")

    corpus = "\n".join(chunks).rstrip() + "\n"

    logger.info("-" * 70)
    logger.info("INGESTION SUMMARY")
    logger.info(f"  Files                 : {stats['files']}")
    logger.info(f"  Tabs read             : {stats['tabs']}")
    logger.info(f"  Tabs skipped          : {stats['tabs_skipped']} (no evidential content)")
    logger.info(f"  Tabs deduped          : {stats['tabs_deduped']} (identical to an earlier tab)")
    logger.info(f"  Rows in / out         : {stats['rows_in']} / {stats['rows_out']}")
    logger.info(f"  Example rows dropped  : {stats['rows_example_dropped']}")
    logger.info(f"  PII columns dropped   : {stats['cols_pii_dropped']}")
    logger.info(f"  PII rows withheld     : {stats['rows_pii_dropped']} (contact-detail rows)")
    logger.info(f"  Sensitive answers held: {stats['rows_sensitive_withheld']} (PSC/DOB/address disclosures)")
    logger.info(f"  Cells PII-scrubbed    : {stats['cells_pii_scrubbed']} (names/emails/phones in free text)")
    logger.info(f"  Blank cells omitted   : {stats['cells_blank_omitted']} (not rendered at all)")
    logger.info(f"  Cells unconfirmed     : {stats['cells_unconfirmed']}")
    logger.info(f"  Money zeros suppressed: {stats['cells_money_zeroed']}")
    logger.info(f"  Corpus size           : {len(corpus):,} chars")
    logger.info("-" * 70)

    if dry_run:
        logger.info("--dry-run: corpus NOT written")
        return corpus

    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    with open(CORPUS_FILE, "w", encoding="utf-8") as fh:
        fh.write(corpus)
    logger.info(f"Corpus written to {CORPUS_FILE}")
    return corpus


# --- Read path (what an analysis run uses) ----------------------------------
_corpus_cache = None


def load_corpus() -> str:
    """Return the cached corpus as a string (cached in-process).

    Returns an empty string if the corpus has never been built, so a run degrades
    to the capability context alone rather than crashing. Never hits the network —
    building is an explicit, separate step (see build_corpus).
    """
    global _corpus_cache
    if _corpus_cache is not None:
        return _corpus_cache

    if not os.path.exists(CORPUS_FILE):
        logger.warning(
            f"No source corpus at {CORPUS_FILE}; detailed analysis will run on the "
            f"capability context alone. Build it with "
            f"'python -m DetailedAnalyzer.sources'."
        )
        _corpus_cache = ""
        return _corpus_cache

    with open(CORPUS_FILE, encoding="utf-8") as fh:
        content = fh.read().strip()

    _corpus_cache = content
    logger.info(f"Loaded source corpus ({len(content)} chars) from {CORPUS_FILE}")
    return _corpus_cache


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        force=True,
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    parser = argparse.ArgumentParser(
        description="Ingest Onepoint's Drive source documents into the analysis corpus."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Render to stdout without writing the corpus file.",
    )
    args = parser.parse_args()

    corpus = build_corpus(dry_run=args.dry_run)
    if args.dry_run:
        print("\n" + "=" * 70 + "\n")
        print(corpus)


if __name__ == "__main__":
    main()
