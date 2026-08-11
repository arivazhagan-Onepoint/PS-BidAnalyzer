"""
Loads the distilled decision heuristics that supplement the bid analysis.

The heuristics are generated periodically by ``analyzer.maintain_knowledge`` from
the human reasons collected in the PS NoBids / PS Bids tabs, and written to
``knowledge/nobid_patterns.md`` and ``knowledge/bid_patterns.md``. This module
reads those files and caches them for injection into the analysis prompt as
*decision precedent* — a secondary signal alongside (never replacing) the Onepoint
capability context.

Mirrors ``onepoint_context.load_onepoint_context``: a missing or empty file
degrades gracefully to an empty string so the analyzer simply injects nothing.
"""
import logging
import os

from .config import NOBID_PATTERNS_FILE, BID_PATTERNS_FILE

logger = logging.getLogger(__name__)

# Cached file contents, keyed by path — one entry per polarity.
_cache = {}


def _load(path: str, label: str) -> str:
    """Return the distilled heuristics at ``path`` as a string (cached).

    Returns an empty string (and logs at INFO, not WARNING — an absent file is a
    normal state before the first maintenance run) if the file is missing/empty.
    """
    if path in _cache:
        return _cache[path]

    if not os.path.exists(path):
        logger.info(
            f"No {label} patterns file at {path}; analysis will run without "
            f"{label} precedent (run 'python -m analyzer.maintain_knowledge' to build it)."
        )
        _cache[path] = ""
        return _cache[path]

    with open(path, encoding="utf-8") as f:
        content = f.read().strip()

    _cache[path] = content
    logger.info(f"Loaded {label} patterns ({len(content)} chars) from {path}")
    return content


def load_nobid_patterns() -> str:
    """Distilled NoBid heuristics — poor-fit patterns that calibrate the score DOWN."""
    return _load(NOBID_PATTERNS_FILE, "NoBid")


def load_bid_patterns() -> str:
    """Distilled Bid heuristics — winnable patterns that calibrate the score UP."""
    return _load(BID_PATTERNS_FILE, "Bid")
