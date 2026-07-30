"""Bright Data SERP API HTTP client.

Thin async wrapper around the Bright Data SERP REST endpoint
(``POST https://api.brightdata.com/request``). Mirrors the pattern in
``tree.data.web.web_unlocker``: pure HTTP — no MongoDB, no Prefect, no
``Document``. Persistence and orchestration live elsewhere.

The SERP zone configured for this project (``cli_serp``) does NOT support
the ``brd_json=1`` parsed-JSON shortcut — when set, the response collapses
to a 226-byte metadata stub with no organic results (see tracker #010
diagnosis). We therefore request the rendered HTML SERP via
``data_format: "html"`` and extract organic results with BeautifulSoup.

Reference:
    .claude/skills/bright-data-best-practices/references/serp-api.md
"""

from __future__ import annotations

import logging
from typing import Literal
from urllib.parse import urlencode, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from tree.config.settings import settings
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)

logger = logging.getLogger(__name__)


class SearchResult(BaseModel):
    """A single organic SERP entry returned by Bright Data's SERP API."""

    rank: int = Field(
        ..., description="Position within the organic results, 1-indexed."
    )
    title: str
    url: str
    snippet: str = Field(
        default="", description="Description / page summary; may be empty."
    )


_BRIGHTDATA_REQUEST_URL = "https://api.brightdata.com/request"
_PAGE_SIZE = 10
_QUERY_LOG_MAX_CHARS = 100
_SNIPPET_MAX_CHARS = 300
_BODY_PREVIEW_MAX_CHARS = 200

# Substrings that indicate a well-formed SERP page that legitimately has no
# organic results (vs. a malformed / regression-shaped response). Matched
# case-insensitively. The list is intentionally small — we only need a single
# anchor to confirm "this is a real SERP, just empty".
_NO_RESULTS_INDICATORS = (
    "did not match any documents",
    "did not match any",
    "no results found",
    "ничего не нашлось",  # yandex
)

# Hosts whose links represent Google's own UI chrome (sign-in, account,
# webcache, image proxy, AMP redirector) rather than organic results.
_NON_ORGANIC_HOST_SUFFIXES = (
    "google.com",
    "googleusercontent.com",
    "gstatic.com",
    "youtube.com/redirect",
)

SearchEngine = Literal["google", "bing", "yandex"]


def _resolve_credentials() -> tuple[str, str]:
    """Read SERP credentials from settings, raising a clear error if missing."""

    api_key = settings.brightdata_api_key.get_secret_value()
    if not api_key:
        raise BrightDataConfigurationError("BRIGHTDATA_API_KEY is not set")

    zone = settings.brightdata_serp_zone
    if not zone:
        raise BrightDataConfigurationError("BRIGHTDATA_SERP_ZONE is not set")

    return api_key, zone


def _build_serp_url(
    *,
    query: str,
    engine: SearchEngine,
    country: str | None,
    language: str | None,
    offset: int,
) -> str:
    """Build the SERP URL Bright Data should fetch on our behalf.

    The URL is the public engine URL — no Bright Data-specific flags. The
    rendered HTML is what we parse downstream.
    """

    if engine == "google":
        params: list[tuple[str, str]] = [("q", query)]
        if country:
            params.append(("gl", country))
        if language:
            params.append(("hl", language))
        if offset > 0:
            params.append(("start", str(offset)))
        return f"https://www.google.com/search?{urlencode(params)}"

    if engine == "bing":
        params = [("q", query)]
        if country:
            params.append(("cc", country))
        if language:
            params.append(("setLang", language))
        # Bing's `first` is 1-indexed.
        params.append(("first", str(offset + 1)))
        return f"https://www.bing.com/search?{urlencode(params)}"

    if engine == "yandex":
        params = [("text", query)]
        if country:
            params.append(("lr", country))
        return f"https://yandex.com/search/?{urlencode(params)}"

    # The Literal type guards against this at type-check time, but be explicit
    # for runtime robustness.
    raise ValueError(f"Unsupported search engine: {engine!r}")


def _is_organic_url(url: str) -> bool:
    """Return True if ``url`` looks like an external organic result link.

    We exclude the engine's own infrastructure (sign-in, account, webcache,
    image proxy) so we don't surface Google's UI chrome as a "result".
    """

    if not url.startswith(("http://", "https://")):
        return False

    host = urlparse(url).hostname or ""
    host = host.lower()

    for suffix in _NON_ORGANIC_HOST_SUFFIXES:
        # Suffix may itself contain a path segment (e.g. youtube.com/redirect),
        # in which case match against host+path.
        if "/" in suffix:
            target = f"{host}{urlparse(url).path}"
            if target.startswith(suffix):
                return False
        elif host == suffix or host.endswith("." + suffix):
            return False

    return True


def _extract_snippet(anchor_tag, title: str) -> str:
    """Best-effort snippet extraction.

    Walk a few levels up from the title's anchor tag to find the result
    container, then return the visible text minus the title. Snippet is
    capped at ``_SNIPPET_MAX_CHARS`` to keep payloads small. Returns ``""``
    when the surrounding text is too short to plausibly be a snippet.
    """

    container = anchor_tag
    for _ in range(6):
        if container.parent is None:
            break
        container = container.parent

    text = container.get_text(" ", strip=True)
    if title and text.startswith(title):
        text = text[len(title) :].lstrip()

    # Drop runs of whitespace; bs4 ' ' separator already collapses, but
    # newlines from nested blocks can leak through on some Google variants.
    text = " ".join(text.split())

    if len(text) < 20:
        return ""

    if len(text) > _SNIPPET_MAX_CHARS:
        text = text[:_SNIPPET_MAX_CHARS].rstrip() + "..."

    return text


def _parse_serp_html(
    html: str,
    *,
    starting_rank: int,
) -> list[SearchResult]:
    """Extract organic results from a rendered SERP HTML body.

    Strategy: every ``<h3>`` that is the descendant of an ``<a href=...>``
    pointing to a non-engine external URL is an organic result. This is the
    most stable structural anchor across Google's frequent layout drift —
    Google's organic blocks consistently render the result title inside an
    ``h3`` wrapped in the destination link, while UI chrome links never
    wrap an ``h3``. The same pattern holds for Bing and Yandex, so the
    parser is engine-agnostic.

    Duplicates (same URL appearing twice — e.g. an ``h3`` plus a sitelink
    pointing to the same page) are dropped, keeping the first occurrence.
    """

    soup = BeautifulSoup(html, "html.parser")

    seen_urls: set[str] = set()
    results: list[SearchResult] = []
    next_rank = starting_rank

    for h3 in soup.find_all("h3"):
        anchor = h3.find_parent("a")
        if anchor is None:
            continue

        href = anchor.get("href") or ""
        if not _is_organic_url(href):
            continue

        if href in seen_urls:
            continue
        seen_urls.add(href)

        title = h3.get_text(strip=True)
        if not title:
            continue

        snippet = _extract_snippet(anchor, title)

        results.append(
            SearchResult(
                rank=next_rank,
                title=title,
                url=href,
                snippet=snippet,
            )
        )
        next_rank += 1

    return results


def _looks_like_legitimate_empty_serp(body: str) -> bool:
    """Return True if ``body`` looks like a real SERP page with no results.

    The heuristic is: the body either contains a known "no results"
    indicator string, OR it has SERP-shaped HTML structure (an ``h3``
    element anywhere, even one we didn't classify as organic) that the
    parser inspected and found empty of organic links. The first signal is
    the strong one — Google / Bing / Yandex all render explicit "no
    results" copy on a successful empty SERP.
    """

    if not body:
        return False

    lowered = body.lower()
    return any(indicator in lowered for indicator in _NO_RESULTS_INDICATORS)


def _parse_organic_or_warn(
    body: str,
    *,
    engine: SearchEngine,
    status: int,
    content_type: str,
    starting_rank: int,
    query_for_log: str,
) -> list[SearchResult]:
    """Parse organic results, classifying empty-result responses.

    Three branches:

    1. Parser returned ≥ 1 result → return them; no extra log line on the
       hot path (the function-level INFO at the start of ``search`` is
       enough on the success path).
    2. Parser returned 0 results AND the body carries a known "no results"
       indicator → INFO log, return ``[]``.
    3. Parser returned 0 results AND the body does NOT look like a SERP →
       WARNING log with diagnostic fields (engine, status, content type,
       200-char body preview), return ``[]``.

    Never raises. The public contract of ``search`` (return ``[]`` on
    empty, raise only on credential / input / non-2xx) is preserved.
    """

    parsed = _parse_serp_html(body, starting_rank=starting_rank)
    if parsed:
        return parsed

    if _looks_like_legitimate_empty_serp(body):
        logger.info(
            "SERP returned 0 organic results for query (engine=%s, query=%s)",
            engine,
            query_for_log,
        )
        return []

    body_preview = body[:_BODY_PREVIEW_MAX_CHARS]
    logger.warning(
        "SERP response had unexpected shape; returning [] "
        "(engine=%s, status=%d, content_type=%s, body_preview=%s)",
        engine,
        status,
        content_type,
        body_preview,
    )
    return []


async def search(
    query: str,
    *,
    engine: SearchEngine = "google",
    num_results: int = 10,
    country: str | None = None,
    language: str | None = None,
    timeout_seconds: float = 30.0,
) -> list[SearchResult]:
    """Run a SERP query via Bright Data's SERP API and return organic results.

    POSTs to ``https://api.brightdata.com/request`` with::

        Authorization: Bearer <BRIGHTDATA_API_KEY>
        json={
            "zone": <BRIGHTDATA_SERP_ZONE>,
            "url": <built SERP URL with locale + start>,
            "format": "raw",
            "data_format": "html",
        }

    Returns up to ``num_results`` organic entries. Pagination is handled
    internally via the engine's offset parameter (pages of 10). Empty result
    sets are returned as an empty list — never raised.

    Args:
        query: Non-empty search query.
        engine: ``"google"`` (default), ``"bing"``, or ``"yandex"``.
        num_results: Maximum number of organic results to return; must be ``>= 1``.
        country: Optional 2-letter ISO country code (passed to the engine's
            locale parameter — ``gl`` for Google, ``cc`` for Bing, ``lr`` for
            Yandex).
        language: Optional language code (``hl`` for Google, ``setLang`` for
            Bing). Yandex ignores this.
        timeout_seconds: Per-request timeout passed to ``httpx``.

    Raises:
        BrightDataConfigurationError: If ``BRIGHTDATA_API_KEY`` or
            ``BRIGHTDATA_SERP_ZONE`` is empty.
        BrightDataRequestError: On any non-2xx response (message includes the
            status code and the response body).
        ValueError: If ``query`` is empty / whitespace-only, or ``num_results``
            is ``< 1``.
    """

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must not be empty")

    if num_results < 1:
        raise ValueError("num_results must be >= 1")

    api_key, zone = _resolve_credentials()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    truncated_query = (
        query
        if len(query) <= _QUERY_LOG_MAX_CHARS
        else query[:_QUERY_LOG_MAX_CHARS] + "..."
    )
    logger.info(
        "Running SERP query via Bright Data (engine=%s, query=%s)",
        engine,
        truncated_query,
    )

    aggregated: list[SearchResult] = []
    offset = 0

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        while len(aggregated) < num_results:
            serp_url = _build_serp_url(
                query=query,
                engine=engine,
                country=country,
                language=language,
                offset=offset,
            )
            payload = {
                "zone": zone,
                "url": serp_url,
                "format": "raw",
                "data_format": "html",
            }

            response = await client.post(
                _BRIGHTDATA_REQUEST_URL,
                json=payload,
                headers=headers,
            )

            if response.status_code < 200 or response.status_code >= 300:
                raise BrightDataRequestError(
                    f"Bright Data SERP API returned HTTP {response.status_code}: "
                    f"{response.text}"
                )

            page_results = _parse_organic_or_warn(
                response.text,
                engine=engine,
                status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                starting_rank=len(aggregated) + 1,
                query_for_log=truncated_query,
            )
            aggregated.extend(page_results)

            # Stop when the engine returned fewer entries than a full page —
            # there are no more results to fetch.
            if len(page_results) < _PAGE_SIZE:
                break

            offset += _PAGE_SIZE

    return aggregated[:num_results]
