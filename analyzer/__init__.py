"""
PS BidAnalyzer — Bid Analyzer package for Onepoint.

Reads tenders from the Google Sheet referenced in project_config.json, runs each
tender's title + description through an LLM-based Tender Analyst (OpenRouter /
Gemini), and writes back the Bid / NoBid / TBD qualification, reason and date.

Entry point:  python -m analyzer.main
"""

__version__ = "0.1.0"
