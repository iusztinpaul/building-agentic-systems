"""Pure document-shard fan-out helpers for the single extraction deployment
(#061, ADR-002 §3 as amended).

The fan-out is folded INTO the one ``memory-extraction-etl`` deployment via a
``num_shards`` parameter using recursive self-dispatch (see
:func:`tree.memory.extraction.pipeline.memory_extraction`). This module holds the
reusable PURE helpers that the orchestrator path of that flow consumes — there is
NO Prefect ``@flow`` and NO deployment here (the #056 standalone parent flow + its
separate fan-out deployment were deleted in #061).

The fan-out axis is document-shards of ONE user. Topology (orchestrator path,
``num_shards > 1``):

1. **Resolve pending docs.** If ``document_ids`` is ``None``, compute the user's
   not-yet-ingested documents: a :class:`~tree.entities.documents.Document` is
   ingested iff its ``_id`` appears in some ``knowledge_graph`` object's
   ``sources`` array (there is no status flag on ``Document``). An explicit list
   is used verbatim. Empty result ⇒ no-op returning a zero report.
2. **Partition.** Split into ``min(num_shards, N)`` contiguous, disjoint, balanced
   shards; ``N < num_shards`` collapses to ``N`` shards.
3. **Fan out extraction.** One ``memory-extraction-etl`` self-dispatch per shard
   via ``run_deployment`` under ``asyncio.gather(return_exceptions=True)`` so one
   shard's failure is isolated and recorded, never aborting the others. Each child
   carries ``num_shards=1`` → the worker path → recursion terminates after one
   level.
4. **Index ONCE.** After the gather, trigger a SINGLE ``memory-indexing-etl`` run
   for the user — NEVER per-shard (indexing is a global backfill over unembedded
   nodes; per-shard would race writers).

Per ``CLAUDE.md`` the Prefect ``@flow`` wiring is covered by integration tests;
the pure decision logic here is unit-tested directly with ``run_deployment``
mocked.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from beanie import PydanticObjectId
from prefect import get_run_logger

from tree.entities.documents import Document

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"
_EXTRACTION_DEPLOYMENT = "memory-extraction-etl/memory-extraction-etl"
_INDEXING_DEPLOYMENT = "memory-indexing-etl/memory-indexing-etl"


def _get_run_logger() -> logging.Logger:
    """Prefect run logger inside a flow/task; the module logger otherwise.

    Lets the pure helpers log through ``caplog`` when invoked outside a flow
    run (unit tests call them directly).
    """

    try:
        return get_run_logger()  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 — Prefect raises a typed context error
        return logger


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class FanOutStats:
    """Per-run accounting for the document-shard fan-out (orchestrator path).

    ``shards_total`` is how many shards the pending docs were partitioned into;
    ``succeeded`` / ``failed`` partition them by extraction outcome. ``failures``
    maps the failing shard index (string) to the exception message so one
    shard's blow-up is logged and isolated, never aborting the others.
    """

    shards_total: int = 0
    succeeded: int = 0
    failed: int = 0
    failures: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "shards_total": self.shards_total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "failures": self.failures,
        }


# ---------------------------------------------------------------------------
# Pure partitioning
# ---------------------------------------------------------------------------


def _resolve_num_shards(num_shards: int) -> int:
    """Resolve the effective shard count, clamped to ``>= 1``.

    The shard count is always an explicit per-run choice
    (``NUM_SHARDS`` / ``--num-shards`` / ``-p num_shards``). A non-positive value
    — 0 or negative, only reachable via a DIRECT Prefect API/UI trigger that
    bypasses the guarded ``--num-shards`` script path — clamps to ``1`` so the
    run shards everything into a single shard instead of becoming a silent
    zero-shard no-op (``_partition_into_shards``'s ``min(num_shards, N)`` would
    otherwise go non-positive on a truthy negative).
    """

    return max(1, num_shards)


def _partition_into_shards(document_ids: list[str], num_shards: int) -> list[list[str]]:
    """Split ``document_ids`` into exactly ``min(num_shards, N)`` shards.

    The shards are contiguous (preserve input order), disjoint, and balanced:
    sizes differ by at most one, with the larger (``ceil(N / shards)``-sized)
    shards leading. The in-order union reconstructs the input exactly.

    * ``N == 0`` ⇒ ``[]`` (no shards, no-op upstream).
    * ``N < num_shards`` ⇒ ``N`` singleton shards (we never emit empty shards).

    Example: ``N=6, num_shards=4`` ⇒ sizes ``2, 2, 1, 1`` (4 shards), NOT
    ``2, 2, 2`` (a fixed ``ceil``-chunk would under-shard).
    """

    n = len(document_ids)
    if n == 0:
        return []

    effective = min(num_shards, n)
    # Balanced contiguous split into exactly ``effective`` shards: the first
    # ``remainder`` shards get the larger ``ceil`` size, the rest the floor.
    base, remainder = divmod(n, effective)

    shards: list[list[str]] = []
    start = 0
    for i in range(effective):
        size = base + (1 if i < remainder else 0)
        shards.append(document_ids[start : start + size])
        start += size
    return shards


# ---------------------------------------------------------------------------
# Pending-doc resolution
# ---------------------------------------------------------------------------


async def _resolve_pending_document_ids(
    *,
    database: Any,
    user_id: PydanticObjectId,
) -> list[str]:
    """Return the user's NOT-yet-ingested document ids, deterministic order.

    A :class:`Document` is *ingested* iff its ``_id`` appears in some
    ``knowledge_graph`` object's ``sources`` array — there is no status flag on
    ``Document`` itself. So the pending set is every user-scoped ``Document``
    whose ``_id`` is absent from the union of all ``sources`` arrays in the
    user's ``knowledge_graph`` rows.

    Only documents with non-null ``content`` are eligible (mirrors
    ``memory_extraction``'s own ``content != None`` fetch — a contentless row
    has nothing to extract). Returned ids are sorted by their string form so
    the shard order is deterministic across runs.
    """

    # All document ids referenced by any of this user's KG objects.
    kg = database[_KG_COLLECTION]
    ingested: set[PydanticObjectId] = set()
    cursor = kg.find({"user_id": user_id, "sources": {"$ne": []}}, {"sources": 1})
    async for row in cursor:
        for src in row.get("sources", []) or []:
            ingested.add(src)

    pending: list[str] = []
    doc_cursor = Document.find({"user_id": user_id, "content": {"$ne": None}})
    async for doc in doc_cursor:
        if doc.id not in ingested:
            pending.append(str(doc.id))

    pending.sort()
    return pending


# ---------------------------------------------------------------------------
# Fan-out core (run_deployment injected so it is unit-testable)
# ---------------------------------------------------------------------------


async def _fan_out_extraction(
    *,
    user_id: PydanticObjectId,
    shards: list[list[str]],
    run_deployment: Any,
) -> FanOutStats:
    """Fan one extraction self-dispatch out per shard, isolate failures, index ONCE.

    Pure orchestration core (no DB, no partitioning) so the gather /
    failure-isolation / single-index contract is unit-testable directly.
    ``run_deployment`` is injected (the Prefect entrypoint in the flow; a fake in
    tests).

    * One ``memory-extraction-etl`` run per shard under
      ``asyncio.gather(return_exceptions=True)``, each with ``num_shards=1`` so the
      child takes the WORKER path (recursion terminates after one level). A single
      shard's exception is caught, logged, recorded in ``stats.failures``, and the
      gather still completes for the others (ADR-002 §3).
    * After the gather, a SINGLE ``memory-indexing-etl`` run for the user fires —
      never per-shard (indexing is a global backfill; per-shard would race
      writers). The index run fires regardless of how many shards failed, so a
      partial extraction is still indexed.
    """

    log = _get_run_logger()
    stats = FanOutStats(shards_total=len(shards))

    if not shards:
        log.info("extraction fan-out: 0 shards — nothing to do (no-op)")
        return stats

    results = await asyncio.gather(
        *[
            run_deployment(
                _EXTRACTION_DEPLOYMENT,
                parameters={
                    "user_id": str(user_id),
                    "document_ids": shard,
                    "num_shards": 1,
                },
            )
            for shard in shards
        ],
        return_exceptions=True,
    )

    for idx, result in enumerate(results):
        if isinstance(result, Exception):
            stats.failed += 1
            stats.failures[str(idx)] = str(result)
            log.error(
                "extraction fan-out: shard %d FAILED (isolated): %s",
                idx,
                result,
                exc_info=result,
            )
            continue
        stats.succeeded += 1

    log.info(
        "extraction fan-out: shards_total=%d succeeded=%d failed=%d",
        stats.shards_total,
        stats.succeeded,
        stats.failed,
    )

    # Index ONCE after every shard's extraction has settled — never per-shard.
    log.info("extraction fan-out: triggering single memory-indexing-etl run")
    await run_deployment(
        _INDEXING_DEPLOYMENT,
        parameters={"user_id": str(user_id)},
    )

    return stats
