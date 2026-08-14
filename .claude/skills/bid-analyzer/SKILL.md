---
name: bid-analyzer
description: >-
  Run the PS-BidAnalyzer to score PS Tender Tracker rows and write back
  Bid/NoBid/TBD qualifications. Use when the user wants to analyse tenders,
  run a bid qualification pass, or do a quick capped test run — instead of
  typing the `python -m analyzer.main` command by hand.
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

There is one optional flag:

| Intent | Flag | Example phrasing |
|--------|------|------------------|
| Cap to the first N qualifying rows (quick test) | `--limit N` | "quick test", "just 5 rows", "limit 3" |
| Analyse everything awaiting qualification | *(no flags)* | "run it", "analyse the tenders" |

```bash
py -m analyzer.main                      # every PreQualified/ReCheck row
py -m analyzer.main --limit 5            # first 5 qualifying rows
```

There is **no date filter and no `--date` flag**. Scope is status alone, so
every row awaiting qualification is processed regardless of age. If the user
asks to analyse or backfill a particular day, explain that dates no longer
select rows: a plain run picks up anything outstanding from that day along with
everything else. The flag is still accepted so an existing scheduled command
does not break, but it only logs a warning and is otherwise ignored.

## Steps

1. Determine whether `--limit` is wanted (default: no flags).
2. Run the command from the project root with the Bash or PowerShell tool.
3. When it finishes, summarise the run's own summary block for the user:
   Eligible / Analysed / Bid / TBD / NoBid / Skipped / Errors. Do not re-explain
   the whole pipeline unless asked.
4. If `Eligible` is 0, say so plainly rather than reporting a bare success —
   it means no row is awaiting qualification, and a row must be set to
   `ReCheck` in the sheet for the analyzer to look at it again.
5. Watch the eligible count. Because nothing bounds run size, the first run
   after a gap drains the whole backlog. A row costs ~1.5 s but always costs
   tokens, so if the eligible count is high, offer `--limit N` to work through
   it in batches rather than in one pass.

## Notes

- Only rows whose `Bid Qualification` is `PreQualified`/`ReCheck` are analysed —
  that status is the analyzer's single entry point, applied before the
  processing loop, so nothing else is processed or logged per row.
- A tender the model fails to score is recorded as `TBD(AI)`, not `NoBid(AI)`,
  with a comment saying it was never scored. Setting it back to `ReCheck`
  re-queues it.
- The run log is also written to `analyzer/analyzer.log`.
- Requires the local credentials and `project_config.json` described in
  `CLAUDE.md` / `SETUP.md`; if the run fails on auth or missing files, point the
  user there.
