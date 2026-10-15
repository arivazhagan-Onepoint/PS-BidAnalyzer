---
name: email-notifications
description: How the analyzer's run-summary email is built, colour-coded, configured and disabled. Use when changing the run summary, alert configuration, or notifier output.
---

# Email notifications

Every run sends exactly one HTML summary email, colour-coded by outcome:

- ✅ **SUCCESS** (green) — run completed with zero row errors.
- ⚠️ **COMPLETED WITH ERRORS** (amber) — run finished but ≥1 row hit an
  analyzer error (`summary['errors'] > 0`).
- ❌ **FAILURE** (red) — the run aborted with an exception; the email embeds the
  traceback and the process still exits non-zero (so schedulers register it).

The body reports environment, start/finish times and the run's scope (every row
awaiting qualification), then two tables:

- **Overall Summary** — sheet-wide Bid and TBD totals across *every* row in the
  tracker (not just this run), rolled up by qualification family so `Bid(AI)`,
  `Bid(Human)` and bare `Bid` count together, plus a "Tenders Needing your
  Attention" total. Counted in `run()` off the rows already read — no extra API
  call — with this run's new qualifications substituted in so the totals reflect
  the sheet after write-back. Omitted entirely on a fatal error, where no totals
  were produced (better than showing a misleading 0).
- **Bid Analysis - This Run** — the per-run metrics (Eligible / Analysed / Bid /
  TBD / NoBid / Skipped / Errors). `Eligible` leads because it is the run's scope;
  `Analysed` below it shows how much of that scope was got through, the two
  differing when `--limit` is set or a row has no title/description.

The sheet hyperlink sits between them. The sheet-wide figures are also written to
the run log so a run is auditable without opening the email.

Configuration lives in the `notifications` block of `project_config.json`
(exposed as `config.NOTIFICATIONS`).

Set `"enabled": false` to disable alerts. SMTP auth (if the relay needs it) is
read from `credentials/smtp_credentials.json`. `notifier.send_alert()` never
raises — a broken mailer will not bring down an analysis run. This mirrors the
notifier in the upstream **PS-WebScrapper** module.
