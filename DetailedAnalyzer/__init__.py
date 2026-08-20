"""
PS BidAnalyzer — DetailedAnalyzer package for Onepoint.

Second-stage analysis. Where ``analyzer`` answers "should Onepoint bid on this
tender at all?" with a single 0-100 score, this module takes the tenders that
survived that gate and produces a fuller, structured assessment for the bid team
to work from.

Entry point:  python -m DetailedAnalyzer.main
"""

__version__ = "0.1.0"
