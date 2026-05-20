"""Watermark read/write helpers for the dream/consolidation pipeline (#050).

The dream pipeline (#051) processes the knowledge graph incrementally: on
each run it only re-examines nodes touched since the last successful run.
The "since" boundary is a per-``(user_id, job)`` *watermark* persisted in
the ``knowledge_graph_meta_state`` collection (see
:class:`tree.entities.meta_state.KnowledgeGraphMetaState`).

Two helpers form the contract:

* :func:`load_watermark` — read the watermark for a tenant/job. A missing
  document means "never run" and yields an **epoch** ``last_run_at`` so the
  first run sweeps everything.
* :func:`record_dream_run` — upsert the watermark after a successful
  non-dry-run, recording the run's **start** time (captured BEFORE
  processing, never ``now()``) so nodes written mid-run are re-driven next
  time rather than skipped.

Both helpers are tenant-scoped: every read and write carries ``user_id``
and keys off the deterministic ``_id = "{user_id}:{job}"``. They operate
on the pymongo collection of the supplied ``database`` (an
``AsyncDatabase`` such as ``client[database_name]``), matching the
data-layer access style in :mod:`tree.memory.indexing.core`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId

from tree.entities.meta_state import (
    KnowledgeGraphMetaState,
    build_meta_state_id,
)

# Unix epoch, timezone-aware UTC. Returned as ``last_run_at`` when no
# watermark exists so the first run of a job treats every node as
# in-delta (a full sweep). Module-level so callers can compare against
# the same canonical value.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_META_STATE_COLLECTION = "knowledge_graph_meta_state"


async def load_watermark(
    *,
    database: Any,
    user_id: PydanticObjectId,
    job: str = "dream",
) -> KnowledgeGraphMetaState:
    """Load the watermark for ``(user_id, job)``.

    Reads the document by its deterministic ``_id = "{user_id}:{job}"``.

    * **Doc exists** ⇒ return the persisted
      :class:`KnowledgeGraphMetaState` (its ``last_run_at`` is the start
      time of the last successful run).
    * **Doc missing** ⇒ return an *unpersisted* watermark whose
      ``last_run_at`` is :data:`EPOCH`, so the caller treats the run as a
      full sweep. Nothing is written to the database in this case.

    The return value always exposes ``last_run_at`` (tz-aware UTC).
    """

    meta_state_id = build_meta_state_id(user_id, job)
    collection = database[_META_STATE_COLLECTION]

    raw = await collection.find_one({"_id": meta_state_id})
    if raw is None:
        # Missing doc ⇒ epoch. Build an in-memory watermark; do NOT
        # persist it — the first successful run is what writes the doc
        # (via record_dream_run).
        now = datetime.now(UTC)
        return KnowledgeGraphMetaState(
            id=meta_state_id,
            user_id=user_id,
            job=job,
            last_run_at=EPOCH,
            last_run_id=None,
            last_stats={},
            updated_at=now,
        )

    return KnowledgeGraphMetaState.model_validate(raw)


async def record_dream_run(
    *,
    database: Any,
    user_id: PydanticObjectId,
    job: str = "dream",
    run_start: datetime,
    last_run_id: str | None,
    last_stats: dict[str, Any],
) -> None:
    """Upsert the watermark for ``(user_id, job)`` after a successful run.

    Writes ``last_run_at = run_start`` — the START timestamp the caller
    captured BEFORE any processing, NOT ``now()``. This **no-gap**
    semantics means nodes ingested *during* a long run get re-driven on
    the next run (a slight idempotent overlap) and can never fall into a
    gap.

    ``updated_at`` is stamped with the current wall-clock ``now`` so
    operators can tell when the watermark itself was last touched.

    Idempotent: a second call with the same ``run_start`` (and same
    ``user_id`` / ``job``) leaves a single document with the same
    ``last_run_at``. The deterministic ``_id`` is the upsert key, so
    repeated calls update in place rather than inserting duplicates.

    ``run_start`` must be timezone-aware UTC; a naive datetime is rejected
    by the entity validator before the write.
    """

    if run_start.tzinfo is None:
        # Fail fast at the call boundary rather than persisting a naive
        # value that would corrupt later comparisons. Mirrors the entity
        # validator so the error surfaces even on the upsert path (which
        # bypasses Beanie model construction).
        raise ValueError(
            f"run_start must be timezone-aware (UTC); got naive datetime {run_start!r}"
        )

    meta_state_id = build_meta_state_id(user_id, job)
    now = datetime.now(UTC)
    collection = database[_META_STATE_COLLECTION]

    await collection.update_one(
        {"_id": meta_state_id},
        {
            "$set": {
                "user_id": user_id,
                "job": job,
                "last_run_at": run_start,
                "last_run_id": last_run_id,
                "last_stats": last_stats,
                "updated_at": now,
            },
        },
        upsert=True,
    )
