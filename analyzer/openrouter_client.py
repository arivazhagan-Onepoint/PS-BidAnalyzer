"""
Thin OpenRouter client wrapper, mirroring the connection pattern used in
keyword_search.py so the whole project shares one approach to the LLM.
"""
import json
import logging

from openai import OpenAI

from .config import (
    OPENROUTER_CREDENTIALS_FILE,
    OPENROUTER_API_KEY_FIELD,
    OPENROUTER_BASE_URL,
)

logger = logging.getLogger(__name__)

_client = None


def get_client() -> OpenAI:
    """Return a lazily-initialised OpenAI client pointed at OpenRouter."""
    global _client
    if _client is None:
        with open(OPENROUTER_CREDENTIALS_FILE, encoding="utf-8") as f:
            api_key = json.load(f).get(OPENROUTER_API_KEY_FIELD)
        if not api_key:
            raise ValueError(
                f"{OPENROUTER_API_KEY_FIELD} is not set in {OPENROUTER_CREDENTIALS_FILE}"
            )
        _client = OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)
        logger.info("OpenRouter client initialised")
    return _client
