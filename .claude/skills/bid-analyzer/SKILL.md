---
name: bid-analyzer
description: >-
  Run the PS-BidAnalyzer to score PS Tender Tracker rows and write back
  Bid/NoBid/TBD qualifications. Use when the user wants to analyse tenders,
  run a bid qualification pass, backfill a specific day, or do a quick capped
  test run — instead of typing the `python -m analyzer.main` command by hand.
---

# Bid Analyzer

Runs the analyzer entry point (`analyzer.main`) which reads the **PS Tender
Tracker** sheet, scores each qualifying tender against Onepoint's capabilities
with Gemini, and writes back `Bid Qualification`, the reason, date, comment and
row colour.

## How to run

Always run from the project root using the interpreter the user has, and stream
the output back to the user. On this machine the interpreter is `py.exe`; fall
back to `python` if `py` is unavailable.

Parse the user's request into at most these two optional flags:

| Intent | Flag | Example phrasing |
|--------|------|------------------|
| Analyse a specific day (backfill / rerun) | `--date YYYY-MM-DD` | "analyse 2026-07-02", "backfill July 2nd" |
| Cap to the first N qualifying rows (quick test) | `--limit N` | "quick test", "just 5 rows", "limit 3" |
| Analyse today's window (UK time) | *(no flags)* | "run it", "analyse today" |

The two flags can be combined. Commands:

```bash
py -m analyzer.main                      # today's window (UK time)
py -m analyzer.main --date 2026-07-02    # a specific day
py -m analyzer.main --limit 5            # first 5 qualifying rows
py -m analyzer.main --date 2026-07-02 --limit 5
```

## Steps

1. Determine the flags from the user's request (default: no flags = today).
2. Run the command from the project root with the Bash or PowerShell tool.
3. When it finishes, summarise the run's own summary block for the user:
   Analysed / Bid / TBD / NoBid / Skipped / Out of window / Errors. Do not
   re-explain the whole pipeline unless asked.
4. If `Analysed` is 0 because everything was out of window, say so plainly and
   offer to rerun with a `--date` that has qualifying rows, rather than
   silently reporting success.

## Notes

- Only rows whose `Bid Qualification` is `PreQualified`/`ReCheck` **and** whose
  `Last Modified Date` falls in the target one-day window are analysed.
- The run log is also written to `analyzer/analyzer.log`.
- Requires the local credentials and `project_config.json` described in
  `CLAUDE.md` / `SETUP.md`; if the run fails on auth or missing files, point the
  user there.
