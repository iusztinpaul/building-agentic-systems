"""Bright Data Web Unlocker HTTP client.

Thin async wrapper around the Bright Data Web Unlocker REST endpoint
(``POST https://api.brightdata.com/request``). The module is intentionally
pure: no MongoDB, no Prefect, no ``Document`` — just credential handling,
URL validation, and the HTTP call. Persistence and orchestration live in
the data pipeline that builds on top of this client.

Reference:
    .claude/skills/bright-data-best-practices/references/web-unlocker.md
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx

from tree.config.settings import settings

logger = logging.getLogger(__name__)

_BRIGHTDATA_REQUEST_URL = "https://api.brightdata.com/request"


class BrightDataConfigurationError(Exception):
    """Raised when required Bright Data credentials are missing."""


class BrightDataRequestError(Exception):
    """Raised when Bright Data returns a non-2xx response."""


def _validate_url(url: str) -> None:
    """Reject URLs Bright Data would either bill us for or refuse."""

    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must start with http:// or https://")

    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("URL must start with http:// or https://")


def _resolve_credentials() -> tuple[str, str]:
    """Read credentials from settings, raising a clear error if missing."""

    api_key = settings.brightdata_api_key.get_secret_value()
    if not api_key:
        raise BrightDataConfigurationError("BRIGHTDATA_API_KEY is not set")

    zone = settings.brightdata_unlocker_zone
    if not zone:
        raise BrightDataConfigurationError("BRIGHTDATA_UNLOCKER_ZONE is not set")

    return api_key, zone


async def fetch_url(
    url: str,
    *,
    data_format: Literal["markdown", "html"] = "markdown",
    timeout_seconds: float = 60.0,
) -> str:
    """Fetch the rendered content of a URL via Bright Data Web Unlocker.

    Posts to ``https://api.brightdata.com/request`` with::

        Authorization: Bearer <BRIGHTDATA_API_KEY>
        json={"zone": <BRIGHTDATA_UNLOCKER_ZONE>,
              "url": url,
              "format": "raw",
              "data_format": data_format}

    Args:
        url: Absolute ``http://`` or ``https://`` URL to fetch.
        data_format: ``"markdown"`` (default, best for LLM input) or ``"html"``.
        timeout_seconds: Per-request timeout passed to ``httpx``.

    Returns:
        The response body verbatim — markdown text by default, raw HTML when
        ``data_format="html"``.

    Raises:
        BrightDataConfigurationError: If ``BRIGHTDATA_API_KEY`` or
            ``BRIGHTDATA_UNLOCKER_ZONE`` is empty.
        ValueError: If ``url`` is empty or does not start with
            ``http://``/``https://``.
        BrightDataRequestError: On any non-2xx response (message includes the
            status code and the response body).
        httpx.TimeoutException: Propagated as-is on network timeout.
    """

    _validate_url(url)
    api_key, zone = _resolve_credentials()

    payload = {
        "zone": zone,
        "url": url,
        "format": "raw",
        "data_format": data_format,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    logger.info("Fetching URL via Bright Data Web Unlocker: %s", url)

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            _BRIGHTDATA_REQUEST_URL,
            json=payload,
            headers=headers,
        )

    if response.status_code < 200 or response.status_code >= 300:
        raise BrightDataRequestError(
            f"Bright Data Web Unlocker returned HTTP {response.status_code}: "
            f"{response.text}"
        )

    return response.text
