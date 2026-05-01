"""
On-demand scrape of one or more URLs via Bright Data Web Unlocker.

Sanity-check companion to the ``scrape_web`` MCP tool. Reuses the tool's
private ``_scrape_one`` helper directly so the CLI and the MCP path share a
single source of truth for the per-URL contract (error taxonomy, truncation,
result envelope shape).

Read-only: does NOT write to MongoDB and does NOT trigger memory extraction.
Use ``ingest_url`` (or its CLI equivalents) afterwards to persist a URL.

Usage:
    uv run python scripts/scrape_web.py --urls "https://example.com"
    uv run python scripts/scrape_web.py --urls "https://a,https://b" --max-chars 500
    uv run python scripts/scrape_web.py --urls "https://example.com" --data-format html
"""

from __future__ import annotations

import asyncio
import json
import logging

import click

from tree.data.web.web_scrape import (
    DEFAULT_MAX_CHARS,
    MAX_URLS_PER_CALL,
    scrape_one,
)
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)


def _parse_urls(raw: str) -> list[str]:
    """Parse the ``--urls`` comma-separated value into a clean list."""

    return [item.strip() for item in raw.split(",") if item.strip()]


async def _run(
    urls: list[str],
    data_format: str,
    max_chars: int | None,
    timeout_seconds: float,
) -> int:
    """Scrape each URL concurrently and print the JSON envelope.

    Returns the desired CLI exit code.
    """

    if not urls:
        logger.error("Invalid input: --urls is empty")
        return 1

    if len(urls) > MAX_URLS_PER_CALL:
        logger.error(
            "Invalid input: max %d urls per call (got %d)",
            MAX_URLS_PER_CALL,
            len(urls),
        )
        return 1

    if max_chars is not None and max_chars < 1:
        logger.error("Invalid input: --max-chars must be >= 1 (or omit to disable)")
        return 1

    results = await asyncio.gather(
        *[
            scrape_one(
                u,
                data_format=data_format,  # type: ignore[arg-type]
                max_chars=max_chars,
                timeout_seconds=timeout_seconds,
            )
            for u in urls
        ]
    )

    succeeded = sum(1 for r in results if r["success"])

    payload: dict[str, object] = {
        "requested": len(urls),
        "succeeded": succeeded,
        "failed": len(urls) - succeeded,
        "results": results,
    }

    logger.info(
        "Scraped %d URL(s): %d succeeded, %d failed",
        len(urls),
        succeeded,
        len(urls) - succeeded,
    )
    logger.info("%s", json.dumps(payload, indent=2))
    return 0


@click.command()
@click.option(
    "--urls",
    "-u",
    required=True,
    help="Comma-separated absolute http:// or https:// URLs (max 5).",
)
@click.option(
    "--data-format",
    type=click.Choice(["markdown", "html"]),
    default="markdown",
    show_default=True,
    help="Response format: markdown (best for LLM input) or raw HTML.",
)
@click.option(
    "--max-chars",
    type=int,
    default=DEFAULT_MAX_CHARS,
    show_default=True,
    help="Per-URL truncation cap (set to 0 to disable).",
)
@click.option(
    "--timeout",
    type=float,
    default=60.0,
    show_default=True,
    help="Per-URL HTTP timeout in seconds.",
)
def main(
    urls: str,
    data_format: str,
    max_chars: int,
    timeout: float,
) -> None:
    """Scrape one or more URLs via Bright Data Web Unlocker. No ingestion."""

    parsed_urls = _parse_urls(urls)
    parsed_max_chars: int | None = max_chars if max_chars > 0 else None

    exit_code = asyncio.run(
        _run(
            parsed_urls,
            data_format,
            parsed_max_chars,
            timeout,
        )
    )
    if exit_code != 0:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
