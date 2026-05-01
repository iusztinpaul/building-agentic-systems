"""Diagnose ``search_web`` empty-results behaviour against the live Bright Data SERP API.

Investigation harness for tracker task #010. Runs three back-to-back probes
against the live Bright Data SERP zone configured in ``.env`` and logs the
shape of each response so we can decide which failure mode (a)-(e) from the
task spec is the real one.

This is a one-shot diagnostic — exit code is 0 even on failure paths so the
operator can capture all three probes in one run. Each probe makes exactly
one HTTP call (no retries), so a full run consumes 3 SERP credits.

The API key is never logged in raw form: only its presence and length.

Usage:
    uv --directory apps/memory run python scripts/diagnose_search_web.py
    uv --directory apps/memory run python scripts/diagnose_search_web.py --query "Harness Engineering"
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import click
import httpx

from tree.config.settings import settings
from tree.data.web.web_serp import search as web_search
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

_BRIGHTDATA_REQUEST_URL = "https://api.brightdata.com/request"
_BODY_PREVIEW_CHARS = 500
_PLACEHOLDER_API_KEY = "your-brightdata-api-key"
_PLACEHOLDER_SERP_ZONE = "your-brightdata-serp-zone"


def _resolve_live_credentials() -> tuple[str, str]:
    """Resolve the live SERP credentials, refusing to run on the placeholders."""

    api_key = settings.brightdata_api_key.get_secret_value()
    zone = settings.brightdata_serp_zone

    if not api_key or api_key == _PLACEHOLDER_API_KEY:
        raise SystemExit(
            "BRIGHTDATA_API_KEY is empty or still set to the .env.example "
            "placeholder. Set a real key in .env before running this diagnostic."
        )
    if not zone or zone == _PLACEHOLDER_SERP_ZONE:
        raise SystemExit(
            "BRIGHTDATA_SERP_ZONE is empty or still set to the .env.example "
            "placeholder. Set a real zone in .env before running this diagnostic."
        )

    return api_key, zone


def _classify_body(body_text: str) -> str:
    """Best-effort label for the response body shape."""

    head = body_text.lstrip().lower()
    if not head:
        return "empty"
    if (
        head.startswith("<!doctype")
        or head.startswith("<html")
        or "<html" in head[:200]
    ):
        return "html"
    if head.startswith("{") or head.startswith("["):
        return "json-shaped"
    return "other"


def _truncate(body_text: str, limit: int = _BODY_PREVIEW_CHARS) -> str:
    """Keep terminals readable — never dump 200 KB of HTML."""

    if len(body_text) <= limit:
        return body_text
    return body_text[:limit] + f"... [truncated, total length={len(body_text)}]"


async def _probe_1_production_path(query: str) -> None:
    """Probe 1: call the production code path exactly as the MCP tool does."""

    logger.info("=" * 72)
    logger.info("PROBE 1 — production code path (tree.data.web.search)")
    logger.info("=" * 72)
    try:
        results = await web_search(query, engine="google", num_results=10)
    except (BrightDataConfigurationError, BrightDataRequestError, ValueError) as exc:
        logger.info("Probe 1 raised %s: %s", type(exc).__name__, exc)
        return
    except Exception as exc:  # noqa: BLE001 — diagnostic must not crash mid-run.
        logger.info(
            "Probe 1 raised unexpected %s: %s",
            type(exc).__name__,
            exc,
        )
        return

    logger.info("Probe 1 returned len(results)=%d", len(results))
    for r in results[:3]:
        logger.info("  [%d] %s — %s", r.rank, r.title, r.url)


async def _probe_post(
    *,
    label: str,
    api_key: str,
    zone: str,
    serp_url: str,
) -> None:
    """Shared body for probes 2 and 3 — single POST with logged shape."""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"zone": zone, "url": serp_url, "format": "raw"}

    logger.info("%s — POST %s", label, _BRIGHTDATA_REQUEST_URL)
    logger.info("%s — payload.url=%s", label, serp_url)
    logger.info("%s — payload.zone=%s payload.format=%s", label, zone, "raw")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                _BRIGHTDATA_REQUEST_URL,
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            logger.info("%s — httpx error: %s: %s", label, type(exc).__name__, exc)
            return

    body_text = response.text
    content_type = response.headers.get("content-type", "<missing>")
    shape = _classify_body(body_text)

    logger.info(
        "%s — status=%d content-type=%s body_len=%d shape=%s",
        label,
        response.status_code,
        content_type,
        len(body_text),
        shape,
    )
    logger.info("%s — body[:%d]:", label, _BODY_PREVIEW_CHARS)
    logger.info("%s", _truncate(body_text))

    # Always attempt JSON parse for both probes — informative either way.
    try:
        parsed: Any = response.json()
    except ValueError as exc:
        logger.info(
            "%s — response.json() FAILED with %s: %s",
            label,
            type(exc).__name__,
            exc,
        )
        return

    if isinstance(parsed, dict):
        keys = sorted(parsed.keys())
        logger.info("%s — response.json() succeeded; top_level_keys=%s", label, keys)
        organic = parsed.get("organic")
        if isinstance(organic, list):
            logger.info("%s — len(parsed['organic'])=%d", label, len(organic))
            if organic:
                first = organic[0]
                if isinstance(first, dict):
                    logger.info(
                        "%s — organic[0] sample keys=%s",
                        label,
                        sorted(first.keys()),
                    )
        else:
            logger.info(
                "%s — parsed['organic'] type=%s (n/a)", label, type(organic).__name__
            )
    elif isinstance(parsed, list):
        logger.info(
            "%s — response.json() succeeded; top-level is list, len=%d",
            label,
            len(parsed),
        )
    else:
        logger.info(
            "%s — response.json() succeeded; top-level type=%s",
            label,
            type(parsed).__name__,
        )


async def _probe_2_no_brd_json(query: str, api_key: str, zone: str) -> None:
    """Probe 2: replicate the user's working curl exactly — no ``brd_json``."""

    logger.info("=" * 72)
    logger.info("PROBE 2 — user's working curl shape (no brd_json=1)")
    logger.info("=" * 72)
    serp_url = f"https://www.google.com/search?q={query}"
    await _probe_post(label="Probe 2", api_key=api_key, zone=zone, serp_url=serp_url)


async def _probe_3_with_brd_json(query: str, api_key: str, zone: str) -> None:
    """Probe 3: replicate the production code's request shape (``brd_json=1``)."""

    logger.info("=" * 72)
    logger.info("PROBE 3 — production code shape (brd_json=1)")
    logger.info("=" * 72)
    serp_url = f"https://www.google.com/search?q={query}&brd_json=1"
    await _probe_post(label="Probe 3", api_key=api_key, zone=zone, serp_url=serp_url)


async def _run(query: str) -> None:
    api_key, zone = _resolve_live_credentials()
    logger.info(
        "BRIGHTDATA_API_KEY configured (length=%d); BRIGHTDATA_SERP_ZONE=%s",
        len(api_key),
        zone,
    )
    logger.info("Diagnostic query=%r", query)

    await _probe_1_production_path(query)
    await _probe_2_no_brd_json(query, api_key, zone)
    await _probe_3_with_brd_json(query, api_key, zone)

    logger.info("=" * 72)
    logger.info("Done. Three probes complete.")


@click.command()
@click.option(
    "--query",
    "-q",
    default="pizza",
    show_default=True,
    help="Query to send to all three probes.",
)
def main(query: str) -> None:
    """Run all three SERP diagnostic probes back-to-back."""

    asyncio.run(_run(query))


if __name__ == "__main__":
    main()
