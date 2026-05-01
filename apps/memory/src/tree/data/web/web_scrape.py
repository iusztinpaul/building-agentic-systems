"""Multi-URL scrape over the Bright Data Web Unlocker.

Pure helper layer used by the ``scrape_web`` MCP tool and its CLI sibling
(``scripts/scrape_web.py``). No MongoDB, no Prefect, no FastMCP — just
``fetch_url`` calls fanned out with ``asyncio.gather`` and an exception
table that maps every failure mode to a typed error envelope.

The module exists to keep the CLI from pulling FastMCP into its import
graph (FastMCP reconfigures the root logger, which suppresses the CLI's
INFO output).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import httpx

from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
    fetch_url,
)

logger = logging.getLogger(__name__)


MAX_URLS_PER_CALL = 5
DEFAULT_MAX_CHARS = 30_000


async def scrape_one(
    url: str,
    *,
    data_format: Literal["markdown", "html"],
    max_chars: int | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Scrape one URL, mapping every failure mode to a typed error envelope.

    Always returns a dict — never raises. The dict shape is identical for
    success and failure cases so callers can iterate uniformly.
    """

    base: dict[str, Any] = {
        "url": url,
        "data_format": data_format,
    }

    try:
        content = await fetch_url(
            url,
            data_format=data_format,
            timeout_seconds=timeout_seconds,
        )
    except ValueError as exc:
        logger.warning(
            "Scrape failed for %s: error_type=%s detail=%s",
            url,
            "invalid_input",
            exc,
        )
        return {
            **base,
            "success": False,
            "content": None,
            "length": None,
            "truncated": None,
            "error": str(exc),
            "error_type": "invalid_input",
        }
    except BrightDataConfigurationError as exc:
        logger.warning(
            "Scrape failed for %s: error_type=%s detail=%s",
            url,
            "configuration_error",
            exc,
        )
        return {
            **base,
            "success": False,
            "content": None,
            "length": None,
            "truncated": None,
            "error": str(exc),
            "error_type": "configuration_error",
        }
    except BrightDataRequestError as exc:
        logger.warning(
            "Scrape failed for %s: error_type=%s detail=%s",
            url,
            "fetch_failed",
            exc,
        )
        return {
            **base,
            "success": False,
            "content": None,
            "length": None,
            "truncated": None,
            "error": str(exc),
            "error_type": "fetch_failed",
        }
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code} from Bright Data Web Unlocker"
        logger.warning(
            "Scrape failed for %s: error_type=%s detail=%s",
            url,
            "http_error",
            detail,
        )
        return {
            **base,
            "success": False,
            "content": None,
            "length": None,
            "truncated": None,
            "error": detail,
            "error_type": "http_error",
        }
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        detail = f"Could not reach Bright Data Web Unlocker: {exc}"
        logger.warning(
            "Scrape failed for %s: error_type=%s detail=%s",
            url,
            "network_error",
            detail,
        )
        return {
            **base,
            "success": False,
            "content": None,
            "length": None,
            "truncated": None,
            "error": detail,
            "error_type": "network_error",
        }

    full_length = len(content)
    truncated = max_chars is not None and full_length > max_chars
    body = content[:max_chars] if truncated else content

    logger.info(
        "Scraped %s (length=%d, truncated=%s)",
        url,
        full_length,
        truncated,
    )

    return {
        **base,
        "success": True,
        "content": body,
        "length": full_length,
        "truncated": truncated,
        "error": None,
        "error_type": None,
    }
