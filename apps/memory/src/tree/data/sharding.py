"""Pure source-shard fan-out helpers for the data orchestrator
(#068, ADR-002 §3 as amended #066).

The orchestrator flow ``data-etl-orchestrator`` consumes these helpers to read the
configured ``sources:`` list → partition into balanced shards → dispatch one
``data-etl-worker`` run per shard (see
:func:`tree.data.pipeline.data_etl_orchestrator`). This module holds the reusable
PURE helpers — there is NO Prefect ``@flow`` and NO deployment here.

The pure, pipeline-agnostic partitioning math (``_partition_into_shards`` /
``_resolve_num_shards``) lives in the neutral :mod:`tree.sharding` module
(ADR-002 §3 Amendment #066) so BOTH orchestrators reuse the IDENTICAL helpers
without copy-paste. They are re-exported here so data call sites and tests share
the same import path; the DATA-specific helpers (``DataFanOutStats``,
``_fan_out_data``) live in this module.

The fan-out axis is SOURCE-shards (a balanced subset of the configured
``sources:`` list), distinct from memory's document-shards but the SAME
partitioning math. Topology (orchestrator path):

1. **Read sources.** Read ``app_config.sources.sources``. Empty ⇒ no-op zero report.
2. **Partition.** Split into ``min(num_shards, N)`` contiguous, disjoint, balanced
   shards; ``N < num_shards`` collapses to ``N`` shards.
3. **Fan out ingestion.** One ``data-etl-worker`` run per shard via ``run_deployment``
   under ``asyncio.gather(return_exceptions=True)`` so one shard's failure is isolated
   and recorded, never aborting the others. The orchestrator dispatches a DISTINCT
   worker deployment — there is NO recursion and each child carries
   ``{user_id, sources}`` (the shard's serialized source entries).
4. **NO trailing step.** The data pipeline only produces ``documents``; there is no
   index. This module NEVER references an indexing deployment.

Per ``CLAUDE.md`` the Prefect ``@flow`` wiring is covered by integration tests;
the pure decision logic here is unit-tested directly with ``run_deployment`` mocked.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from beanie import PydanticObjectId
from prefect import get_run_logger

# The balanced-contiguous partitioning math lives in the neutral, pipeline-
# agnostic ``tree.sharding`` module (ADR-002 §3 Amendment #066) so BOTH the memory
# and data orchestrators import the IDENTICAL helpers without copy-paste. The data
# orchestrator keeps importing them through this module; the functions are
# re-exported so call sites and tests share one import path.
from tree.sharding import _partition_into_shards, _resolve_num_shards

logger = logging.getLogger(__name__)

__all__ = [
    "DataFanOutStats",
    "_fan_out_data",
    "_partition_into_shards",
    "_resolve_num_shards",
]

# The orchestrator dispatches the WORKER deployment (#068) — NOT itself. There is
# no recursion; the worker has no ``num_shards`` param. There is NO trailing
# index deployment for the data path.
_WORKER_DEPLOYMENT = "data-etl-worker/data-etl-worker"


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
class DataFanOutStats:
    """Per-run accounting for the source-shard fan-out (orchestrator path).

    Mirrors the memory ``FanOutStats`` shape (``shards_total`` / ``succeeded`` /
    ``failed`` / ``failures``) so the two splits report identically. The data
    orchestrator does NOT collect per-shard ``Document`` lists back — the worker
    persists documents directly, so the orchestrator only needs the fan-out
    accounting. ``failures`` maps the failing shard index (string) to the
    exception message so one shard's blow-up is logged and isolated, never
    aborting the others.
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
# Fan-out core (run_deployment injected so it is unit-testable)
# ---------------------------------------------------------------------------


async def _fan_out_data(
    *,
    user_id: PydanticObjectId,
    shards: list[list[dict[str, Any]]],
    run_deployment: Any,
    opik_trace_headers: dict[str, str] | None = None,
) -> DataFanOutStats:
    """Fan one worker dispatch out per shard, isolate failures, NO trailing step.

    Pure orchestration core (no DB, no partitioning) so the gather /
    failure-isolation contract is unit-testable directly. ``run_deployment`` is
    injected (the Prefect entrypoint in the flow; a fake in tests).

    * One ``data-etl-worker`` run per shard under
      ``asyncio.gather(return_exceptions=True)``, each carrying
      ``{user_id, sources}`` — the orchestrator dispatches a DISTINCT worker
      deployment, so there is NO recursion and NO ``num_shards`` child key (the
      worker has no such param). A single shard's exception is caught, logged,
      recorded in ``stats.failures``, and the gather still completes for the
      others (ADR-002 §3).
    * NO trailing/index run — the data pipeline only produces ``documents``;
      there is no index. This function fires EXACTLY ``len(shards)`` worker runs
      and nothing else.

    ``opik_trace_headers`` (the orchestrator's distributed-trace headers) is
    forwarded to every worker as a flow parameter so the orchestrated data run
    renders as ONE Opik trace across ``run_deployment``'s process hop. ``None``
    is simply not forwarded — each worker then starts its own trace.
    """

    log = _get_run_logger()
    stats = DataFanOutStats(shards_total=len(shards))

    if not shards:
        log.info("data fan-out: 0 shards — nothing to do (no-op)")
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
                    "sources": shard,
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
                "data fan-out: shard %d FAILED (isolated): %s",
                idx,
                result,
                exc_info=result,
            )
            continue
        stats.succeeded += 1

    log.info(
        "data fan-out: shards_total=%d succeeded=%d failed=%d (NO trailing index)",
        stats.shards_total,
        stats.succeeded,
        stats.failed,
    )

    return stats
