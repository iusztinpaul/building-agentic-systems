"""Bright Data SERP API HTTP client.

Thin async wrapper around the Bright Data SERP REST endpoint
(``POST https://api.brightdata.com/request``). Mirrors the pattern in
``tree.data.web.web_unlocker``: pure HTTP — no MongoDB, no Prefect, no
``Document``. Persistence and orchestration live elsewhere.

Reference:
    .claude/skills/bright-data-best-practices/references/serp-api.md
"""

from __future__ import annotations

import logging
from typing import Literal
from urllib.parse import urlencode

import httpx

from tree.config.settings import settings
from tree.data.web.types import SearchResult
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)

logger = logging.getLogger(__name__)

_BRIGHTDATA_REQUEST_URL = "https://api.brightdata.com/request"
_PAGE_SIZE = 10
_QUERY_LOG_MAX_CHARS = 100

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

    Always passes ``brd_json=1`` so Bright Data returns parsed JSON instead
    of raw HTML.
    """

    if engine == "google":
        params: list[tuple[str, str]] = [("q", query), ("brd_json", "1")]
        if country:
            params.append(("gl", country))
        if language:
            params.append(("hl", language))
        if offset > 0:
            params.append(("start", str(offset)))
        return f"https://www.google.com/search?{urlencode(params)}"

    if engine == "bing":
        params = [("q", query), ("brd_json", "1")]
        if country:
            params.append(("cc", country))
        if language:
            params.append(("setLang", language))
        # Bing's `first` is 1-indexed.
        params.append(("first", str(offset + 1)))
        return f"https://www.bing.com/search?{urlencode(params)}"

    if engine == "yandex":
        params = [("text", query), ("brd_json", "1")]
        if country:
            params.append(("lr", country))
        return f"https://yandex.com/search/?{urlencode(params)}"

    # The Literal type guards against this at type-check time, but be explicit
    # for runtime robustness.
    raise ValueError(f"Unsupported search engine: {engine!r}")


def _parse_organic(
    organic: list[dict],
    *,
    starting_rank: int,
) -> list[SearchResult]:
    """Map a SERP API ``organic`` payload to ``SearchResult`` instances.

    Entries with no ``link`` are skipped defensively. ``starting_rank`` is the
    1-indexed rank to assign to the first kept entry when the upstream entry
    lacks an explicit ``rank`` field.
    """

    results: list[SearchResult] = []
    next_rank = starting_rank
    for entry in organic:
        link = entry.get("link") or ""
        if not link:
            continue

        rank_value = entry.get("rank")
        rank = int(rank_value) if isinstance(rank_value, int) else next_rank

        results.append(
            SearchResult(
                rank=rank,
                title=entry.get("title") or "",
                url=link,
                snippet=entry.get("description") or "",
            )
        )
        next_rank += 1

    return results


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
            "url": <built SERP URL with brd_json=1 + locale + start>,
            "format": "raw",
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

            data = response.json()
            organic = data.get("organic") or []

            page_results = _parse_organic(organic, starting_rank=len(aggregated) + 1)
            aggregated.extend(page_results)

            # Stop when the engine returned fewer entries than a full page —
            # there are no more results to fetch.
            if len(organic) < _PAGE_SIZE:
                break

            offset += _PAGE_SIZE

    return aggregated[:num_results]
