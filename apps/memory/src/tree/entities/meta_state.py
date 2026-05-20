"""Knowledge-graph meta-state entity (#050).

The incremental-watermark substrate the dream/consolidation pipeline
(#051) drives off. One :class:`KnowledgeGraphMetaState` document per
``(user_id, job)`` records *when the last successful, non-dry-run of that
job started* so the next run only has to re-examine nodes touched since.

Key design points:

* ``_id`` is a deterministic string ``f"{user_id}:{job}"`` (e.g.
  ``"65f...:dream"``), mirroring the project's string-``_id`` convention
  for KG docs (see ``build_node_id`` / ``build_edge_id`` in
  :mod:`tree.entities.knowledge_graph`). Because ``_id`` already encodes
  both ``user_id`` and ``job``, upserts are idempotent and cross-tenant
  collisions are impossible by construction.
* ``last_run_at`` is the **START** timestamp of the last successful run,
  NOT its completion time. Writing the start time means any node ingested
  *during* a long run is re-driven on the next run (a slight idempotent
  overlap) and can never fall into a gap.
* Every datetime is timezone-aware UTC. Per ``CLAUDE.md`` the project
  rejects naive datetimes; a validator enforces this so a naive value can
  never corrupt later comparisons against a tz-aware ``now``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Document as BeanieDocument
from beanie import PydanticObjectId
from pydantic import Field, field_validator
from pymongo import IndexModel


def build_meta_state_id(user_id: PydanticObjectId, job: str) -> str:
    """Build a tenant-scoped meta-state ``_id``: ``"{user_id}:{job}"``.

    ``user_id`` and ``job`` are both **required** — there is intentionally
    no default. The deterministic id is the upsert key and the tenant
    correctness guarantee in one: two different tenants (or two different
    jobs for one tenant) can never share a document.
    """

    return f"{user_id}:{job}"


class KnowledgeGraphMetaState(BeanieDocument):
    """Per-``(user_id, job)`` incremental-processing watermark (#050).

    Stored in the ``knowledge_graph_meta_state`` collection keyed by the
    deterministic string ``_id = "{user_id}:{job}"``. The dream pipeline
    (#051) reads ``last_run_at`` to bound the set of nodes it must
    re-examine and writes it back (= the run's start time) after a
    successful non-dry-run.
    """

    id: str
    user_id: PydanticObjectId
    """Tenant scope. Indexed via the compound ``(user_id, job)`` index."""

    job: str
    """Job name this watermark belongs to (e.g. ``"dream"``)."""

    last_run_at: datetime
    """START timestamp of the last SUCCESSFUL non-dry-run. Timezone-aware
    UTC. The next run treats every node touched at-or-after this instant
    as in-delta."""

    last_run_id: str | None = None
    """Prefect flow-run id (or a generated id) of the last run, for
    traceability. ``None`` when not supplied."""

    last_stats: dict[str, Any] = Field(default_factory=dict)
    """Free-form stats blob from the last run (counts of pairs examined /
    auto-merged / flagged, etc.)."""

    updated_at: datetime
    """When this watermark document was last written (the upsert's
    wall-clock ``now``). Timezone-aware UTC."""

    @field_validator("last_run_at", "updated_at", mode="after")
    @classmethod
    def _require_tz_aware(cls, value: datetime) -> datetime:
        """Reject naive datetimes on ``last_run_at`` / ``updated_at``.

        Per ``CLAUDE.md``: all datetimes are timezone aware (UTC by
        default). A naive value would silently corrupt later comparisons
        against tz-aware ``now`` in the consolidation flow — make it a
        hard error at validation time.
        """

        if value.tzinfo is None:
            raise ValueError(
                "last_run_at / updated_at must be timezone-aware (UTC); "
                f"got naive datetime {value!r}"
            )
        return value

    class Settings:
        name = "knowledge_graph_meta_state"
        indexes = [
            # ``_id`` already encodes ``(user_id, job)``, so this compound
            # index is technically optional. It is declared so that
            # ``find({"user_id": X})`` scans (e.g. "every watermark for
            # this tenant") hit an index prefix rather than a collection
            # scan, consistent with the user_id-leading convention on
            # every other tenant-scoped collection in this codebase.
            IndexModel(
                [("user_id", 1), ("job", 1)],
                name="user_job",
            ),
        ]
