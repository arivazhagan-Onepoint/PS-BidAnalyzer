"""
Loads the Onepoint capability context that grounds the detailed analysis.

Reads the SAME hand-authored file the analyzer uses
(``analyzer/knowledge/onepoint_capabilities.md``, see config.ONEPOINT_CONTEXT_FILE)
rather than a copy: both stages must judge capability against one text, or they
will drift and start disagreeing about what Onepoint can do. Nothing in this
module writes to that file.

Mirrors ``analyzer/onepoint_context.py``: cached per process, and a missing or
empty file degrades to an empty string with a warning rather than crashing a run.
"""
import logging
import os

from .config import ONEPOINT_CONTEXT_FILE

logger = logging.getLogger(__name__)

_context_cache = None

_MISSING_CONTEXT_WARNING = (
    "Onepoint capability context file not found or empty at {path}. Detailed "
    "analysis will proceed with NO company context, which will produce generic, "
    "low-confidence output. This file is shared with the analyzer stage — "
    "populate it there."
)


def load_onepoint_context() -> str:
    """Return the Onepoint capability context as a string (cached)."""
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
    logger.info(
        f"Loaded Onepoint context ({len(content)} chars) from {ONEPOINT_CONTEXT_FILE}"
    )
    return _context_cache
