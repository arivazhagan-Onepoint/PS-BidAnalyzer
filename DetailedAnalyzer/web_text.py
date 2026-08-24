"""
Fetching readable text out of a web page.

Used by ``sources`` to read Onepoint's own website into the corpus. Kept as its
own module so the fetching and the boilerplate-stripping can be tested apart from
the ingestion around them, and so anything else in this stage that later needs a
web page strips the same things — a cookie banner counted as evidence in one
place and not another would be a difference with no reason behind it.

Deliberately small. ``requests`` + ``beautifulsoup4`` are already project
dependencies, used by the upstream PS-WebScrapper, so nothing new is added.
"""
import logging
import re

import requests
from bs4 import BeautifulSoup

from .config import WEB_USER_AGENT

logger = logging.getLogger(__name__)

# Elements that never carry evidence. Stripped before the text is taken, because
# a nav menu repeated on 25 pages reads to a model as 25 mentions of whatever it
# links to.
_STRIP_TAGS = (
    "script", "style", "noscript", "nav", "footer", "header",
    "form", "svg", "iframe", "button", "template",
)

# Cookie/consent banners and skip links. Matched on the id/class of a container,
# then on the text itself as a fallback — GOV.UK services put the banner in a
# plain <div> that no tag filter can see.
_STRIP_ATTR_HINTS = (
    "cookie", "consent", "gdpr", "skip-link", "skiplink",
    "breadcrumb", "site-header", "site-footer", "menu", "navbar",
)
_BOILERPLATE_LINES = (
    re.compile(r"we use some essential cookies.*?(?=\.|$)", re.I | re.S),
    re.compile(r"(accept|reject) (all |analytics )?cookies", re.I),
    re.compile(r"view cookies", re.I),
    re.compile(r"skip to (main )?content", re.I),
    re.compile(r"you[’']ve (accepted|rejected) .{0,40}cookies", re.I),
    re.compile(r"hide (this )?(message|banner)", re.I),
)

# Whole lines that are page furniture. Matched exactly (after whitespace
# collapsing) so a real sentence merely containing the words survives.
_FURNITURE_LINES = frozenset((
    "back to top of page", "back to top", "read more", "learn more",
    "find out more", "share this", "next", "previous", "menu", "close",
    "search", "toggle navigation", "all rights reserved",
))

# A paragraph repeated elsewhere on the same page is one claim restated, and to a
# model repetition reads as corroboration. Only applied to lines this long: short
# ones ("Overview", a date, a name) legitimately recur.
_DEDUPE_MIN_LEN = 60


def html_to_text(html: str) -> str:
    """Readable text of an HTML page, boilerplate removed.

    Block-level elements are separated by newlines rather than spaces so headings
    and list items stay on their own lines — a wall of run-together text loses the
    structure that makes a requirements list readable.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()

    for element in soup.find_all(attrs={"class": True}):
        joined = " ".join(element.get("class") or []).lower()
        if any(h in joined for h in _STRIP_ATTR_HINTS):
            element.decompose()
    for element in soup.find_all(attrs={"id": True}):
        if any(h in (element.get("id") or "").lower() for h in _STRIP_ATTR_HINTS):
            element.decompose()

    text = soup.get_text("\n")
    for pattern in _BOILERPLATE_LINES:
        text = pattern.sub(" ", text)

    out, previous, seen = [], None, set()
    for raw in text.splitlines():
        line = " ".join(raw.split())
        # Blanks, and immediate repeats — a template often emits a heading twice,
        # once visibly and once for a screen reader.
        if not line or line == previous:
            continue
        if line.lower() in _FURNITURE_LINES:
            continue
        # Non-adjacent repeats of a substantial paragraph. Measured on
        # /accelerated/, where one 240-character claim appeared twice.
        if len(line) >= _DEDUPE_MIN_LEN:
            if line in seen:
                continue
            seen.add(line)
        out.append(line)
        previous = line
    return "\n".join(out)


def fetch_text(url: str, timeout: int, max_chars: int = 0) -> str:
    """GET a page and return its readable text. Raises on a failed fetch.

    Raising rather than returning empty is deliberate: the caller records *why* a
    page could not be read, and "the fetch 403'd" and "the page is genuinely
    empty" call for different handling.
    """
    response = requests.get(
        url, headers={"User-Agent": WEB_USER_AGENT}, timeout=timeout,
        allow_redirects=True,
    )
    response.raise_for_status()

    content_type = (response.headers.get("Content-Type") or "").lower()
    if "html" not in content_type and "xml" not in content_type:
        raise ValueError(f"not an HTML page (Content-Type: {content_type or 'unknown'})")

    text = html_to_text(response.text).strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncated at {max_chars:,} characters ...]"
    return text


def page_title(html: str) -> str:
    """The page's <title>, trimmed of the trailing site name where there is one."""
    soup = BeautifulSoup(html, "html.parser")
    title = " ".join((soup.title.string or "").split()) if soup.title else ""
    return title
