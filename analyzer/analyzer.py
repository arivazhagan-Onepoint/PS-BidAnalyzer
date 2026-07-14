"""
Core Bid Analyzer.

Given a tender title and description, acts as a Tender Analyst for Onepoint:
scores Onepoint's ability to meet the tender requirements out of 100 against the
Onepoint capability context, then maps that score to a Bid / NoBid / TBD
qualification.

Public API:
    analyze_tender(title, description, run_date=None) -> BidAnalysis
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from google.genai import types

from .config import (
    ANALYZER_MODEL,
    ANALYZER_TEMPERATURE,
    ANALYZER_MAX_TOKENS,
    ANALYZER_THINKING_BUDGET,
    ANALYZER_MAX_RETRIES,
    API_THROTTLE_SECONDS,
    UK_TIMEZONE,
    score_to_qualification,
    QUALIFICATION_NOBID,
)
from .gemini_client import get_client
from .onepoint_context import load_onepoint_context

logger = logging.getLogger(__name__)


@dataclass
class BidAnalysis:
    """Result of analysing a single tender.

    The three fields the main module records back to the sheet map to:
      bid_qualification      -> [Bid Qualification]
      bid_qualification_reason -> [Bid Qualification Reason(System)]
      bid_qualification_date -> [Bid Qualification Date]
    """
    bid_qualification: str          # Bid / NoBid / TBD
    bid_qualification_reason: str   # system-generated summarised reason
    bid_qualification_date: str     # YYYY-MM-DD the qualification was arrived at
    score: float = 0.0              # raw analysis score 0-100 (kept for comments)


_SYSTEM_PROMPT = (
    "You are a Tender Analyst for Onepoint. Your job is to sincerely assess "
    "whether Onepoint should bid for a tender, giving genuine hope to opportunities "
    "Onepoint can realistically win while being honest about poor fits. You base "
    "your judgement strictly on Onepoint's documented capabilities, experience and "
    "accreditations provided below — never on assumptions beyond them."
)


def _build_prompt(title: str, description: str, context: str) -> str:
    context_block = context if context else "(No Onepoint capability context provided.)"
    return f"""Onepoint capability context (use ONLY this to judge capability):
---
{context_block}
---

Tender under review:
Title: {title}
Description: {description}

Assess how well Onepoint can meet this tender's requirements. Consider capability
fit, relevant past experience, required accreditations/certifications, scale and
any obvious blockers. Produce a single overall score out of 100 where a higher
score means a stronger, more winnable fit for Onepoint.

Respond with ONLY a JSON object — no markdown, no explanation:
{{"score": <integer 0-100>, "reason": "<2-4 sentence summary justifying the score, citing the specific capability matches or gaps>"}}"""


def analyze_tender(title: str, description: str, run_date: datetime = None) -> BidAnalysis:
    """Analyse one tender and return a BidAnalysis.

    On empty input or API failure, returns a NoBid with a system-generated reason
    explaining why, so the caller can always record a deterministic result.
    """
    if run_date is None:
        run_date = datetime.now(UK_TIMEZONE)
    date_str = run_date.strftime("%Y-%m-%d")

    title = (title or "").strip()
    description = (description or "").strip()

    if not title and not description:
        return BidAnalysis(
            bid_qualification=QUALIFICATION_NOBID,
            bid_qualification_reason="No tender title or description available to analyse.",
            bid_qualification_date=date_str,
            score=0.0,
        )

    context = load_onepoint_context()
    prompt = _build_prompt(title, description, context)

    last_error = None
    for attempt in range(1, ANALYZER_MAX_RETRIES + 1):
        try:
            response = get_client().models.generate_content(
                model=ANALYZER_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM_PROMPT,
                    temperature=ANALYZER_TEMPERATURE,
                    max_output_tokens=ANALYZER_MAX_TOKENS,
                    # Ask Gemini for raw JSON so the reply parses cleanly.
                    response_mime_type="application/json",
                    # Thinking tokens share the output budget on Gemini 3.x; keep
                    # them bounded so the JSON always fits (see config note).
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=ANALYZER_THINKING_BUDGET
                    ),
                ),
            )

            candidate = response.candidates[0] if response.candidates else None
            finish_reason = getattr(candidate, "finish_reason", None)
            try:
                raw = (response.text or "").strip()
            except Exception:
                # response.text raises if the candidate didn't cleanly finish;
                # treat it as empty and let the retry loop handle it below.
                raw = ""

            # Bail out if the model didn't cleanly stop and let the retry loop try
            # again rather than parse garbage. Gemini's clean stop is FinishReason.STOP.
            finish_name = getattr(finish_reason, "name", None)
            if (finish_name not in ("STOP", None)) or not raw:
                raise ValueError(
                    f"incomplete response from model "
                    f"(finish_reason={finish_name!r}, {len(raw)} chars)"
                )

            result = _parse_response(raw)

            score = float(result.get("score", 0))
            score = max(0.0, min(100.0, score))
            reason = str(result.get("reason", "")).strip() or "No reason returned by the analyzer."
            qualification = score_to_qualification(score)

            logger.info(f"Analysed '{title[:60]}': score={score:.0f} -> {qualification}")
            return BidAnalysis(
                bid_qualification=qualification,
                bid_qualification_reason=reason,
                bid_qualification_date=date_str,
                score=score,
            )

        except Exception as e:
            last_error = e
            logger.warning(
                f"Analyzer attempt {attempt}/{ANALYZER_MAX_RETRIES} failed for "
                f"title='{title[:60]}': {e}"
            )
            # Throttle between attempts (respects OpenRouter rate limits and
            # gives a flaky upstream provider a moment to recover).
            if attempt < ANALYZER_MAX_RETRIES:
                time.sleep(API_THROTTLE_SECONDS)

    # All retries exhausted — record a deterministic NoBid for manual review.
    logger.error(f"Analyzer failed for title='{title[:60]}' after {ANALYZER_MAX_RETRIES} attempts: {last_error}")
    time.sleep(API_THROTTLE_SECONDS)
    return BidAnalysis(
        bid_qualification=QUALIFICATION_NOBID,
        bid_qualification_reason=f"Analysis could not be completed ({last_error}). Marked NoBid pending manual review.",
        bid_qualification_date=date_str,
        score=0.0,
    )


def _parse_response(raw: str) -> dict:
    """Parse the model's JSON reply, tolerating markdown code-fence wrapping."""
    text = raw
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)
raw = ""

            # A truncated reply (finish_reason MAX_TOKENS) or a blocked/empty
            # body leaves partial JSON that fails to parse. Reject anything that
          