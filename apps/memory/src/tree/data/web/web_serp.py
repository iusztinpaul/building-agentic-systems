"""Bright Data SERP API HTTP client — Bing only.

Thin async wrapper around the Bright Data SERP REST endpoint
(``POST https://api.brightdata.com/request``). Mirrors the pattern in
``tree.data.web.web_unlocker``: pure HTTP — no MongoDB, no Prefect, no
``Document``. Persistence and orchestration live elsewhere.

The SERP zone configured for this project (``cli_serp``) does NOT support
the ``brd_json=1`` parsed-JSON shortcut — when set, the response collapses
to a 226-byte metadata stub with no organic results (see tracker #010
diagnosis). We therefore request the rendered HTML SERP via
``data_format: "html"`` and extract organic results with BeautifulSoup.

Why Bing (2026-10, ADR-013 §1): Google now wraps every organic link in an
encrypted ``/goto?url=<token>`` redirect in ALL Bright Data formats (raw
HTML, ``data_format: markdown``, ``format: json``), and neither the SERP nor
the Unlocker zone resolves ``/goto`` — so a Google SERP yields zero usable
URLs. Bing's organic results (``li.b_algo h2 a``) carry either the direct URL
or a ``bing.com/ck/a?…&u=a1<base64url>`` redirect that decodes locally to the
real URL. One engine, one parser.

Bright Data sometimes answers HTTP 200 with a plain-text cooldown sentence
("This query recently failed and cannot be attempted at this time…"). The
page is waited out ONCE (``_COOLDOWN_RETRY_SECONDS``) and re-requested; a
second cooldown raises :class:`BrightDataCooldownError`.

Reference:
    .agents/skills/bright-data-best-practices/references/serp-api.md
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import math
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup, Tag
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
# Bing serves a variable number of ``li.b_algo`` per page (5–10 observed on
# the same query), so pagination cannot stop on a short page. It stops on the
# SECOND consecutive page that adds no NEW url (Bing occasionally ignores
# ``first`` and re-serves page 1 once), and never fetches more than
# ``ceil(num_results / _PAGE_SIZE) + _EXTRA_PAGES`` pages.
_EXTRA_PAGES = 2
_MAX_CONSECUTIVE_NO_NEW_PAGES = 2
# Per-request httpx timeout. Bright Data SERP answers took 30–48 s on 6 of 23
# live calls (2026-10-03), so 30 s timed out too often.
_DEFAULT_TIMEOUT_SECONDS = 60.0

# Substrings that indicate a well-formed SERP page that legitimately has no
# organic results (vs. a malformed / regression-shaped response). Matched
# case-insensitively. The list is intentionally small — we only need a single
# anchor to confirm "this is a real SERP, just empty". Bing renders "There are
# no results for <query>"; the other two are generic fallbacks.
_NO_RESULTS_INDICATORS = (
    "there are no results for",
    "no results found",
    "did not match any",
)

# Hosts whose DIRECT links are Bing's own chrome (images / videos / maps
# verticals, ads) rather than organic results. ``microsoft.com`` is the apex
# only: ``learn.microsoft.com`` docs are legitimate organic results.
_BING_HOST = "bing.com"
_CHROME_EXACT_HOSTS = ("microsoft.com",)

# Bing's click-tracking redirect: ``/ck/a?…&u=a1<base64url of the real URL>``.
_BING_REDIRECT_PATH = "/ck/a"
_BING_REDIRECT_TOKEN_PREFIX = "a1"

# Bing appends ``msockid=<session id>`` to direct result hrefs — tracking
# noise that would defeat url dedup and pollute ingested source urls.
_TRACKING_QUERY_PARAMS = frozenset({"msockid"})

# Bright Data's 2xx "cooling down" answer — a plain-text sentence, not a SERP.
_COOLDOWN_MARKER = "This query recently failed and cannot be attempted at this time"
_COOLDOWN_RETRY_SECONDS = 15.0
_COOLDOWN_SCAN_CHARS = 300


class BrightDataCooldownError(BrightDataRequestError):
    """Bright Data answered its cooldown sentence twice for the same page.

    The message is Bright Data's own sentence. Retryable — after 15 s or more.
    """


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
    country: str | None,
    language: str | None,
    offset: int,
) -> str:
    """Build the Bing SERP URL Bright Data should fetch on our behalf.

    The URL is the public engine URL — no Bright Data-specific flags. The
    rendered HTML is what we parse downstream.
    """

    params: list[tuple[str, str]] = [("q", query)]
    if country:
        params.append(("cc", country))
    if language:
        params.append(("setLang", language))
    # Bing's `first` is 1-indexed.
    params.append(("first", str(offset + 1)))
    return f"https://www.bing.com/search?{urlencode(params)}"


def _is_chrome_host(host: str) -> bool:
    """Return True if ``host`` is Bing / Microsoft chrome rather than a result."""

    return (
        host == _BING_HOST
        or host.endswith("." + _BING_HOST)
        or host in _CHROME_EXACT_HOSTS
    )


def _decode_bing_redirect(href: str) -> str | None:
    """Decode a ``bing.com/ck/a?…&u=a1<base64url>`` redirect to its target URL."""

    tokens = parse_qs(urlparse(href).query).get("u")
    if not tokens or not tokens[0].startswith(_BING_REDIRECT_TOKEN_PREFIX):
        return None

    token = tokens[0][len(_BING_REDIRECT_TOKEN_PREFIX) :]
    # Bing strips the base64 ``=`` padding; restore it before decoding.
    token += "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(token).decode("utf-8")
    except binascii.Error, UnicodeDecodeError, ValueError:
        return None

    return decoded if decoded.startswith(("http://", "https://")) else None


def _strip_tracking_params(url: str) -> str:
    """Drop Bing's tracking query parameters (``msockid``), keeping all others."""

    parsed = urlparse(url)
    if not parsed.query:
        return url

    params = parse_qsl(parsed.query, keep_blank_values=True)
    kept = [(key, value) for key, value in params if key not in _TRACKING_QUERY_PARAMS]
    if len(kept) == len(params):
        return url

    return urlunparse(parsed._replace(query=urlencode(kept)))


def _resolve_bing_href(href: str) -> str | None:
    """Return the real destination of a Bing result link, or ``None`` to skip it.

    - A direct ``http(s)://`` href whose host is not ``bing.com`` /
      ``*.bing.com`` / ``microsoft.com`` → itself.
    - A ``bing.com/ck/a?…&u=a1<token>`` redirect → the base64url-decoded
      token, kept only if it is an ``http(s)://`` URL.
    - Anything else (relative vertical links, ads, ``javascript:``) → ``None``.

    The tracking parameter ``msockid`` is stripped from the resolved url.
    """

    if not href.startswith(("http://", "https://")):
        return None

    parsed = urlparse(href)
    host = (parsed.hostname or "").lower()

    if not _is_chrome_host(host):
        return _strip_tracking_params(href)

    if parsed.path == _BING_REDIRECT_PATH:
        decoded = _decode_bing_redirect(href)
        return _strip_tracking_params(decoded) if decoded is not None else None

    return None


def _clean_snippet(text: str) -> str:
    """Collapse whitespace and cap at ``_SNIPPET_MAX_CHARS``."""

    text = " ".join(text.split())
    if len(text) > _SNIPPET_MAX_CHARS:
        text = text[:_SNIPPET_MAX_CHARS].rstrip() + "..."
    return text


def _extract_snippet(result: Tag, title: str) -> str:
    """Best-effort snippet for one ``li.b_algo``.

    ``.b_caption p`` is the classic layout; the image-caption layout puts the
    snippet ``p`` beside the title with an empty ``.b_caption``, so any ``p``
    in the result is the second choice. Last resort: the result's own text
    minus the title — bounded to the ``li`` so it never bleeds into the next
    result.
    """

    paragraph = result.select_one(".b_caption p") or result.select_one("p")
    if paragraph is not None:
        text = paragraph.get_text(" ", strip=True)
        if text:
            return _clean_snippet(text)

    text = result.get_text(" ", strip=True)
    if title and text.startswith(title):
        text = text[len(title) :].lstrip()
    return _clean_snippet(text)


def _parse_serp_html(
    html: str,
    *,
    starting_rank: int,
    seen_urls: set[str],
) -> tuple[list[SearchResult], int]:
    """Extract organic results from a rendered Bing SERP HTML body.

    One result per ``li.b_algo`` whose ``h2 > a`` href resolves (see
    :func:`_resolve_bing_href`). ``h2 a`` links outside ``li.b_algo`` (top
    answers, ads) are never considered. Duplicates are dropped on the
    RESOLVED url, keeping the first occurrence; ranks are positional from
    ``starting_rank``.

    ``seen_urls`` is shared across the pages of one search and updated in
    place, so a url already returned by an earlier page is dropped too.

    Returns the new results and the number of results dropped as duplicates.
    """

    soup = BeautifulSoup(html, "html.parser")

    results: list[SearchResult] = []
    duplicates = 0
    next_rank = starting_rank

    for result in soup.select("li.b_algo"):
        anchor = result.select_one("h2 > a")
        if anchor is None:
            continue

        url = _resolve_bing_href(str(anchor.get("href") or ""))
        if url is None:
            continue
        if url in seen_urls:
            duplicates += 1
            continue

        title = anchor.get_text(strip=True)
        if not title:
            continue
        seen_urls.add(url)

        results.append(
            SearchResult(
                rank=next_rank,
                title=title,
                url=url,
                snippet=_extract_snippet(result, title),
            )
        )
        next_rank += 1

    return results, duplicates


def _is_cooldown_body(text: str) -> bool:
    """Return True if ``text`` is Bright Data's 2xx "query cooling down" answer.

    Only the head of the body is inspected: the cooldown answer is the
    sentence alone, while a real SERP that happens to quote it is long HTML.
    """

    head = text[:_COOLDOWN_SCAN_CHARS].lower()
    return _COOLDOWN_MARKER.lower() in head


def _looks_like_legitimate_empty_serp(body: str) -> bool:
    """Return True if ``body`` is a real SERP page carrying "no results" copy.

    Bing renders an explicit "There are no results for …" line on a
    successful empty SERP; that indicator is what separates "really empty"
    from "unexpected shape".
    """

    if not body:
        return False

    lowered = body.lower()
    return any(indicator in lowered for indicator in _NO_RESULTS_INDICATORS)


def _parse_organic_or_warn(
    body: str,
    *,
    status: int,
    content_type: str,
    starting_rank: int,
    seen_urls: set[str],
    query_for_log: str,
) -> list[SearchResult]:
    """Parse organic results, classifying empty-result responses.

    Four branches:

    1. Parser returned ≥ 1 result → return them; no extra log line on the
       hot path (the function-level INFO at the start of ``search`` is
       enough on the success path).
    2. Parser returned 0 NEW results because every result was already seen
       on an earlier page → INFO log, return ``[]``.
    3. Parser returned 0 results AND the body carries a known "no results"
       indicator → INFO log, return ``[]``.
    4. Parser returned 0 results AND the body does NOT look like a SERP →
       WARNING log with diagnostic fields (status, content type, 200-char
       body preview), return ``[]``.

    Never raises. The public contract of ``search`` (return ``[]`` on
    empty, raise only on credential / input / non-2xx / repeated cooldown)
    is preserved.
    """

    parsed, duplicates = _parse_serp_html(
        body, starting_rank=starting_rank, seen_urls=seen_urls
    )
    if parsed:
        return parsed

    if duplicates:
        logger.info(
            "SERP page added no new results — all %d already seen (query=%s)",
            duplicates,
            query_for_log,
        )
        return []

    if _looks_like_legitimate_empty_serp(body):
        logger.info(
            "SERP returned 0 organic results for query (query=%s)",
            query_for_log,
        )
        return []

    body_preview = body[:_BODY_PREVIEW_MAX_CHARS]
    logger.warning(
        "SERP response had unexpected shape; returning [] "
        "(status=%d, content_type=%s, body_preview=%s)",
        status,
        content_type,
        body_preview,
    )
    return []


async def _post_serp_page(
    client: httpx.AsyncClient,
    *,
    payload: dict[str, str],
    headers: dict[str, str],
) -> httpx.Response:
    """POST one SERP page request, raising on any non-2xx status."""

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

    return response


async def _fetch_serp_page(
    client: httpx.AsyncClient,
    *,
    payload: dict[str, str],
    headers: dict[str, str],
    query_for_log: str,
    offset: int,
) -> httpx.Response:
    """Fetch one SERP page, waiting out ONE Bright Data cooldown answer.

    A 2xx cooldown body is logged, slept on for ``_COOLDOWN_RETRY_SECONDS``
    and re-requested once; a second cooldown raises
    :class:`BrightDataCooldownError` carrying Bright Data's sentence.
    """

    response = await _post_serp_page(client, payload=payload, headers=headers)
    if not _is_cooldown_body(response.text):
        return response

    logger.warning(
        "SERP cooldown for query=%s — retrying once in %.1fs (offset=%d)",
        query_for_log,
        _COOLDOWN_RETRY_SECONDS,
        offset,
    )
    await asyncio.sleep(_COOLDOWN_RETRY_SECONDS)

    response = await _post_serp_page(client, payload=payload, headers=headers)
    if _is_cooldown_body(response.text):
        raise BrightDataCooldownError(response.text.strip()[:_COOLDOWN_SCAN_CHARS])

    return response


async def search(
    query: str,
    *,
    num_results: int = 10,
    country: str | None = None,
    language: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[SearchResult]:
    """Run a Bing query via Bright Data's SERP API and return organic results.

    POSTs to ``https://api.brightdata.com/request`` with::

        Authorization: Bearer <BRIGHTDATA_API_KEY>
        json={
            "zone": <BRIGHTDATA_SERP_ZONE>,
            "url": "https://www.bing.com/search?q=…&first=<offset+1>",
            "format": "raw",
            "data_format": "html",
        }

    Returns up to ``num_results`` organic entries, unique by url. Pagination
    is handled internally via Bing's ``first`` parameter, advanced by 10 per
    page: it continues until ``num_results`` is reached, a legitimate
    "no results" page, two consecutive pages that add no new url, or
    ``ceil(num_results / 10) + 2`` pages were fetched. Empty result sets are
    returned as an empty list — never raised. If a later page stays in
    cooldown after its retry, times out or cannot connect, the results
    already collected are returned (WARNING logged); on the first page those
    errors raise.

    Args:
        query: Non-empty search query.
        num_results: Maximum number of organic results to return; must be ``>= 1``.
        country: Optional 2-letter ISO country code (Bing's ``cc``).
        language: Optional language, mapped to Bing's ``setLang`` (e.g.
            ``en`` or ``en-US``).
        timeout_seconds: Per-request timeout passed to ``httpx``
            (default ``_DEFAULT_TIMEOUT_SECONDS``, 60 s).

    Raises:
        BrightDataConfigurationError: If ``BRIGHTDATA_API_KEY`` or
            ``BRIGHTDATA_SERP_ZONE`` is empty.
        BrightDataCooldownError: If Bright Data answers its cooldown sentence
            for the first page twice, ``_COOLDOWN_RETRY_SECONDS`` apart.
        BrightDataRequestError: On any non-2xx response (message includes the
            status code and the response body).
        httpx.TimeoutException, httpx.ConnectError: If the first page times
            out or cannot connect.
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
    logger.info("Running Bing SERP query via Bright Data (query=%s)", truncated_query)

    aggregated: list[SearchResult] = []
    seen_urls: set[str] = set()
    consecutive_no_new_pages = 0
    max_pages = math.ceil(num_results / _PAGE_SIZE) + _EXTRA_PAGES

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for page_index in range(max_pages):
            if len(aggregated) >= num_results:
                break

            # Bing's `first` indexes its slot grid (10 per page), not our
            # parsed list: rich blocks occupy slots, so a page often yields
            # fewer than 10 `b_algo`. Advancing by what was served instead
            # re-requests slots already seen (measured live, 2026-10-03).
            offset = page_index * _PAGE_SIZE
            serp_url = _build_serp_url(
                query=query,
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

            try:
                response = await _fetch_serp_page(
                    client,
                    payload=payload,
                    headers=headers,
                    query_for_log=truncated_query,
                    offset=offset,
                )
            except (
                BrightDataCooldownError,
                httpx.TimeoutException,
                httpx.ConnectError,
            ) as exc:
                if not aggregated:
                    raise
                logger.warning(
                    "SERP page failed for query=%s at offset=%d (%s) — "
                    "returning the %d result(s) already collected",
                    truncated_query,
                    offset,
                    type(exc).__name__,
                    len(aggregated),
                )
                break

            page_results = _parse_organic_or_warn(
                response.text,
                status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                starting_rank=len(aggregated) + 1,
                seen_urls=seen_urls,
                query_for_log=truncated_query,
            )
            if page_results:
                consecutive_no_new_pages = 0
                aggregated.extend(page_results)
                continue

            # No NEW url. Bing's "no results" page is the end of the SERP;
            # any other no-new page may be Bing re-serving an earlier page
            # once, so only a second one in a row stops pagination.
            consecutive_no_new_pages += 1
            if (
                _looks_like_legitimate_empty_serp(response.text)
                or consecutive_no_new_pages >= _MAX_CONSECUTIVE_NO_NEW_PAGES
            ):
                break

    return aggregated[:num_results]
