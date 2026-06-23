"""Shared per-element isolation for batch-grain ETL tasks (#078 pattern, lifted #079).

:func:`gather_isolated` is the "run an async unit-of-work over a batch, isolate
per-element failures" shape #078 INLINED inside its arxiv ``enrich_batch`` /
``load_batch`` tasks. #079 makes the same shape recur 4+ times (substack RSS
``load_batch``, substack article ``extract_batch``, substack article ``load_batch``,
plus arxiv ``load_batch``), crossing the threshold #078 named for pulling it into a
shared module — so it lives here now.

Contract: every element runs under a SINGLE ``asyncio.gather(return_exceptions=True)``.
A per-element exception is logged at WARNING and the element is DROPPED; a ``None``
result (e.g. a dedup skip) is dropped too but is NOT counted as a failure. The helper
returns ``(successes, failure_count)`` and NEVER propagates one element's failure. Only
a batch-WIDE failure (raised by the caller OUTSIDE the gather) hard-fails the calling
task, which Prefect then retries — safe because the data-layer loads dedup on
``(user_id, source_uri)`` so a retried batch never double-inserts.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def gather_isolated[T, R](
    items: list[T],
    work: Callable[[T], Awaitable[R | None]],
) -> tuple[list[R], int]:
    """Run ``work`` over each item, isolating per-element failures.

    Awaits ``work(item)`` for every item under one
    ``asyncio.gather(return_exceptions=True)``. Returns the successful, non-``None``
    results (input order) and the count of elements whose ``work`` raised. A raise is
    logged at WARNING + dropped (never propagated); a ``None`` result is dropped
    without being counted as a failure.
    """

    results = await asyncio.gather(
        *[work(item) for item in items], return_exceptions=True
    )

    successes: list[R] = []
    failures = 0
    for item, result in zip(items, results, strict=True):
        if isinstance(result, BaseException):
            failures += 1
            logger.warning("Batch element failed; skipping: %r", item, exc_info=result)
        elif result is not None:
            successes.append(result)

    return successes, failures
