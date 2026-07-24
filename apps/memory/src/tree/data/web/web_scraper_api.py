"""Bright Data Web Scraper API HTTP client (trigger → poll → download).

Thin async wrapper around the Bright Data dataset collection endpoints
(``/datasets/v3/trigger``, ``/datasets/v3/progress``, ``/datasets/v3/snapshot``).
Like :mod:`tree.data.web.web_unlocker`, the module is intentionally pure: no
MongoDB, no Prefect, no ``Document``, no config reads — just credential
handling and the HTTP calls. Every knob is a parameter, so the caller owns
timing policy; dataset-specific ids and record mapping live in the caller too.

Collection is ALWAYS async (trigger + poll), never the sync ``/scrape``
endpoint: a measured collection took ~173 s for a single video, so the sync
endpoint's 1-minute window would 202-fall-through into this exact polling
logic on virtually every call (ADR-004, Decision 2).

Reference:
    .agents/skills/bright-data-best-practices/references/web-scraper-api.md
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from tree.config.settings import settings
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)

logger = logging.getLogger(__name__)

_BRIGHTDATA_TRIGGER_URL = "https://api.brightdata.com/datasets/v3/trigger"
_BRIGHTDATA_PROGRESS_URL = "https://api.brightdata.com/datasets/v3/progress"
_BRIGHTDATA_SNAPSHOT_URL = "https://api.brightdata.com/datasets/v3/snapshot"

# Per-HTTP-request timeout, distinct from the overall collection bound
# (``timeout_seconds``): httpx defaults to 5 s, too short for a snapshot
# download. Mirrors ``web_unlocker.fetch_url``'s 60 s default.
_HTTP_REQUEST_TIMEOUT_SECONDS = 60.0

# Upper bound on how much of a response body lands in an exception message: a
# full HTML captcha page is unreadable in logs.
_MAX_ERROR_BODY_CHARS = 500

_READY_STATUS = "ready"
_FAILED_STATUS = "failed"


class BrightDataTimeoutError(Exception):
    """Raised when a collection is not ready within the bounded wait."""


async def collect(
    dataset_id: str,
    inputs: list[dict[str, Any]],
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> list[dict[str, Any]]:
    """Run one Bright Data dataset collection and return its records.

    Triggers a collection for ``inputs``, polls ``/progress`` every
    ``poll_interval_seconds`` until the snapshot is ``ready`` (or ``failed``,
    or the bound expires), then downloads and returns the parsed records.

    Args:
        dataset_id: Bright Data scraper identifier (e.g. ``gd_...``).
        inputs: Collection input rows, e.g. ``[{"url": "https://..."}]``. An
            empty list is a no-op: no credentials are needed and no request
            is made.
        timeout_seconds: Upper bound on the whole poll wait, measured from
            just after the trigger returns.
        poll_interval_seconds: Delay between two ``/progress`` reads.

    Returns:
        The snapshot records verbatim, one dict per collected row.

    Raises:
        BrightDataConfigurationError: If ``BRIGHTDATA_API_KEY`` is empty
            (raised before any HTTP request).
        BrightDataRequestError: On any non-2xx response, a trigger response
            without a ``snapshot_id``, a ``failed`` collection, a snapshot body
            that is not a list of records, or a transport failure (connect/read
            timeout, network, protocol or proxy error).
        BrightDataTimeoutError: If the snapshot is still not ready after
            ``timeout_seconds``.
    """

    if not inputs:
        logger.info("No inputs for dataset %s; skipping collection", dataset_id)
        return []

    api_key = _resolve_api_key()

    snapshot_id = await _trigger(dataset_id, inputs, api_key=api_key)
    await _wait_until_ready(
        snapshot_id,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    records = await _download_snapshot(snapshot_id, api_key=api_key)

    logger.info(
        "Bright Data snapshot %s returned %d record(s)", snapshot_id, len(records)
    )

    return records


def _resolve_api_key() -> str:
    """Read the Bright Data API key from settings, raising if missing."""

    api_key = settings.brightdata_api_key.get_secret_value()
    if not api_key:
        raise BrightDataConfigurationError("BRIGHTDATA_API_KEY is not set")

    return api_key


def _auth_headers(api_key: str) -> dict[str, str]:
    """Build the Bearer auth headers every Web Scraper API call needs."""

    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


async def _trigger(
    dataset_id: str, inputs: list[dict[str, Any]], *, api_key: str
) -> str:
    """Start a collection and return its ``snapshot_id``."""

    logger.info(
        "Triggering Bright Data collection on dataset %s for %d input(s)",
        dataset_id,
        len(inputs),
    )

    payload = await _post_json(
        _BRIGHTDATA_TRIGGER_URL,
        params={"dataset_id": dataset_id, "format": "json"},
        payload={"input": inputs},
        api_key=api_key,
    )

    snapshot_id = payload.get("snapshot_id") if isinstance(payload, dict) else None
    if not snapshot_id:
        raise BrightDataRequestError(
            f"Bright Data trigger response has no snapshot_id: {payload}"
        )

    logger.info("Bright Data collection triggered: snapshot %s", snapshot_id)

    return str(snapshot_id)


async def _wait_until_ready(
    snapshot_id: str,
    *,
    api_key: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> None:
    """Poll ``/progress`` until the snapshot is ready, failed, or out of time."""

    deadline = time.monotonic() + timeout_seconds

    while True:
        payload = await _get_json(
            f"{_BRIGHTDATA_PROGRESS_URL}/{snapshot_id}",
            params=None,
            api_key=api_key,
        )
        status = payload.get("status") if isinstance(payload, dict) else None

        if status == _READY_STATUS:
            return

        if status == _FAILED_STATUS:
            raise BrightDataRequestError(
                f"Bright Data collection {snapshot_id} failed: {payload}"
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrightDataTimeoutError(
                f"Bright Data snapshot {snapshot_id} still {status!r} after "
                f"{timeout_seconds}s"
            )

        logger.debug(
            "Bright Data snapshot %s is %r; polling again in %ss",
            snapshot_id,
            status,
            poll_interval_seconds,
        )
        await asyncio.sleep(min(poll_interval_seconds, remaining))


async def _download_snapshot(snapshot_id: str, *, api_key: str) -> list[dict[str, Any]]:
    """Download a ready snapshot as a list of record dicts."""

    records = await _get_json(
        f"{_BRIGHTDATA_SNAPSHOT_URL}/{snapshot_id}",
        params={"format": "json"},
        api_key=api_key,
    )

    if not isinstance(records, list):
        raise BrightDataRequestError(
            f"Bright Data snapshot {snapshot_id} is not a record list: {records}"
        )

    return records


async def _post_json(
    url: str,
    *,
    params: dict[str, Any] | None,
    payload: dict[str, Any],
    api_key: str,
) -> Any:
    """Single POST returning parsed JSON. Kept thin so tests patch one seam."""

    try:
        async with httpx.AsyncClient(timeout=_HTTP_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                params=params,
                json=payload,
                headers=_auth_headers(api_key),
            )
    except httpx.TransportError as exc:
        raise _transport_error(url, exc) from exc

    return _parse_json(response)


async def _get_json(
    url: str,
    *,
    params: dict[str, Any] | None,
    api_key: str,
) -> Any:
    """Single GET returning parsed JSON. Kept thin so tests patch one seam."""

    try:
        async with httpx.AsyncClient(timeout=_HTTP_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(
                url,
                params=params,
                headers=_auth_headers(api_key),
            )
    except httpx.TransportError as exc:
        raise _transport_error(url, exc) from exc

    return _parse_json(response)


def _transport_error(url: str, exc: httpx.TransportError) -> BrightDataRequestError:
    """Type a transport failure as the error the fallback chain keys on.

    "Bright Data is unreachable right now" — a connect/read timeout, a DNS or
    TCP failure, a proxy refusal, a mid-response disconnect — is the SAME
    operational condition as the poll timeout ADR-004 Decision 3 already routes
    to the Gemini fallback. Letting a raw ``httpx`` error escape ``collect``
    instead hard-fails the Prefect task and burns its retries, because the
    fallback chain catches only the three Bright Data error types.

    Scope is ``httpx.TransportError``, NOT the wider ``httpx.HTTPError``: it is
    exactly the "request never completed" branch (timeouts, network, protocol,
    proxy, unsupported scheme), while ``httpx.HTTPStatusError`` — a real HTTP
    response, already handled by ``_parse_json`` — and every non-httpx error
    stay unwrapped, so a genuine bug still surfaces as itself.
    """

    return BrightDataRequestError(
        f"Bright Data Web Scraper API request to {url} failed at the transport "
        f"layer: {type(exc).__name__}: {exc}"
    )


def _parse_json(response: httpx.Response) -> Any:
    """Reject non-2xx responses, then decode the body as JSON.

    A non-JSON body is a request error, not a crash: Bright Data can answer
    HTTP 200 with a WAF / rate-limit / captcha HTML page or an empty body, and
    the caller's fallback chain keys on ``BrightDataRequestError`` (ADR-004,
    Decision 3). Letting ``json.JSONDecodeError`` escape would bypass that
    fallback and hard-fail the task instead.
    """

    if response.status_code < 200 or response.status_code >= 300:
        raise BrightDataRequestError(
            f"Bright Data Web Scraper API returned HTTP {response.status_code} "
            f"for {response.request.url}: {_body_excerpt(response)}"
        )

    try:
        return response.json()
    except ValueError as exc:
        # ``ValueError`` covers both ways a body can fail to decode:
        # ``json.JSONDecodeError`` (not JSON) and ``UnicodeDecodeError``
        # (binary body) — and nothing unrelated, so real bugs still surface.
        raise BrightDataRequestError(
            f"Bright Data Web Scraper API returned HTTP {response.status_code} "
            f"with a non-JSON body for {response.request.url}: "
            f"{_body_excerpt(response)}"
        ) from exc


def _body_excerpt(response: httpx.Response) -> str:
    """Response body trimmed to stay readable in logs and exception strings."""

    body = response.text
    if len(body) <= _MAX_ERROR_BODY_CHARS:
        return body

    return f"{body[:_MAX_ERROR_BODY_CHARS]}… ({len(body)} chars, truncated)"
