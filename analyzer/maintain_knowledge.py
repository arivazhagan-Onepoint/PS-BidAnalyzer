"""
Bid knowledge maintenance — the scheduled 2-step flow that keeps the human
decision precedent used by the bid analysis current. This is deliberately
SEPARATE from the per-tender analysis (analyzer.main): it runs occasionally,
not per tender.

Both steps iterate config.KNOWLEDGE_SOURCES, so each polarity runs through
identical code:

  Step 1  Extract : sync every human-decision status from the main tab into its
                    own tab — NoBid(Human) -> PS NoBids and Bid(Human) -> PS Bids
                    (reuses SheetsClient.sync_matching_to_tab — deduped). One read
                    of the sheet serves every source.
  Step 2  Distil  : consolidate each tab's human reasons into general decision
                    heuristics via one LLM call per source, written to that
                    source's patterns file (knowledge/nobid_patterns.md,
                    knowledge/bid_patterns.md).

Each source is independent: its own guard, its own LLM call, its own file, and its
own error handling. One polarity failing or lacking data never affects the other.

Step 2 is guarded per source: unless there are at least that source's
min_examples distinct genuine reasons (test/placeholder junk filtered out), it
SKIPS regeneration and keeps the existing file — so sparse or junk data never
overwrites good heuristics. ``--force`` bypasses the minimum for every source,
but a source with ZERO genuine reasons is still skipped: there is nothing to send
the model, so its existing file is kept.

Both artifacts are injected into the analysis prompt by analyzer.main as separate
precedent blocks — NoBid heuristics calibrate a score DOWN, Bid heuristics UP — so
what this flow writes directly shapes future scoring. That is why the guards
matter: heuristics distilled from too few examples become confident nonsense the
analyzer then applies to every tender.

Run:  python -m analyzer.maintain_knowledge            (respects the data guards)
      python -m analyzer.maintain_knowledge --force    (distil regardless — testing)
"""
import argparse
import logging
import sys
from datetime import datetime

from google.genai import types

from .config import (
    LOG_FILE,
    UK_TIMEZONE,
    KNOWLEDGE_SOURCES,
    ANALYZER_MODEL,
    DISTILL_MAX_TOKENS,
    DISTILL_TEMPERATURE,
    DISTILL_THINKING_BUDGET,
    genuine_reasons,
)
from .gemini_client import get_client
from .sheets_client import SheetsClient

logger = logging.getLogger(__name__)

# Columns read from each source tab for distillation.
NAME_FIELD   = "Name"
REASON_FIELD = "Bid Qualification Reason(Human)"

_DISTILL_SYSTEM = (
    "You consolidate a company's historical {polarity} decisions into a concise set "
    "of general, reusable decision heuristics for a Tender Analyst at Onepoint. You "
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


def _distill(examples: list, source: dict) -> str:
    """Distil (name, reason) pairs into a markdown heuristics list via one LLM call.

    ``source`` is a KNOWLEDGE_SOURCES entry supplying the polarity wording, so one
    routine serves both the Bid and NoBid sides. Raises on an incomplete/empty
    model reply so the caller keeps the existing file rather than writing a
    truncated one.
    """
    polarity = source["polarity"]
    listing = "\n".join(
        f"- Tender: {name or '(no title)'}\n  Reason for {polarity}: {reason}"
        for name, reason in examples
    )
    prompt = f"""Below are past tenders Onepoint {source['decision']}, each with the human-written reason.

{listing}

Consolidate these into a SHORT markdown bullet list of {source['directive']}.
Group similar reasons together, deduplicate, and ignore any placeholder or test
entries. Each bullet must be a GENERAL rule useful for judging future tenders —
not a restatement of a single tender.

Phrase every bullet as a direct instruction to the analyst, beginning with the word
"{source['verb']}" — a rule to apply, not a description of what Onepoint has tended
to do. A bullet may carry a short bold label first (e.g. "**Scope of Services:**"),
but the instruction that follows it must start with "{source['verb']}".

Output ONLY the markdown bullet list, with no preamble or closing commentary."""

    response = get_client().models.generate_content(
        model=ANALYZER_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_DISTILL_SYSTEM.format(polarity=polarity),
            temperature=DISTILL_TEMPERATURE,
            max_output_tokens=DISTILL_MAX_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_budget=DISTILL_THINKING_BUDGET),
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
    logger.info("PS BidAnalyzer — Bid knowledge maintenance")
    logger.info(f"Run timestamp: {run_dt.isoformat()}")
    logger.info("=" * 80)

    client = SheetsClient()
    client.open_sheet()

    # --- Step 1: extract human-decision rows into their dedicated tabs --------
    # One read of the sheet feeds every polarity (NoBid(Human) -> PS NoBids,
    # Bid(Human) -> PS Bids). Each source is isolated: a failure on one tab is
    # logged and recorded as 0 rather than aborting the run, so a problem on one
    # polarity can never prevent the other's distillation below.
    tenders = client.read_tenders()
    extracted = {}
    for source in KNOWLEDGE_SOURCES:
        tab_name, status_value = source["tab"], source["status"]
        try:
            extracted[tab_name] = client.sync_matching_to_tab(tenders, status_value, tab_name)
        except Exception as e:
            logger.error(f"Extract failed for '{status_value}' -> '{tab_name}': {e}")
            extracted[tab_name] = 0
    logger.info(
        "Step 1 (extract) complete: "
        + ", ".join(f"'{tab}' holds {n} row(s)" for tab, n in extracted.items())
    )

    # --- Step 2: distil each source's human reasons into its heuristics -------
    # Per source, fully independent: own guard, own LLM call, own file. A failure
    # or a data shortfall on one polarity leaves the other's file untouched.
    distilled = {}
    for source in KNOWLEDGE_SOURCES:
        try:
            distilled[source["polarity"]] = _distill_source(client, source, run_dt, force)
        except Exception as e:
            logger.error(
                f"Distillation failed for {source['polarity']}; keeping the existing "
                f"{source['patterns_file']}: {e}"
            )
            distilled[source["polarity"]] = {"genuine": 0, "regenerated": False}

    return {"extracted": extracted, "distilled": distilled}


def _distill_source(client, source: dict, run_dt, force: bool) -> dict:
    """Distil one KNOWLEDGE_SOURCES entry. Returns {genuine, regenerated}.

    Skips regeneration — keeping the existing patterns file — when the source has
    fewer than its ``min_examples`` genuine reasons (unless ``force``), or when it
    has none at all. Zero examples are skipped even under ``force``: there is
    nothing to send the model, and a regeneration attempt would either fail or
    invent heuristics from no evidence.
    """
    polarity, min_examples = source["polarity"], source["min_examples"]
    patterns_file = source["patterns_file"]

    rows = client.read_tab(source["tab"])
    pairs = [(r.get(NAME_FIELD, "").strip(), r.get(REASON_FIELD, "").strip()) for r in rows]
    genuine = genuine_reasons(reason for _, reason in pairs)
    logger.info(
        f"[{polarity}] {len(genuine)} genuine reason(s) from {len(pairs)} row(s) in "
        f"'{source['tab']}' (minimum required: {min_examples}"
        f"{'; --force set' if force else ''})."
    )

    if not genuine:
        logger.warning(
            f"[{polarity}] No genuine reasons to distil"
            f"{' (--force cannot substitute for missing data)' if force else ''}; "
            f"keeping the existing {patterns_file}."
        )
        return {"genuine": 0, "regenerated": False}

    if len(genuine) < min_examples and not force:
        logger.warning(
            f"[{polarity}] Insufficient genuine reasons ({len(genuine)} < {min_examples}); "
            f"SKIPPING regeneration and keeping the existing {patterns_file}."
        )
        return {"genuine": len(genuine), "regenerated": False}

    # One example per distinct genuine reason (first tender seen for it).
    genuine_set, examples, seen = {g.lower() for g in genuine}, [], set()
    for name, reason in pairs:
        low = reason.lower()
        if low in genuine_set and low not in seen:
            seen.add(low)
            examples.append((name, reason))

    patterns = _distill(examples, source)
    header = (
        f"<!-- Generated {run_dt.strftime('%Y-%m-%d %H:%M %Z')} by "
        f"analyzer.maintain_knowledge from {len(examples)} {source['status']} example(s). "
        f"Do not edit by hand — regenerated on each maintenance run. -->\n\n"
        f"# {source['title']}\n\n"
    )
    with open(patterns_file, "w", encoding="utf-8") as f:
        f.write(header + patterns.rstrip() + "\n")
    logger.info(
        f"[{polarity}] Distilled {len(patterns)} chars of heuristics from "
        f"{len(examples)} example(s) to {patterns_file}."
    )
    return {"genuine": len(genuine), "regenerated": True}


def main():
    _configure_logging()
    parser = argparse.ArgumentParser(
        description=(
            "Maintain the bid knowledge: extract human Bid/NoBid decisions into "
            "their tabs, then distil nobid_patterns.md."
        )
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Distil even if fewer than the minimum genuine reasons exist (testing).",
    )
    args = parser.parse_args()

    try:
        summary = run(force=args.force)
    except Exception as e:
        logger.error(f"Bid knowledge maintenance failed: {e}", exc_info=True)
        sys.exit(1)

    logger.info("=" * 80)
    logger.info("BID KNOWLEDGE MAINTENANCE COMPLETE")
    for tab, count in summary["extracted"].items():
        logger.info(f"  Extracted to {tab:<12} : {count}")
    for polarity, result in summary["distilled"].items():
        logger.info(
            f"  {polarity:<5} distillation      : {result['genuine']} genuine reason(s), "
            f"regenerated={result['regenerated']}"
        )
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
