"""Pure document-shard fan-out helpers for the memory extraction coordinator
(#067, ADR-002 §3 as amended #066).

The coordinator flow ``memory-extract-etl-coordinator`` consumes these helpers to
resolve → partition → dispatch ``memory-extract-etl-worker`` runs → index once (see
:func:`tree.memory.extraction.pipeline.memory_extract_etl_coordinator`). This module
holds the reusable PURE helpers — there is NO Prefect ``@flow`` and NO deployment
here.

The pure, pipeline-agnostic partitioning math (``_partition_into_shards`` /
``_resolve_num_shards``) now lives in the neutral :mod:`tree.sharding` module
(ADR-002 §3 Amendment #066) so the data coordinator (#068) reuses the IDENTICAL
helpers without copy-paste. They are re-exported here so memory call sites and
tests keep their existing import paths; the MEMORY-specific helpers
(``_resolve_pending_document_ids``, ``_fan_out_extraction``, ``FanOutStats``)
remain in this module.

The fan-out axis is document-shards of ONE user. Topology (coordinator path):

1. **Resolve pending docs.** If ``document_ids`` is ``None``, compute the user's
   not-yet-ingested documents: a :class:`~tree.entities.documents.Document` is
   ingested iff its ``_id`` appears in some ``knowledge_graph`` object's
   ``sources`` array (there is no status flag on ``Document``). An explicit list
   is used verbatim. Empty result ⇒ no-op returning a zero report.
2. **Partition.** Split into ``min(num_shards, N)`` contiguous, disjoint, balanced
   shards; ``N < num_shards`` collapses to ``N`` shards.
3. **Fan out extraction.** One ``memory-extract-etl-worker`` run per shard via
   ``run_deployment`` under ``asyncio.gather(return_exceptions=True)`` so one
   shard's failure is isolated and recorded, never aborting the others. The
   coordinator dispatches a DISTINCT worker deployment — there is NO recursion and
   each child carries only ``{user_id, document_ids}`` (the worker has no
   ``num_shards`` param).
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

# The balanced-contiguous partitioning math now lives in the neutral, pipeline-
# agnostic ``tree.sharding`` module (ADR-002 §3 Amendment #066) so BOTH the memory
# and data coordinators import the IDENTICAL helpers without copy-paste. Memory
# extraction keeps importing them through this module (behaviour unchanged); the
# functions are re-exported so existing call sites and tests are untouched.
from tree.sharding import _partition_into_shards, _resolve_num_shards

logger = logging.getLogger(__name__)

__all__ = [
    "FanOutStats",
    "_fan_out_extraction",
    "_partition_into_shards",
    "_resolve_num_shards",
    "_resolve_pending_document_ids",
]

_KG_COLLECTION = "knowledge_graph"
# The coordinator dispatches the WORKER deployment (#067) — NOT itself. There is
# no recursion; the worker has no ``num_shards`` param.
_WORKER_DEPLOYMENT = "memory-extract-etl-worker/memory-extract-etl-worker"
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
    """Per-run accounting for the document-shard fan-out (coordinator path).

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
    ``memory_extract_etl_worker``'s own ``content != None`` fetch — a contentless row
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
    opik_trace_headers: dict[str, str] | None = None,
) -> FanOutStats:
    """Fan one worker dispatch out per shard, isolate failures, index ONCE.

    Pure coordination core (no DB, no partitioning) so the gather /
    failure-isolation / single-index contract is unit-testable directly.
    ``run_deployment`` is injected (the Prefect entrypoint in the flow; a fake in
    tests).

    * One ``memory-extract-etl-worker`` run per shard under
      ``asyncio.gather(return_exceptions=True)``, each carrying only
      ``{user_id, document_ids}`` — the coordinator dispatches a DISTINCT worker
      deployment, so there is NO recursion and NO ``num_shards`` child key (the
      worker has no such param). A single shard's exception is caught, logged,
      recorded in ``stats.failures``, and the gather still completes for the others
      (ADR-002 §3).
    * After the gather, a SINGLE ``memory-indexing-etl`` run for the user fires —
      never per-shard (indexing is a global backfill; per-shard would race
      writers). The index run fires regardless of how many shards failed, so a
      partial extraction is still indexed.

    ``opik_trace_headers`` (the coordinator's distributed-trace headers) is
    forwarded to every worker AND the indexing run as a flow parameter, so the
    whole coordinated run renders as ONE Opik trace across the process hops that
    ``run_deployment`` introduces. ``None`` (Opik off / no active trace) is
    simply not forwarded — each child then starts its own trace.
    """

    log = _get_run_logger()
    stats = FanOutStats(shards_total=len(shards))

    if not shards:
        log.info("extraction fan-out: 0 shards — nothing to do (no-op)")
        return stats

    worker_params: dict[str, Any] = {}
    if opik_trace_headers is not None:
        worker_params["opik_trace_headers"] = opik_trace_headers

    results = await asyncio.gather(
        *[
            run_deployment(
                _WORKER_DEPLOYMENT,
                parameters={
                    "user_id": str(user_id),
                    "document_ids": shard,
                    **worker_params,
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
        parameters={"user_id": str(user_id), **worker_params},
    )

    return stats
