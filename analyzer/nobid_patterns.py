"""
Loads the distilled NoBid decision heuristics that supplement the bid analysis.

The heuristics are generated periodically by ``analyzer.maintain_nobids`` from the
human ``NoBid(Human)`` reasons collected in the PS NoBids tab, and written to
``knowledge/nobid_patterns.md``. This module reads that file and caches it for
injection into the analysis prompt as *decision precedent* — a secondary signal
alongside (never replacing) the Onepoint capability context.

Mirrors ``onepoint_context.load_onepoint_context``: a missing or empty file
degrades gracefully to an empty string so the analyzer simply injects nothing.
"""
import logging
import os

from .config import NOBID_PATTERNS_FILE

logger = logging.getLogger(__name__)

_patterns_cache = None


def load_nobid_patterns() -> str:
    """Return the distilled NoBid heuristics as a string (cached).

    Returns an empty string (and logs at INFO, not WARNING — an absent file is a
    normal state before the first maintenance run) if the file is missing/empty.
    """
    global _patterns_cache
    if _patterns_cache is not None:
        return _patterns_cache

    if not os.path.exists(NOBID_PATTERNS_FILE):
        logger.info(
            f"No NoBid patterns file at {NOBID_PATTERNS_FILE}; analysis will run "
            f"without NoBid precedent (run 'python -m analyzer.maintain_nobids' to build it)."
        )
        _patterns_cache = ""
        return _patterns_cache

    with open(NOBID_PATTERNS_FILE, encoding="utf-8") as f:
        content = f.read().strip()

    _patterns_cache = content
    logger.info(f"Loaded NoBid patterns ({len(content)} chars) from {NOBID_PATTERNS_FILE}")
    return _patterns_cache
