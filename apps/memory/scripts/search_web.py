"""
On-demand web search via Bright Data's SERP API.

Sanity-check companion to the ``search_web`` MCP tool. Calls the same
``tree.data.web.web_serp.search`` client directly — no MongoDB, no extraction.
Optionally fires the ``ingest-web-url-batch-etl`` Prefect deployment when
``--ingest`` is passed.

Usage:
    uv run python scripts/search_web.py --query "knowledge graphs"
    uv run python scripts/search_web.py --query "Prefect 3" --engine google --num-results 5
    uv run python scripts/search_web.py --query "datenschutz" --country de --language de
    uv run python scripts/search_web.py --query "agent tool use" --ingest --ingest-top-k 3
    uv run python scripts/search_web.py --query "x" --ingest --ingest-urls "https://a,https://b"
"""

from __future__ import annotations

import asyncio
import json
import logging

import click

from tree.data.web.web_search_ingest import trigger_url_batch_ingest
from tree.data.web.web_serp import search as web_search
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)


def _parse_ingest_urls(raw: str | None) -> list[str] | None:
    """Parse the ``--ingest-urls`` comma-separated value. Returns None if unset."""

    if raw is None:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


async def _run(
    query: str,
    engine: str,
    num_results: int,
    country: str | None,
    language: str | None,
    ingest: bool,
    ingest_top_k: int | None,
    ingest_urls: list[str] | None,
) -> int:
    """Run the SERP query and (optionally) fire the ingest deployment.

    Returns the desired CLI exit code.
    """

    # Validate ingest flag combinations BEFORE making the SERP call.
    if not ingest and (ingest_top_k is not None or ingest_urls is not None):
        logger.error("Invalid input: --ingest-urls/--ingest-top-k requires --ingest")
        return 1

    if ingest and ingest_urls is not None and len(ingest_urls) == 0:
        logger.error("Invalid input: --ingest-urls is empty")
        return 1

    if ingest_top_k is not None and ingest_top_k < 1:
        logger.error(
            "Invalid input: --ingest-top-k must be >= 1 (got %d); omit it to ingest "
            "all SERP results",
            ingest_top_k,
        )
        return 1

    try:
        results = await web_search(
            query,
            engine=engine,  # type: ignore[arg-type]
            num_results=num_results,
            country=country,
            language=language,
        )
    except ValueError as exc:
        logger.error("Invalid input: %s", exc)
        return 1
    except BrightDataConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        return 1
    except BrightDataRequestError as exc:
        logger.error("SERP request failed: %s", exc)
        return 1

    if not results:
        logger.info("No results for query=%r (engine=%s)", query, engine)
    else:
        logger.info(
            "Got %d result(s) for query=%r (engine=%s):", len(results), query, engine
        )
        for r in results:
            logger.info("[%d] %s — %s", r.rank, r.title, r.url)

    payload: dict[str, object] = {
        "query": query,
        "engine": engine,
        "results": [r.model_dump() for r in results],
    }

    if ingest:
        payload["ingest"] = await _maybe_ingest(results, ingest_top_k, ingest_urls)

    logger.info("%s", json.dumps(payload, indent=2))
    return 0


async def _maybe_ingest(
    results: list,
    ingest_top_k: int | None,
    ingest_urls: list[str] | None,
) -> dict[str, object]:
    """Pick URLs and fire-and-forget the batch ingest deployment.

    Search succeeded by the time we get here — never let an ingestion failure
    flip the CLI's exit code.
    """

    if ingest_urls is not None:
        selected: list[str] = list(ingest_urls)
    elif ingest_top_k is not None:
        selected = [r.url for r in results[:ingest_top_k]]
    else:
        selected = [r.url for r in results]

    if not selected:
        logger.info("No URLs to ingest.")
        return {
            "triggered": False,
            "urls": [],
            "detail": "no urls to ingest",
        }

    try:
        trigger = await trigger_url_batch_ingest(selected)
    except Exception as exc:  # noqa: BLE001 — best-effort.
        logger.error("Failed to trigger ingest-web-url-batch-etl: %s", exc)
        return {
            "triggered": False,
            "urls": selected,
            "error": str(exc),
        }

    logger.info(
        "Triggered ingest-web-url-batch-etl: flow_run_id=%s urls=%d",
        trigger["flow_run_id"],
        len(selected),
    )
    return {
        "triggered": True,
        "urls": selected,
        "flow_run_id": trigger["flow_run_id"],
        "tracking_url": trigger["tracking_url"],
    }


@click.command()
@click.option(
    "--query",
    "-q",
    required=True,
    help="Search query.",
)
@click.option(
    "--engine",
    "-e",
    type=click.Choice(["google", "bing", "yandex"]),
    default="google",
    show_default=True,
    help="Search engine to query.",
)
@click.option(
    "--num-results",
    "-n",
    type=int,
    default=10,
    show_default=True,
    help="Maximum number of organic results to return.",
)
@click.option(
    "--country",
    "-c",
    default=None,
    help="Optional 2-letter ISO country code for geo-targeting (e.g. 'us').",
)
@click.option(
    "--language",
    "-l",
    default=None,
    help="Optional 2-letter language code (e.g. 'en').",
)
@click.option(
    "--ingest",
    is_flag=True,
    default=False,
    help="Fire-and-forget the ingest-web-url-batch-etl Prefect deployment with the selected URLs.",
)
@click.option(
    "--ingest-top-k",
    type=int,
    default=None,
    help="When --ingest is set, ingest only the first K SERP URLs.",
)
@click.option(
    "--ingest-urls",
    default=None,
    help=(
        "When --ingest is set, ingest exactly these URLs (comma-separated). "
        "Overrides --ingest-top-k."
    ),
)
def main(
    query: str,
    engine: str,
    num_results: int,
    country: str | None,
    language: str | None,
    ingest: bool,
    ingest_top_k: int | None,
    ingest_urls: str | None,
) -> None:
    """Run a Bright Data SERP search and print results."""

    parsed_ingest_urls = _parse_ingest_urls(ingest_urls)

    exit_code = asyncio.run(
        _run(
            query,
            engine,
            num_results,
            country,
            language,
            ingest,
            ingest_top_k,
            parsed_ingest_urls,
        )
    )
    if exit_code != 0:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
