"""
Thin Gemini client wrapper for the detailed analysis stage.

Same shape as ``analyzer/gemini_client.py`` — one lazily-initialised client per
process, API key read from the shared credentials file. Kept as this module's own
file rather than importing the analyzer's so the two stages stay independent:
neither can break the other by changing how it talks to the provider.
"""
import json
import logging

from google import genai

from .config import (
    GEMINI_CREDENTIALS_FILE,
    GEMINI_API_KEY_FIELD,
)

logger = logging.getLogger(__name__)

_client = None


def get_client() -> genai.Client:
    """Return a lazily-initialised Gemini client."""
    global _client
    if _client is None:
        with open(GEMINI_CREDENTIALS_FILE, encoding="utf-8") as f:
            api_key = json.load(f).get(GEMINI_API_KEY_FIELD)
        if not api_key:
            raise ValueError(
                f"{GEMINI_API_KEY_FIELD} is not set in {GEMINI_CREDENTIALS_FILE}"
            )
        _client = genai.Client(api_key=api_key)
        logger.info("Gemini client initialised (DetailedAnalyzer)")
    return _client
