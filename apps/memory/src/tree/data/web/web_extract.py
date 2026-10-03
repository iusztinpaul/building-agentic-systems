"""Main-content extraction: Unlocker HTML → markdown of the page's article (ADR-013 §6).

Pure (no network, no Mongo): ``trafilatura`` keeps headings, links, lists and tables of
the main content and drops nav / sidebar / footer. ``None`` means "too thin to trust" —
the caller falls back to Bright Data's whole-page markdown.

Best of two (task 182): trafilatura discards code wrappers whose classes match its
boilerplate rules (Mintlify/Tailwind ``print:…``, ``overflow-y-hidden!``). When the
default run keeps fewer fenced code blocks than the page has ``<pre>`` elements, a
second run strips ``class`` / ``id`` from each ``<pre>`` and its ancestors (a copy of
the HTML) and wins only with MORE fenced blocks. Stripping is not a default because it
also removes the markers that let trafilatura find code and main content on other sites
(a blog's ``.highlight`` / ``#primary``).

Known limitations (measured live): Mintlify's ``<span data-as="p">`` paragraphs come out
with a few duplicated sentences and link tails glued to the next word; code trafilatura
emits WITHOUT fences can put ``# comment`` lines at line start, which read as H1s.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

import trafilatura
from bs4 import BeautifulSoup
from pydantic import BaseModel

logger = logging.getLogger(__name__)
# trafilatura logs ERROR ("empty HTML tree", "parsed tree length: 0") on every thin or
# non-HTML page; our own WARNING in ``fetch_and_extract_web`` already covers that case.
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)

MIN_EXTRACTED_CHARS = 300
MAX_HEADING_CHARS = 300

ExtractionVariant = Literal["default", "pre_unwrapped"]

_H1_RE = re.compile(r"^\s*#\s+\S", re.MULTILINE)
_FENCE_RE = re.compile(r"^```", re.MULTILINE)
_WHITESPACE_RE = re.compile(r"\s+")
_STRIP_STOP_TAGS = frozenset({"body", "html", "[document]"})


class ExtractedPage(BaseModel):
    """The main content of a page as markdown, the page's metadata title, and which
    trafilatura run produced it."""

    markdown: str
    title: str | None = None
    variant: ExtractionVariant = "default"


def page_title(html: str) -> str | None:
    """The page's metadata title (``og:title`` / single ``<h1>`` / ``<title>``, in
    trafilatura's order), fail-open to ``None``."""

    try:
        metadata = trafilatura.extract_metadata(html)
    except Exception:  # A title is optional; never fail the ingest on it.
        logger.warning("Metadata extraction failed; no page title", exc_info=True)
        return None
    title = metadata.title if metadata is not None else None
    return (title.strip() or None) if title else None


def _first_page_h1(html: str) -> str | None:
    """Text of the first ``<h1>`` in the HTML, whitespace-collapsed; ``None`` if absent."""

    for h1 in BeautifulSoup(html, "html.parser").find_all("h1"):
        text = _WHITESPACE_RE.sub(" ", h1.get_text()).strip()
        if text:
            return text
    return None


def _as_heading(text: str | None) -> str | None:
    """``text`` as H1 text: leading ``#``/whitespace stripped (no ``# # …``), capped at
    ``MAX_HEADING_CHARS``; ``None`` when nothing is left."""

    if not text:
        return None
    return text.lstrip("# \t\r\n")[:MAX_HEADING_CHARS].strip() or None


def _to_markdown(html: str, url: str) -> str | None:
    return trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_formatting=True,
        include_tables=True,
        include_comments=False,
    )


def _code_blocks(markdown: str | None) -> int:
    """Number of fenced code blocks (pairs of line-leading ```` ``` ````)."""

    return len(_FENCE_RE.findall(markdown)) // 2 if markdown else 0


def _best_markdown(html: str, url: str) -> tuple[str | None, ExtractionVariant]:
    """Default run; if it lost code, also the ``<pre>``-unwrapped run — more fences wins."""

    markdown = _to_markdown(html, url)
    if "<pre" not in html.lower():
        return markdown, "default"

    soup = BeautifulSoup(html, "html.parser")
    pres = soup.find_all("pre")
    if _code_blocks(markdown) >= len(pres):
        return markdown, "default"

    for pre in pres:
        for element in (pre, *pre.parents):
            if element.name in _STRIP_STOP_TAGS:
                break
            element.attrs.pop("class", None)
            element.attrs.pop("id", None)
    unwrapped = _to_markdown(str(soup), url)
    if _code_blocks(unwrapped) > _code_blocks(markdown):
        return unwrapped, "pre_unwrapped"
    return markdown, "default"


def extract_main_content(html: str, url: str) -> ExtractedPage | None:
    """Extract ``html``'s main content as markdown, or ``None`` when it is too thin.

    Returns ``None`` when trafilatura finds nothing or the markdown has fewer than
    ``MIN_EXTRACTED_CHARS`` non-whitespace characters. trafilatura often drops the
    page's H1; when the markdown has none, ``# {heading}`` is prepended — the page's
    first ``<h1>`` text, else the metadata title.
    """

    if not html.strip():
        return None

    markdown, variant = _best_markdown(html, url)
    if markdown is None:
        return None
    if len(_WHITESPACE_RE.sub("", markdown)) < MIN_EXTRACTED_CHARS:
        return None

    title = page_title(html)
    if not _H1_RE.search(markdown):
        heading = _as_heading(_first_page_h1(html) or title)
        if heading:
            markdown = f"# {heading}\n\n{markdown}"

    return ExtractedPage(markdown=markdown, title=title, variant=variant)
