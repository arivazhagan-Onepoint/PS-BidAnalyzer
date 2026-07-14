"""
Loads the Onepoint capability context that grounds the bid analysis.

The Requirements reference four NotebookLM notebooks describing Onepoint's
capabilities, past performance, accreditations and target markets. NotebookLM
content cannot be fetched programmatically, so the substance of those notebooks
must be captured once in ``knowledge/onepoint_capabilities.md``; this module
reads that file and caches it for injection into the analysis prompt.
"""
import logging
import os

from .config import ONEPOINT_CONTEXT_FILE

logger = logging.getLogger(__name__)

_context_cache = None

_MISSING_CONTEXT_WARNING = (
    "Onepoint capability context file not found or empty at "
    "{path}. Analysis will proceed with NO company context, which will produce "
    "low-confidence scores. Populate this file from the NotebookLM sources listed "
    "in Requirements.md."
)


def load_onepoint_context() -> str:
    """Return the Onepoint capability context as a string (cached).

    Returns an empty string and logs a warning if the knowledge file is absent
    or empty, so a missing file degrades gracefully rather than crashing a run.
    """
    global _context_cache
    if _context_cache is not None:
        return _context_cache

    if not os.path.exists(ONEPOINT_CONTEXT_FILE):
        logger.warning(_MISSING_CONTEXT_WARNING.format(path=ONEPOINT_CONTEXT_FILE))
        _context_cache = ""
        return _context_cache

    with open(ONEPOINT_CONTEXT_FILE, encoding="utf-8") as f:
        content = f.read().strip()

    if not content:
        logger.warning(_MISSING_CONTEXT_WARNING.format(path=ONEPOINT_CONTEXT_FILE))

    _context_cache = content
    logger.info(f"Loaded Onepoint context ({len(content)} chars) from {ONEPOINT_CONTEXT_FILE}")
    return _context_cache
