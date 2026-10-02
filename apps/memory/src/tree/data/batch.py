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

Isolated is not forgotten (#174): every dropped element is ALSO added to the active
:class:`ItemFailureTally`, opened by the data worker via :func:`track_item_failures`.
The tally lives here, in the ONE function every per-item failure routes through, so
the ~10 call sites across five platform pipelines need no return-type plumbing. The
worker then fails its run with :func:`item_failures_message`, and the data
coordinator — a ``run_deployment`` process hop away, with no result persistence —
reads the count back out of the run's state message via :func:`parse_items_failed`.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel

from tree.flow_runs import PartialIngestError

logger = logging.getLogger(__name__)

# The token the worker's failure message carries and the coordinator parses. The
# formatter and the parser below are the only two places that know it. Anchored to
# the raised error's type name and the ``;`` separator, so a Crashed run whose
# message merely MENTIONS ``items_failed=0`` is never read as a partial ingest.
_ITEMS_FAILED_PATTERN = re.compile(
    rf"{PartialIngestError.__name__}: items_failed=(\d+);"
)
# Bounds the item repr named by a first error whose exception has no message.
_ITEM_REPR_MAX_CHARS = 200


class ItemFailureTally(BaseModel):
    """Per-item failures :func:`gather_isolated` dropped while the tally was active."""

    count: int = 0
    first_error: str | None = None


# A MUTABLE tally, so the count survives Prefect copying the context into a task
# or an inline subflow: the copy holds the same object.
_ACTIVE_TALLY: ContextVar[ItemFailureTally | None] = ContextVar(
    "item_failure_tally", default=None
)


@contextmanager
def track_item_failures() -> Iterator[ItemFailureTally]:
    """Tally every element :func:`gather_isolated` drops inside the block."""

    tally = ItemFailureTally()
    token = _ACTIVE_TALLY.set(tally)
    try:
        yield tally
    finally:
        _ACTIVE_TALLY.reset(token)


def item_failures_message(tally: ItemFailureTally) -> str:
    """The worker's failure message: ``items_failed=<n>; first: <Type: message>``."""

    return f"items_failed={tally.count}; first: {tally.first_error}"


def parse_items_failed(message: str) -> int | None:
    """The count a raised :func:`item_failures_message` carries; ``None`` if absent.

    Searched, because Prefect wraps the raised message
    (``Flow run encountered an exception: PartialIngestError: items_failed=3; …``),
    but anchored to that exact token. A count below 1 is ``None`` too: the worker
    never raises over zero failed items, so such a run hard-failed for some other
    reason and the coordinator must count it as a failed shard.
    """

    match = _ITEMS_FAILED_PATTERN.search(message)
    if match is None:
        return None
    count = int(match.group(1))
    return count if count >= 1 else None


def _describe_failure(item: object, error: BaseException) -> str:
    """``Type: message``; ``Type on <item repr>`` when the message is empty."""

    if str(error):
        return f"{type(error).__name__}: {error}"
    return f"{type(error).__name__} on {repr(item)[:_ITEM_REPR_MAX_CHARS]}"


async def gather_isolated[T, R](
    items: list[T],
    work: Callable[[T], Awaitable[R | None]],
) -> tuple[list[R], int]:
    """Run ``work`` over each item, isolating per-element failures.

    Awaits ``work(item)`` for every item under one
    ``asyncio.gather(return_exceptions=True)``. Returns the successful, non-``None``
    results (input order) and the count of elements whose ``work`` raised. A raise is
    logged at WARNING + dropped (never propagated) and added to the active
    :class:`ItemFailureTally`, if any; a ``None`` result is dropped without being
    counted as a failure.
    """

    results = await asyncio.gather(
        *[work(item) for item in items], return_exceptions=True
    )

    successes: list[R] = []
    failures = 0
    first_error: str | None = None
    for item, result in zip(items, results, strict=True):
        if isinstance(result, BaseException):
            failures += 1
            first_error = first_error or _describe_failure(item, result)
            logger.warning("Batch element failed; skipping: %r", item, exc_info=result)
        elif result is not None:
            successes.append(result)

    tally = _ACTIVE_TALLY.get()
    if tally is not None and failures:
        tally.count += failures
        tally.first_error = tally.first_error or first_error

    return successes, failures
