"""
NoBid knowledge maintenance — the scheduled 2-step flow that keeps the NoBid
precedent used by the bid analysis current. This is deliberately SEPARATE from
the per-tender analysis (analyzer.main): it runs occasionally, not per tender.

  Step 1  Extract : sync NoBid(Human) rows from the main tab into the PS NoBids
                    tab (reuses SheetsClient.sync_matching_to_tab — deduped).
  Step 2  Distil  : consolidate the human reasons into general NoBid decision
                    heuristics via one LLM call and write them to
                    knowledge/nobid_patterns.md, which analyzer.main injects.

Step 2 is guarded: unless there are at least NOBID_MIN_EXAMPLES distinct genuine
reasons (test/placeholder junk filtered out), it SKIPS regeneration and keeps the
existing file — so sparse or junk data never overwrites good heuristics.

Run:  python -m analyzer.maintain_nobids            (respects the data guard)
      python -m analyzer.maintain_nobids --force    (distil regardless — testing)
"""
import argparse
import logging
import sys
from datetime import datetime

from google.genai import types

from .config import (
    LOG_FILE,
    UK_TIMEZONE,
    COPY_TO_NOBIDS_STATUS,
    NOBIDS_SHEET_NAME,
    NOBID_PATTERNS_FILE,
    NOBID_MIN_EXAMPLES,
    ANALYZER_MODEL,
    NOBID_DISTILL_MAX_TOKENS,
    NOBID_DISTILL_TEMPERATURE,
    NOBID_DISTILL_THINKING_BUDGET,
    genuine_nobid_reasons,
)
from .gemini_client import get_client
from .sheets_client import SheetsClient

logger = logging.getLogger(__name__)

# Columns read from the PS NoBids tab for distillation.
NAME_FIELD   = "Name"
REASON_FIELD = "Bid Qualification Reason(Human)"

_DISTILL_SYSTEM = (
    "You consolidate a company's historical NoBid decisions into a concise set of "
    "general, reusable decision heuristics for a Tender Analyst at Onepoint. You "
    "generalise across the examples and never invent reasons not supported by them."
)


def _configure_logging():
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


def _distill(examples: list) -> str:
    """Distil (name, reason) pairs into a markdown heuristics list via one LLM call.

    Raises on an incomplete/empty model reply so the caller keeps the existing
    file rather than writing a truncated one.
    """
    listing = "\n".join(
        f"- Tender: {name or '(no title)'}\n  Reason for NoBid: {reason}"
        for name, reason in examples
    )
    prompt = f"""Below are past tenders Onepoint decided NOT to bid on, each with the human-written reason.

{listing}

Consolidate these into a SHORT markdown bullet list of general NoBid decision
heuristics: the recurring patterns and criteria that explain why Onepoint declines
tenders. Group similar reasons together, deduplicate, and ignore any placeholder or
test entries. Each bullet must be a GENERAL rule useful for judging future tenders —
not a restatement of a single tender. Output ONLY the markdown bullet list, with no
preamble or closing commentary."""

    response = get_client().models.generate_content(
        model=ANALYZER_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_DISTILL_SYSTEM,
            temperature=NOBID_DISTILL_TEMPERATURE,
            max_output_tokens=NOBID_DISTILL_MAX_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_budget=NOBID_DISTILL_THINKING_BUDGET),
        ),
    )
    candidate = response.candidates[0] if response.candidates else None
    finish_name = getattr(getattr(candidate, "finish_reason", None), "name", None)
    try:
        text = (response.text or "").strip()
    except Exception:
        text = ""
    if (finish_name not in ("STOP", None)) or not text:
        raise ValueError(
            f"incomplete distillation reply (finish_reason={finish_name!r}, {len(text)} chars)"
        )
    return text


def run(force: bool = False) -> dict:
    """Run Step 1 (extract) then Step 2 (distil). Returns a summary dict."""
    run_dt = datetime.now(UK_TIMEZONE)
    logger.info("=" * 80)
    logger.info("PS BidAnalyzer — NoBid knowledge maintenance")
    logger.info(f"Run timestamp: {run_dt.isoformat()}")
    logger.info("=" * 80)

    client = SheetsClient()
    client.open_sheet()

    # --- Step 1: extract NoBid(Human) rows into the PS NoBids tab -------------
    tenders = client.read_tenders()
    synced = client.sync_matching_to_tab(tenders, COPY_TO_NOBIDS_STATUS, NOBIDS_SHEET_NAME)
    logger.info(f"Step 1 (extract) complete: '{NOBIDS_SHEET_NAME}' now holds {synced} row(s).")

    # --- Step 2: distil the human reasons into heuristics --------------------
    rows = client.read_tab(NOBIDS_SHEET_NAME)
    pairs = [(r.get(NAME_FIELD, "").strip(), r.get(REASON_FIELD, "").strip()) for r in rows]
    genuine = genuine_nobid_reasons(reason for _, reason in pairs)
    logger.info(
        f"{len(genuine)} genuine reason(s) from {len(pairs)} row(s) "
        f"(minimum required: {NOBID_MIN_EXAMPLES}{'; --force set' if force else ''})."
    )

    if len(genuine) < NOBID_MIN_EXAMPLES and not force:
        logger.warning(
            f"Insufficient genuine NoBid reasons ({len(genuine)} < {NOBID_MIN_EXAMPLES}); "
            f"SKIPPING regeneration and keeping the existing {NOBID_PATTERNS_FILE}."
        )
        return {"synced": synced, "genuine": len(genuine), "regenerated": False}

    # One example per distinct genuine reason (first tender seen for it).
    genuine_set, examples, seen = {g.lower() for g in genuine}, [], set()
    for name, reason in pairs:
        low = reason.lower()
        if low in genuine_set and low not in seen:
            seen.add(low)
            examples.append((name, reason))

    if not examples:
        logger.warning("No genuine examples to distil (all filtered); keeping existing file.")
        return {"synced": synced, "genuine": len(genuine), "regenerated": False}

    patterns = _distill(examples)
    header = (
        f"<!-- Generated {run_dt.strftime('%Y-%m-%d %H:%M %Z')} by "
        f"analyzer.maintain_nobids from {len(examples)} NoBid(Human) example(s). "
        f"Do not edit by hand — regenerated on each maintenance run. -->\n\n"
        f"# Onepoint NoBid Decision Heuristics\n\n"
    )
    with open(NOBID_PATTERNS_FILE, "w", encoding="utf-8") as f:
        f.write(header + patterns.rstrip() + "\n")
    logger.info(
        f"Step 2 (distil) complete: wrote {len(patterns)} chars of heuristics "
        f"from {len(examples)} example(s) to {NOBID_PATTERNS_FILE}."
    )
    return {"synced": synced, "genuine": len(genuine), "regenerated": True}


def main():
    _configure_logging()
    parser = argparse.ArgumentParser(
        description="Maintain the NoBid knowledge: sync PS NoBids, then distil nobid_patterns.md."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Distil even if fewer than the minimum genuine reasons exist (testing).",
    )
    args = parser.parse_args()

    try:
        summary = run(force=args.force)
    except Exception as e:
        logger.error(f"NoBid maintenance failed: {e}", exc_info=True)
        sys.exit(1)

    logger.info("=" * 80)
    logger.info("NOBID MAINTENANCE COMPLETE")
    logger.info(f"  Synced to {NOBIDS_SHEET_NAME} : {summary['synced']}")
    logger.info(f"  Genuine reasons             : {summary['genuine']}")
    logger.info(f"  Patterns regenerated        : {summary['regenerated']}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
