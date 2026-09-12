"""The ``memory_clusters`` collection: one row per **Memory cluster** (ADR-007 §3).

A **Clustering run** groups a user's embedded **Child chunk**s and writes one
:class:`MemoryCluster` per non-noise cluster. The rows are DERIVED, per-run
artefacts with their own lifecycle — a new run deletes the user's previous rows
and writes fresh ones, so there is no run history and nothing is ever diffed
across runs.

Why a separate collection rather than cluster nodes in ``memory`` (ADR-007 §3):
as ``memory`` nodes they would need edges to their members (graphrag-only,
contradicting the mode-orthogonality of clustering), they would pollute
``$vectorSearch`` / ``$text`` results and the graph tools, and "delete the
previous run" would become a graph mutation. Membership therefore lives on the
chunk (``MemoryEntry.cluster_id``), which is what the **Embedding map** draws.

Noise is NOT a row: chunks HDBSCAN could not place get ``cluster_id = -1`` and
coordinates on the chunk, but no cluster row and no summary.
"""

from __future__ import annotations

from datetime import datetime

from beanie import Document as BeanieDocument
from beanie import PydanticObjectId
from pydantic import BaseModel, Field, field_validator
from pymongo import IndexModel

MEMORY_CLUSTERS_COLLECTION = "memory_clusters"
"""Name of the collection holding every **Memory cluster** row — the ONE spelling.

:class:`MemoryCluster`'s ``Settings.name`` and every raw-pymongo reader read
THIS constant, so a future rename is a one-line change (the lesson of the
``knowledge_graph`` -> ``memory`` rename, ADR-006).
"""


def build_cluster_id(user_id: PydanticObjectId, cluster_id: int) -> str:
    """Build a tenant-scoped cluster ``_id``: ``"{user_id}:cluster:{cluster_id}"``.

    Deterministic, exactly like :func:`tree.entities.memory.build_node_id`: the
    id IS the upsert key, so re-running the writer for a given run is
    idempotent, and two tenants can never share a row.

    ``user_id`` is a **required, positional** parameter — forgetting it is a
    type-checker error, never a silent runtime fallback.
    """

    return f"{user_id}:cluster:{cluster_id}"


class ClusterCentroid(BaseModel):
    """A cluster's centre in **Embedding map** space (ADR-007 §3).

    2-D DISPLAY coordinates — the mean of its members' ``viz.x`` / ``viz.y``,
    not a centre in the 1024-d embedding space (that one is transient: it is
    computed during sampling and never stored).
    """

    x: float = Field(description="Centroid x in the 2-D Embedding map space.")
    y: float = Field(description="Centroid y in the 2-D Embedding map space.")


class MemoryCluster(BeanieDocument):
    """One HDBSCAN cluster of a user's **Child chunk** embeddings (ADR-007 §3).

    Written wholesale by a **Clustering run** and read (never computed) by the
    ``visualize_memory_embeddings`` MCP tool and the CLI. Mode-orthogonal:
    identical in ``rag`` and ``graphrag``, with no graph rows and no edges.
    """

    id: str

    user_id: PydanticObjectId = Field(
        description="Tenant scope. Read via the compound (user_id, run_id) index.",
    )
    run_id: str = Field(
        description=(
            "The Clustering run that produced this row (the Prefect flow-run "
            "id). Chunks whose viz.run_id differs are stale and are omitted "
            "from the Embedding map."
        ),
    )
    # No ``ge=0`` constraint: the validator below owns the bound so the error
    # message can explain WHY (-1 is noise), instead of a bare "greater than or
    # equal to 0".
    cluster_id: int = Field(
        description=(
            "HDBSCAN label, unique per run and per user. Noise (-1) never gets "
            "a row — it is stored on the chunk only."
        ),
    )
    label: str = Field(
        description=(
            "LLM-written cluster name, at most 6 words. A cluster whose summary "
            "call fails after retries falls back to 'Cluster {cluster_id}'."
        ),
    )
    summary: str = Field(
        description=(
            "LLM-written description of what the cluster's chunks are about, at "
            "most 100 words. Empty when the summary call failed (fail-open)."
        ),
    )
    keywords: list[str] = Field(
        description=(
            "3-5 LLM-written keywords for the cluster. Explicitly empty (never "
            "absent) on a failed summary."
        ),
    )
    size: int = Field(
        ge=1,
        description="Number of child chunks assigned to this cluster in this run.",
    )
    sample_chunk_ids: list[str] = Field(
        description=(
            "The <= 20 child chunk _ids the summariser actually saw (nearest "
            "the centroid + seeded random) — the evidence behind label/summary."
        ),
    )
    centroid: ClusterCentroid = Field(
        description="Cluster centre in Embedding map space; where the legend points.",
    )
    created_at: datetime = Field(
        description="When the row was written. Timezone-aware UTC.",
    )

    @field_validator("cluster_id", mode="after")
    @classmethod
    def _reject_noise(cls, value: int) -> int:
        """Noise (``-1``) is never a cluster row (ADR-007 §3).

        HDBSCAN's ``-1`` label is "not in any cluster", so a ``-1`` row would be
        a labelled summary of everything the algorithm refused to group. The
        marker lives on the chunk (``MemoryEntry.cluster_id``) instead.
        """

        if value < 0:
            raise ValueError(
                "cluster_id must be >= 0: HDBSCAN noise (-1) is stored on the "
                f"chunk and never gets a memory_clusters row; got {value}"
            )
        return value

    @field_validator("created_at", mode="after")
    @classmethod
    def _require_tz_aware(cls, value: datetime) -> datetime:
        """Reject naive datetimes on ``created_at``.

        Per ``CLAUDE.md``: all datetimes are timezone aware (UTC by default).
        A naive value would silently corrupt later comparisons against a
        tz-aware ``now`` (e.g. "which run is the latest").
        """

        if value.tzinfo is None:
            raise ValueError(
                f"created_at must be timezone-aware (UTC); got naive datetime {value!r}"
            )
        return value

    class Settings:
        name = MEMORY_CLUSTERS_COLLECTION
        indexes = [
            # The two reads this collection serves: "every cluster of this
            # user's latest run" (the map + the MCP tool) and "delete this
            # user's previous run" (the writer). Both are (user_id, run_id).
            IndexModel(
                [("user_id", 1), ("run_id", 1)],
                name="user_run",
            ),
        ]
