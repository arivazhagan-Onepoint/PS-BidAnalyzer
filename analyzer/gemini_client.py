"""
Thin Gemini client wrapper, mirroring the connection pattern used in
openrouter_client.py so the whole project shares one approach to the LLM.

This is the ACTIVE provider. The equivalent OpenRouter wrapper is retained in
openrouter_client.py as a backup/reference but is not used by the analyzer.
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
        logger.info("Gemini client initialised")
    return _client
