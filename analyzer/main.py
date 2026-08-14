"""
Bid Analyzer — main orchestration module.

Flow (per Requirements.md):
  1. Read tenders from the sheet referenced in project_config.json.
  2. For each tender, pass its title + description to the analyzer module and
     record the response in:
       a. [Bid Qualification]                 = Bid / NoBid / TBD
       b. [Bid Qualification Reason(System)]   = system-generated reason
                                                 ([Bid Qualification Reason(Human)] is
                                                  left for manual input)
       c. [Bid Qualification Date]            = date the qualification was arrived at
     Then append a relative comment to [Comments] and maintain the control
     columns [Processed Date], [Last Modified Date], [Created Date].

Run with:  python -m analyzer.main            (analyse all tenders)
           python -m analyzer.main --limit 5  (analyse only the first 5 — handy for testing)
"""
import argparse
import logging
import sys
import traceback
from datetime import datetime

from .config import (
    LOG_FILE,
    UK_TIMEZONE,
    ENVIRONMENT,
    NOTIFICATIONS,
    SHEET_NAME,
    STATUS_FIELD,
    PROCESS_STATUSES,
    should_analyse,
    qualification_family,
)
from .analyzer import analyze_tender
from .patterns import load_nobid_patterns, load_bid_patterns
from .sheets_client import SheetsClient, status_color

# notifier.py lives at the project root (stdlib-only email transport), importable
# because `python -m analyzer.main` runs with the project root on sys.path.
from notifier import send_alert

logger = logging.getLogger(__name__)


def _configure_logging():
    if hasattr(sys.stdout, "reconfigure"):
        # Avoid UnicodeEncodeError on Windows consoles (default cp1252).
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


def _build_row_update(tender, analysis, run_dt) -> dict:
    """Assemble the field->value map to write back for one analysed tender.

    Preserves an existing Created Date; stamps Processed Date and Last Modified
    Date to this run. The new dated reason is prepended to
    Bid Qualification Reason(System) so the latest sits on top and prior runs'
    reasons are kept below; the same entry is appended to the Comments audit log.
    Bid Qualification Reason(Human) is never written, so manual notes survive.
    """
    now_iso = run_dt.isoformat()
    ts = run_dt.strftime("%Y-%m-%d %H:%M")

    # A failed analysis carries a placeholder score of 0.0 that means "never
    # scored", not "scored zero" — writing "score 0/100" beside the TBD it falls
    # back to would read as a real assessment (and, later, as a threshold bug).
    # Say plainly that no score exists instead.
    score_note = (
        "not scored — analysis failed" if analysis.analysis_failed
        else f"score {analysis.score:.0f}/100"
    )
    entry = (
        f"[{ts}] Bid Qualification: {analysis.bid_qualification} "
        f"({score_note}) | {analysis.bid_qualification_reason}"
    )

    # Prepend the newest system reason; keep prior runs' reasons below it.
    prior_reason = tender.data.get("Bid Qualification Reason(System)", "")
    combined_reason = f"{entry}\n{prior_reason}" if prior_reason else entry

    # Comments stays an append-only log (oldest first).
    prior_comments = tender.data.get("Comments", "")
    combined_comments = f"{prior_comments}\n{entry}" if prior_comments else entry

    created = tender.data.get("Created Date", "") or now_iso

    return {
        "Bid Qualification": analysis.bid_qualification,
        "Bid Qualification Reason(System)": combined_reason,
        "Bid Qualification Date": analysis.bid_qualification_date,
        "Comments": combined_comments,
        "Processed Date": now_iso,
        "Last Modified Date": now_iso,
        "Created Date": created,
    }


def run(limit: int = None) -> dict:
    """Analyse every eligible tender in the sheet and write qualifications back.

    Eligibility is the analyzer's single entry point: a row is in scope when its
    [Bid Qualification] is one of PROCESS_STATUSES (PreQualified / ReCheck) — see
    should_analyse(). Nothing else is processed, and nothing else is logged per
    row. There is no date filter: a row stays in scope until it has been given a
    qualification, so a row missed by a failed or skipped run is simply picked up
    by the next one. Returns a summary dict.
    """
    run_dt = datetime.now(UK_TIMEZONE)

    logger.info("=" * 80)
    logger.info("PS BidAnalyzer — Bid qualification run")
    logger.info(f"Run timestamp: {run_dt.isoformat()}")
    logger.info(f"Scope: rows whose {STATUS_FIELD} is in {sorted(PROCESS_STATUSES)}")
    logger.info("=" * 80)

    client = SheetsClient()
    client.open_sheet()
    tenders = client.read_tenders()

    if limit is not None:
        logger.info(f"--limit applied: analysing at most {limit} qualifying tender(s)")

    summary = {"eligible": 0, "analysed": 0, "Bid": 0, "TBD": 0, "NoBid": 0,
               "skipped": 0, "errors": 0}
    # Link the alert email straight to the PS Tender Tracker tab (uses the tab's
    # numeric gid so it opens on that tab, not just the spreadsheet default).
    summary["sheet_url"] = (
        f"https://docs.google.com/spreadsheets/d/{client.sheet_id}/edit#gid={client.sheet_tab_id}"
    )

    # Distilled decision precedent (built separately by analyzer.maintain_knowledge):
    # NoBid heuristics calibrate a score down, Bid heuristics calibrate it up. Each
    # is loaded once and passed into every analyze_tender() call; either being empty
    # (never built) simply means the analyzer injects that block not at all.
    nobid_patterns = load_nobid_patterns()
    bid_patterns = load_bid_patterns()

    updates = []
    row_color_map = {}   # row number -> background colour for changed rows
    new_status = {}      # row number -> qualification this run assigned

    # Status is the analyzer's entry point, applied once here rather than inside
    # the loop. The Sheets API can't filter by cell value server-side (the read
    # always returns the full range), so the selection happens in Python — but
    # selecting up front means the loop, and every per-row log line it writes,
    # covers only tenders this run is actually responsible for. Ineligible rows
    # (manual overrides, and anything already qualified by a previous run) are
    # dropped here silently and reported as a single count.
    eligible = [
        tender for tender in tenders
        if should_analyse(tender.data.get(STATUS_FIELD, ""))
    ]
    summary["eligible"] = len(eligible)
    logger.info(
        f"{len(eligible)} of {len(tenders)} row(s) are {sorted(PROCESS_STATUSES)}; "
        f"processing those"
    )

    for idx, tender in enumerate(eligible, 1):
        # Stop once we've analysed the requested number of qualifying tenders.
        # --limit caps analysed rows rather than rows read.
        if limit is not None and summary["analysed"] >= limit:
            break

        title = tender.title.strip()
        description = tender.description.strip()
        status = tender.data.get(STATUS_FIELD, "").strip()

        if not title and not description:
            logger.info(f"[{idx}/{len(eligible)}] Row {tender.row}: no title/description — skipping")
            summary["skipped"] += 1
            continue

        logger.info(f"[{idx}/{len(eligible)}] Row {tender.row}: analysing '{title[:70]}' (status: {status})")
        try:
            analysis = analyze_tender(title, description, run_date=run_dt,
                                      nobid_patterns=nobid_patterns,
                                      bid_patterns=bid_patterns)
        except Exception as e:
            logger.error(f"Row {tender.row}: unexpected analyzer error: {e}")
            summary["errors"] += 1
            continue

        updates.append((tender.row, _build_row_update(tender, analysis, run_dt)))
        new_status[tender.row] = analysis.bid_qualification
        summary["analysed"] += 1
        # Count by family so 'Bid(AI)' etc. roll up under Bid/TBD/NoBid.
        family = qualification_family(analysis.bid_qualification) or analysis.bid_qualification
        summary[family] = summary.get(family, 0) + 1
        # Colour the row we're changing, reusing the project's palette.
        color = status_color(analysis.bid_qualification)
        if color:
            row_color_map[tender.row] = color

    if updates:
        client.write_qualifications(updates)
        client.apply_row_colors(row_color_map)

    # Sheet-wide Bid/TBD totals for the email's "Overall Summary" table — every
    # row in the tracker, not just this run's. Counted off the rows already read
    # (no extra API call), with this run's new qualifications substituted in so
    # the totals reflect the sheet as it stands after the write-back.
    overall = {}
    for tender in tenders:
        status = new_status.get(tender.row) or tender.data.get(STATUS_FIELD, "")
        family = qualification_family(status)
        if family:
            overall[family] = overall.get(family, 0) + 1
    summary["overall_Bid"] = overall.get("Bid", 0)
    summary["overall_TBD"] = overall.get("TBD", 0)
    summary["overall_total_rows"] = len(tenders)

    logger.info("=" * 80)
    logger.info("BID ANALYSIS COMPLETE — SUMMARY")
    logger.info("=" * 80)
    logger.info(f"  Eligible : {summary['eligible']} ({'/'.join(sorted(PROCESS_STATUSES))})")
    logger.info(f"  Analysed : {summary['analysed']}")
    logger.info(f"  Bid      : {summary['Bid']}")
    logger.info(f"  TBD      : {summary['TBD']}")
    logger.info(f"  NoBid    : {summary['NoBid']}")
    logger.info(f"  Skipped  : {summary['skipped']} (eligible but no title/description)")
    logger.info(f"  Errors   : {summary['errors']}")
    logger.info("-" * 80)
    logger.info(f"  Sheet-wide Bid : {summary['overall_Bid']}")
    logger.info(f"  Sheet-wide TBD : {summary['overall_TBD']}")
    logger.info(
        f"  Needing attention : {summary['overall_Bid'] + summary['overall_TBD']}"
        f" of {summary['overall_total_rows']} tender(s)"
    )
    logger.info("=" * 80)
    return summary


def _build_report(summary, started_at, finished_at, run_date, environment,
                  error_tb=None):
    """Return (subject, html_body) summarising a completed Bid Analyzer run.

    A fatal ``error_tb`` marks the run FAILURE (red); otherwise a non-zero row
    ``errors`` count marks it COMPLETED WITH ERRORS (amber); a clean run is
    SUCCESS (green).
    """
    s = summary or {}
    if error_tb:
        subject = f"❌ PS BidAnalyzer [{environment}] — {run_date} — FAILURE (run aborted)"
        banner_bg = "#c0392b"
    elif s.get("errors", 0):
        subject = (
            f"⚠️ PS BidAnalyzer [{environment}] — {run_date} — "
            f"COMPLETED WITH ERRORS ({s['errors']} row error(s))"
        )
        banner_bg = "#e67e22"
    else:
        subject = (
            f"✅ PS BidAnalyzer [{environment}] — {run_date} — "
            f"SUCCESS ({s.get('analysed', 0)} analysed)"
        )
        banner_bg = "#27ae60"

    # Eligible leads: it is the run's scope (rows in PreQualified / ReCheck), and
    # Analysed below it shows how much of that scope was actually got through —
    # the two differ when --limit is set or rows are skipped for missing text.
    metric_rows = [
        ("Eligible (PreQualified / ReCheck)", s.get("eligible", 0)),
        ("Analysed", s.get("analysed", 0)),
        ("Bid", s.get("Bid", 0)),
        ("TBD", s.get("TBD", 0)),
        ("NoBid", s.get("NoBid", 0)),
        ("Skipped (no title / description)", s.get("skipped", 0)),
        ("Errors", s.get("errors", 0)),
    ]
    rows = "".join(
        f"<tr><td>{label}</td><td style='text-align:right'>{value}</td></tr>"
        for label, value in metric_rows
    )

    # At-a-glance headline: every Bid/TBD tender in the tracker as it stands
    # after this run (see run()), not just the rows this run touched — those are
    # in the "This Run" table below. Suppressed on a fatal error, where run()
    # never produced the totals.
    bid_count = s.get("overall_Bid", 0)
    tbd_count = s.get("overall_TBD", 0)
    total_rows = s.get("overall_total_rows", 0)
    qualification_table = f"""\
  <h3 style="margin:16px 0 4px;font-size:15px">Overall Summary</h3>
  <table cellpadding="8" cellspacing="0" border="1"
         style="border-collapse:collapse;border-color:#ddd;font-size:14px">
    <tr style="background:#f0f0f0"><th align="left">Qualification</th><th>Total Tenders</th></tr>
    <tr><td>Bid</td><td style="text-align:right">{bid_count}</td></tr>
    <tr><td>TBD</td><td style="text-align:right">{tbd_count}</td></tr>
    <tr style="background:#f9f9f9"><td><b>Tenders Needing your Attention</b></td>
        <td style="text-align:right"><b>{bid_count + tbd_count}</b></td></tr>
  </table>
  <p style="color:#888;font-size:12px;margin:4px 0 0">
     Across all {total_rows} tenders in the tracker.</p>""" if total_rows else ""

    sheet_url = s.get("sheet_url")
    sheet_link = (
        f'<p><b>Sheet:</b> <a href="{sheet_url}">{SHEET_NAME}</a></p>'
        if sheet_url else ""
    )

    details = ""
    if error_tb:
        details = (
            "<h3 style='margin:16px 0 4px'>Traceback</h3>"
            "<pre style='background:#f4f4f4;padding:12px;border-radius:4px;"
            f"overflow-x:auto;font-size:12px'>{error_tb}</pre>"
        )
    elif summary is None:
        details = "<p>No run summary was produced.</p>"

    html = f"""\
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222">
  <div style="background:{banner_bg};color:#fff;padding:14px 18px;border-radius:6px;
              font-size:18px;font-weight:bold">{subject}</div>
  <p><b>Environment:</b> {environment}<br>
     <b>Started:</b> {started_at}<br>
     <b>Finished:</b> {finished_at}<br>
     <b>Scope:</b> every row awaiting qualification (PreQualified / ReCheck)</p>
{qualification_table}
  {sheet_link}
  <h3 style="margin:16px 0 4px;font-size:15px">Bid Analysis - This Run</h3>
  <table cellpadding="8" cellspacing="0" border="1"
         style="border-collapse:collapse;border-color:#ddd;font-size:14px">
    <tr style="background:#f0f0f0"><th align="left">Metric</th><th>Count</th></tr>
    {rows}
  </table>
  {details}
  <p style="color:#888;font-size:12px;margin-top:20px">
     Automated message from the PS BidAnalyzer.</p>
</body></html>"""
    return subject, html


def main():
    _configure_logging()
    parser = argparse.ArgumentParser(description="Run the Onepoint Bid Analyzer over the tender sheet.")
    parser.add_argument("--limit", type=int, default=None, help="Analyse only the first N tenders (for testing).")
    # --date selected a one-day window that no longer exists: scope is now every
    # PreQualified/ReCheck row. The flag is still ACCEPTED so an existing
    # scheduled command keeps starting instead of dying on an unknown argument;
    # it does nothing but warn. Delete it once the scheduler entry is confirmed
    # clean.
    parser.add_argument(
        "--date",
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.date:
        logger.warning(
            f"--date {args.date} ignored: the analyzer no longer filters by date and "
            f"processes every PreQualified/ReCheck row. Remove it from the command."
        )

    # Stamp the run date for the alert up front so it is reported even if the run
    # aborts before producing a summary.
    run_date = datetime.now(UK_TIMEZONE).strftime("%Y-%m-%d")
    started_at = datetime.now(UK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    summary = None
    error_tb = None
    try:
        summary = run(limit=args.limit)
    except Exception as e:
        error_tb = traceback.format_exc()
        logger.error(f"Fatal error: {e}", exc_info=True)

    finished_at = datetime.now(UK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    subject, html = _build_report(
        summary, started_at, finished_at, run_date, ENVIRONMENT, error_tb
    )
    send_alert(subject, html, NOTIFICATIONS)

    # Preserve non-zero exit on failure so schedulers still register the run as
    # failed, in addition to the email alert.
    if error_tb:
        sys.exit(1)


if __name__ == "__main__":
    main()
