"""
On-demand web search via Bright Data's SERP API.

Sanity-check companion to the ``search_web`` MCP tool. Calls the same
``tree.data.web.web_serp.search`` client directly — no Prefect, no MongoDB,
no ingestion. Useful for verifying that ``BRIGHTDATA_SERP_ZONE`` is wired
correctly and the agent's view of search results matches what we expect.

Usage:
    uv run python scripts/search_web.py --query "knowledge graphs"
    uv run python scripts/search_web.py --query "Prefect 3" --engine google --num-results 5
    uv run python scripts/search_web.py --query "datenschutz" --country de --language de
"""

from __future__ import annotations

import asyncio
import json
import logging

import click

from tree.data.web.web_serp import search as web_search
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)


async def _run(
    query: str,
    engine: str,
    num_results: int,
    country: str | None,
    language: str | None,
) -> int:
    """Run the SERP query and log results. Returns the desired exit code."""

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

    payload = {
        "query": query,
        "engine": engine,
        "results": [r.model_dump() for r in results],
    }
    logger.info("%s", json.dumps(payload, indent=2))
    return 0


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
def main(
    query: str,
    engine: str,
    num_results: int,
    country: str | None,
    language: str | None,
) -> None:
    """Run a Bright Data SERP search and print results."""

    exit_code = asyncio.run(_run(query, engine, num_results, country, language))
    if exit_code != 0:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
