"""
DetailedAnalyzer — main orchestration module.

Flow (mirrors analyzer/main.py):
  1. Read tenders from the sheet referenced in project_config.json.
  2. Select the rows in scope — [Bid Qualification] in PROCESS_STATUSES, minus any
     already carrying a completed detailed analysis. Applied once before the loop,
     so out-of-scope rows never reach the model or the per-row log.
  3. Run each through analyse_tender_detail() and assemble the write-back.
  4. Write back — only if config.WRITE_BACK_ENABLED; otherwise log what would
     have been written and touch nothing.
  5. Send one HTML summary email, success or failure.

Run with:  python -m DetailedAnalyzer.main             (all in-scope rows)
           python -m DetailedAnalyzer.main --limit 3   (first 3 — quick test)
"""
import argparse
import logging
import sys
import traceback
from datetime import datetime
from html import escape as html_escape

from .config import (
    LOG_FILE,
    UK_TIMEZONE,
    ENVIRONMENT,
    NOTIFICATIONS,
    SHEET_NAME,
    STATUS_FIELD,
    PROCESS_STATUSES,
    OUTPUT_FIELD_MAP,
    WRITE_BACK_ENABLED,
    COMPLETED_STATUS,
    MARK_COMPLETE,
    MARK_COMPLETE_REQUIRES_REPORT,
    SYSTEM_REASON_FIELD,
    LINK_FIELDS,
    REPORTS_ENABLED,
    REPORTS_FOLDER_ID,
    EMAIL_LINK_REPORTS,
    should_analyse,
    already_detailed,
)
from .detailed_analyzer import analyse_tender_detail
from .report_writer import ReportWriter, render_markdown, report_name
from .sheets_client import SheetsClient

# notifier.py lives at the project root (stdlib-only email transport), importable
# because `python -m DetailedAnalyzer.main` runs with the project root on sys.path.
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


def _build_row_update(tender, brief, run_dt, report_url="", mark_done=False) -> dict:
    """Assemble the field->value map to write back for one analysed tender.

    Output goes to whatever columns OUTPUT_FIELD_MAP names (keys are TenderBrief
    attributes, values are sheet columns); an empty map means only the status and
    the audit trail below are written.

    When ``mark_done`` the row's status moves to COMPLETED_STATUS, taking it out
    of scope for future runs. The brief's likelihood band implies a qualification
    (brief.qualification_family) but is deliberately NOT written as the status:
    the likelihood is an assessment of this tender, recorded in the report, while
    the status column tracks where the row is in the workflow. Overwriting a
    workflow state with an assessment would lose the fact that the analysis ran at
    all. Neither Bid Qualification Reason column is touched, so the analyzer's
    system reason and the team's manual notes both survive.
    """
    now_iso = run_dt.isoformat()
    ts = run_dt.strftime("%Y-%m-%d %H:%M")

    update = {}
    for attr, column in OUTPUT_FIELD_MAP.items():
        value = report_url if attr == "report_url" else getattr(brief, attr, "")
        update[column] = value

    if mark_done:
        update[STATUS_FIELD] = COMPLETED_STATUS

    entry = (
        f"[{ts}] Detailed analysis: {brief.likelihood_summary}"
        f"{' | Report: ' + report_url if report_url else ''}"
        f"{f' | {STATUS_FIELD} set to {COMPLETED_STATUS}' if mark_done else ''}"
    )

    # The system reason column is prepended to, newest first — the analyzer's
    # convention, so the two stages' entries interleave into one readable history
    # of every automated judgement on this row rather than two rival logs.
    prior_reason = tender.data.get(SYSTEM_REASON_FIELD, "")
    update[SYSTEM_REASON_FIELD] = f"{entry}\n{prior_reason}" if prior_reason else entry

    # Comments stays append-only (oldest first), also matching the analyzer, so
    # one column reads as the chronological history of everything that has
    # happened to the row.
    prior_comments = tender.data.get("Comments", "")
    update["Comments"] = f"{prior_comments}\n{entry}" if prior_comments else entry

    update["Processed Date"] = now_iso
    update["Last Modified Date"] = now_iso
    return update


def run(limit: int = None, dry_run: bool = False) -> dict:
    """Run detailed analysis over every in-scope tender. Returns a summary dict.

    ``dry_run`` analyses and renders each brief to the log without creating a
    report in Drive or touching the tracker — the way to review the brief's
    content while the prompt is still being tuned.
    """
    run_dt = datetime.now(UK_TIMEZONE)

    logger.info("=" * 80)
    logger.info("PS BidAnalyzer — Detailed analysis run")
    logger.info(f"Run timestamp: {run_dt.isoformat()}")
    logger.info(f"Scope: rows whose {STATUS_FIELD} is in {sorted(PROCESS_STATUSES)}")
    if not WRITE_BACK_ENABLED:
        logger.warning(
            "WRITE_BACK_ENABLED is False — this run will analyse and report but "
            "will NOT write to the sheet. Set it in DetailedAnalyzer/config.py "
            "once OUTPUT_FIELD_MAP is filled in."
        )
    logger.info("=" * 80)

    client = SheetsClient()
    client.open_sheet()
    tenders = client.read_tenders()

    if limit is not None:
        logger.info(f"--limit applied: analysing at most {limit} qualifying tender(s)")

    summary = {"eligible": 0, "analysed": 0, "skipped": 0, "errors": 0,
               "written": 0, "reports": 0, "report_errors": 0,
               "Bid": 0, "TBD": 0, "NoBid": 0, "marked_done": 0, "report_refs": [],
               "write_back_enabled": WRITE_BACK_ENABLED and not dry_run,
               "reports_enabled": REPORTS_ENABLED and not dry_run,
               "dry_run": dry_run}
    summary["sheet_url"] = (
        f"https://docs.google.com/spreadsheets/d/{client.sheet_id}"
        f"/edit#gid={client.sheet_tab_id}"
    )

    # Selection happens here, once, rather than inside the loop: the Sheets API
    # cannot filter by cell value server-side, so it is done in Python — but doing
    # it up front means the loop and its per-row logging cover only the rows this
    # run is actually responsible for.
    eligible = [
        t for t in tenders
        if should_analyse(t.data.get(STATUS_FIELD, "")) and not already_detailed(t.data)
    ]
    summary["eligible"] = len(eligible)
    logger.info(
        f"{len(eligible)} of {len(tenders)} row(s) are in scope "
        f"({sorted(PROCESS_STATUSES)}); processing those"
    )

    # One writer for the whole run — it authenticates on construction, so building
    # it per row would re-auth for every tender. Built only when it will be used.
    writer = None
    if REPORTS_ENABLED and not dry_run and eligible:
        writer = ReportWriter()
        logger.info(f"Reports will be written to Drive folder {REPORTS_FOLDER_ID}")

    updates = []
    for idx, tender in enumerate(eligible, 1):
        # --limit caps analysed rows, not rows read.
        if limit is not None and summary["analysed"] >= limit:
            break

        title = tender.title.strip()
        description = tender.description.strip()

        if not title and not description:
            logger.info(
                f"[{idx}/{len(eligible)}] Row {tender.row}: no title/description — skipping"
            )
            summary["skipped"] += 1
            continue

        logger.info(
            f"[{idx}/{len(eligible)}] Row {tender.row}: detailed analysis of '{title[:70]}'"
        )
        try:
            brief = analyse_tender_detail(tender.data, run_date=run_dt)
        except Exception as e:
            logger.error(f"Row {tender.row}: unexpected error: {e}")
            summary["errors"] += 1
            continue

        # A flagged result is a failure, not an assessment — count it, and write
        # neither a report nor a row update. A brief that reads as complete but
        # was never scored is worse than no brief at all.
        if brief.analysis_failed:
            logger.error(f"Row {tender.row}: {brief.recommendation}")
            summary["errors"] += 1
            continue

        summary["analysed"] += 1
        family = brief.qualification_family
        summary[family] = summary.get(family, 0) + 1
        logger.info(
            f"  → {brief.likelihood_summary}, implies {family}; "
            f"{len(brief.fit_dimensions)} fit dimension(s)"
        )

        report_url = ""
        if dry_run:
            logger.info("\n" + render_markdown(brief, report_name(tender.data, run_dt)))
        elif REPORTS_ENABLED:
            try:
                name, file_id, report_url = writer.write(brief, tender.data, run_dt)
                summary["reports"] += 1
                # Kept for the email — its attachments and its list of links.
                summary["report_refs"].append({
                    "name": name, "file_id": file_id, "url": report_url,
                    "title": title, "likelihood": brief.likelihood_summary,
                })
            except Exception as e:
                # The analysis succeeded; only the report failed. Count it
                # separately so a Drive permission problem is not mistaken for a
                # bad analysis, and carry on with the rest of the run.
                logger.error(f"Row {tender.row}: report write failed: {e}")
                summary["report_errors"] += 1

        # Mark the row done only when there is something to show for it. A report
        # failure (Drive permissions, a transient 5xx) leaves the status alone so
        # the next run retries the tender, rather than taking it out of scope with
        # no brief anywhere.
        mark_done = MARK_COMPLETE and not dry_run
        if mark_done and MARK_COMPLETE_REQUIRES_REPORT and REPORTS_ENABLED and not report_url:
            mark_done = False
            logger.warning(
                f"Row {tender.row}: not marking {COMPLETED_STATUS} — no report was "
                f"written, so the row stays in scope for the next run"
            )
        if mark_done:
            summary["marked_done"] += 1

        updates.append((
            tender.row,
            _build_row_update(tender, brief, run_dt, report_url, mark_done),
        ))

    if updates and WRITE_BACK_ENABLED and not dry_run:
        summary["written"] = client.write_updates(updates, link_fields=LINK_FIELDS)
    elif updates:
        logger.info(
            f"Write-back disabled: {len(updates)} row(s) would have been updated "
            f"(rows {[r for r, _ in updates]})"
        )
        if not OUTPUT_FIELD_MAP:
            logger.warning(
                "OUTPUT_FIELD_MAP is empty, so even with write-back enabled only "
                "the Comments/control columns would be written. Add the detailed "
                "analysis column(s) to the tracker by hand, then map them."
            )

    logger.info("=" * 80)
    logger.info("DETAILED ANALYSIS COMPLETE — SUMMARY")
    logger.info("=" * 80)
    logger.info(f"  Eligible      : {summary['eligible']}")
    logger.info(f"  Analysed      : {summary['analysed']}")
    logger.info(f"  Bid / TBD / NoBid (implied) : "
                f"{summary['Bid']} / {summary['TBD']} / {summary['NoBid']}")
    logger.info(f"  Reports built : {summary['reports']}"
                f"{'' if summary['reports_enabled'] else ' (reports disabled)'}")
    logger.info(f"  Report errors : {summary['report_errors']}")
    logger.info(f"  Skipped       : {summary['skipped']} (eligible but no title/description)")
    logger.info(f"  Errors        : {summary['errors']}")
    logger.info(f"  Rows written  : {summary['written']}"
                f"{'' if summary['write_back_enabled'] else ' (write-back disabled)'}")
    logger.info(f"  Marked {COMPLETED_STATUS:<7}: {summary['marked_done']}"
                f" (out of scope for future runs)")
    logger.info("=" * 80)
    return summary


def _reports_table(summary) -> str:
    """HTML table of this run's reports — tender, likelihood, and a direct link.

    This is how a report is reached: one click from the mailbox to the exact brief
    for that tender. Without it a report is only findable by browsing the Drive
    folder, which is the difference between a deliverable and a file that exists.

    The likelihood sits beside each link so the table is scannable on its own —
    the reader can see which briefs are worth opening before opening any.
    """
    refs = summary.get("report_refs") or []
    if not (EMAIL_LINK_REPORTS and refs):
        return ""
    rows = "".join(
        f"<tr><td><a href=\"{r['url']}\">{html_escape(r['title'][:90]) or r['name']}</a>"
        f"<br><span style='color:#999;font-size:11px'>{html_escape(r['name'])}</span></td>"
        f"<td style='text-align:right;white-space:nowrap'><b>{r['likelihood']}</b></td></tr>"
        for r in refs
    )
    return f"""\
  <h3 style="margin:16px 0 4px;font-size:15px">Reports produced ({len(refs)})</h3>
  <table cellpadding="8" cellspacing="0" border="1"
         style="border-collapse:collapse;border-color:#ddd;font-size:14px">
    <tr style="background:#f0f0f0"><th align="left">Tender — click to open its report</th>
        <th>Likelihood of Winning</th></tr>
    {rows}
  </table>
  <p style="color:#888;font-size:12px;margin:4px 0 0">
     Each link opens that tender's brief in Drive. The Drive copy is the record —
     it is not attached, so there is only ever one version of an assessment.</p>"""


def _build_report(summary, started_at, finished_at, run_date, environment,
                  error_tb=None):
    """Return (subject, html_body) summarising a completed detailed analysis run.

    Same three-state convention as the analyzer's alert: a fatal ``error_tb`` is
    FAILURE (red), a non-zero row ``errors`` count is COMPLETED WITH ERRORS
    (amber), a clean run is SUCCESS (green).
    """
    s = summary or {}
    if error_tb:
        subject = f"❌ PS DetailedAnalyzer [{environment}] — {run_date} — FAILURE (run aborted)"
        banner_bg = "#c0392b"
    elif s.get("errors", 0) or s.get("report_errors", 0):
        n = s.get("errors", 0) + s.get("report_errors", 0)
        subject = (
            f"⚠️ PS DetailedAnalyzer [{environment}] — {run_date} — "
            f"COMPLETED WITH ERRORS ({n} error(s))"
        )
        banner_bg = "#e67e22"
    else:
        subject = (
            f"✅ PS DetailedAnalyzer [{environment}] — {run_date} — "
            f"SUCCESS ({s.get('analysed', 0)} analysed)"
        )
        banner_bg = "#27ae60"

    metric_rows = [
        ("Eligible (qualified as Bid)", s.get("eligible", 0)),
        ("Analysed", s.get("analysed", 0)),
        ("Likelihood HIGH/VERY HIGH (implies Bid)", s.get("Bid", 0)),
        ("Likelihood MEDIUM (implies TBD)", s.get("TBD", 0)),
        ("Likelihood LOW (implies NoBid)", s.get("NoBid", 0)),
        ("Reports built", s.get("reports", 0)),
        ("Report errors", s.get("report_errors", 0)),
        (f"Marked {COMPLETED_STATUS} (now out of scope)", s.get("marked_done", 0)),
        ("Skipped (no title / description)", s.get("skipped", 0)),
        ("Errors", s.get("errors", 0)),
        ("Rows written", s.get("written", 0)),
    ]
    rows = "".join(
        f"<tr><td>{label}</td><td style='text-align:right'>{value}</td></tr>"
        for label, value in metric_rows
    )

    # Say so loudly when the run was read-only — a green SUCCESS email reporting
    # analysed rows would otherwise imply the sheet was updated.
    notes = []
    if s.get("dry_run"):
        notes.append(
            "<b>Dry run.</b> Briefs were produced and logged; no reports were "
            "created and the tracker was not touched."
        )
    else:
        if not s.get("write_back_enabled", True):
            notes.append(
                "<b>Tracker write-back is disabled.</b> Reports were created, but "
                "no likelihood score or report link was written to the tracker "
                "(<code>WRITE_BACK_ENABLED = False</code>, and "
                "<code>OUTPUT_FIELD_MAP</code> needs the columns)."
            )
        if not s.get("reports_enabled", True):
            notes.append(
                "<b>Report creation is disabled.</b> Tenders were analysed but no "
                "report was written to Drive (<code>REPORTS_ENABLED = False</code>)."
            )
    dry_run_note = "".join(
        f"<p style='background:#fff3cd;border:1px solid #ffe08a;padding:10px;"
        f"border-radius:4px;font-size:13px'>{n}</p>" for n in notes
    )

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
     <b>Scope:</b> tenders qualified as Bid and not yet analysed in detail</p>
  {dry_run_note}
  {sheet_link}
{_reports_table(s)}
  <h3 style="margin:16px 0 4px;font-size:15px">Detailed Analysis - This Run</h3>
  <table cellpadding="8" cellspacing="0" border="1"
         style="border-collapse:collapse;border-color:#ddd;font-size:14px">
    <tr style="background:#f0f0f0"><th align="left">Metric</th><th>Count</th></tr>
    {rows}
  </table>
  {details}
  <p style="color:#888;font-size:12px;margin-top:20px">
     Automated message from the PS BidAnalyzer (DetailedAnalyzer).</p>
</body></html>"""
    return subject, html


def main():
    _configure_logging()
    parser = argparse.ArgumentParser(
        description="Run detailed analysis over qualified tenders in the tracker."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Analyse only the first N tenders (for testing).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Analyse and log each brief without creating reports or writing to "
             "the tracker.",
    )
    args = parser.parse_args()

    # Stamp the run date up front so the alert reports it even if the run aborts
    # before producing a summary.
    run_date = datetime.now(UK_TIMEZONE).strftime("%Y-%m-%d")
    started_at = datetime.now(UK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    summary = None
    error_tb = None
    try:
        summary = run(limit=args.limit, dry_run=args.dry_run)
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
