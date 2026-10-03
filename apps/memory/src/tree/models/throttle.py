"""The shared Voyage embed throttle: one Prefect rate-limit acquire per POST.

Both Voyage clients (:mod:`tree.models.voyage_embedding`,
:mod:`tree.models.voyage_multimodal_embedding`) call
:func:`acquire_voyage_slot` — this module is the only Prefect ``rate_limit``
call site in ``tree``.
"""

import asyncio
import logging

import httpx
from prefect.concurrency.asyncio import (
    AcquireConcurrencySlotTimeoutError,
    ConcurrencySlotAcquisitionError,
    rate_limit,
)
from prefect.exceptions import PrefectHTTPStatusError

from tree.config.app_config import app_config

logger = logging.getLogger(__name__)

# The Prefect global concurrency limit (ADR-002 §1) that throttles every real
# Voyage embed POST across separate flow runs.
VOYAGE_EMBED_LIMIT = "voyage-embeddings"

# "Cannot reach the limiter" (ADR-013 §5). prefect 3.6's
# ``aacquire_concurrency_slots`` wraps every acquire failure in
# ``ConcurrencySlotAcquisitionError`` (the client's 401 / ConnectError chained
# as ``__cause__``) and timeouts in ``AcquireConcurrencySlotTimeoutError``; the
# bare client errors are listed for an unwrapped path. ``PrefectHTTPStatusError``
# is already an ``httpx.HTTPError`` — kept by name because the ADR names it.
_LIMITER_UNAVAILABLE = (
    ConcurrencySlotAcquisitionError,
    AcquireConcurrencySlotTimeoutError,
    PrefectHTTPStatusError,
    httpx.HTTPError,
)


async def acquire_voyage_slot() -> None:
    """Acquire one ``voyage-embeddings`` slot before a real Voyage POST.

    Placement (ADR-002 §1): callers await this immediately before each real
    network POST attempt, INSIDE their 429-backoff loop, so a 429-retry
    re-acquires a fresh slot, and AFTER their ``if not texts: return []``
    short-circuit, so an empty call never occupies a slot. A
    ``_CachedSingleEmbedding`` cache hit never reaches a Voyage client, so it
    is never throttled. ``strict=False`` makes an absent limit a no-op (Prefect
    logs its own "do not exist - skipping acquisition" warning).

    Fail-open (ADR-013 §5, task 178): the limiter is a courtesy to the shared
    free-tier key — Voyage's own 429 backoff is the quota guard. When the
    limiter cannot be reached (stale ``PREFECT_API_KEY``, Prefect down, HTTP
    error, timeout) this logs ONE WARNING and returns, so the POST proceeds.
    Any other exception propagates unchanged.

    Bounded acquire (task 178): a limiter that accepts but never replies would
    otherwise stall each POST for Prefect's whole retry budget (~6-8 min).
    ``asyncio.wait_for`` is the hard caller bound — Prefect serializes acquires
    per limit in one background service, and its ``timeout_seconds`` only
    starts once this request reaches the head of that queue. Passing the same
    ``timeout_seconds`` lets the service drop a hung request so the queue
    drains. Trade-off: the bound also caps a legitimate throttling wait; past
    it the POST proceeds and Voyage's 429 backoff guards the quota.
    """

    bound = app_config.concurrency.voyage_slot_acquire_timeout_seconds
    try:
        await asyncio.wait_for(
            rate_limit(
                VOYAGE_EMBED_LIMIT, occupy=1, timeout_seconds=bound, strict=False
            ),
            timeout=bound,
        )
    except _LIMITER_UNAVAILABLE as exc:
        # The wrapper's message is generic; the cause names the 401 / refusal.
        # A timeout's cause is a message-less ``TimeoutError`` — fall back.
        detail = str(exc.__cause__ or "") or str(exc)
        logger.warning(
            "Prefect rate limiter %r unavailable: %s — embedding without throttle",
            VOYAGE_EMBED_LIMIT,
            detail,
        )
    except TimeoutError:
        # ``wait_for``'s own bound (message-less). Must follow the clause above:
        # prefect's ``AcquireConcurrencySlotTimeoutError`` is a ``TimeoutError``.
        logger.warning(
            "Prefect rate limiter %r unavailable: no reply within %s s"
            " — embedding without throttle",
            VOYAGE_EMBED_LIMIT,
            bound,
        )
