"""Every Mongo touch of the clustering feature (ADR-007 §3).

Four operations, and nothing else reads or writes these rows:

* :func:`load_child_embeddings` — the run's INPUT: one user's embedded **Child
  chunk**s, in ``_id`` order. The order is load-bearing, not cosmetic: UMAP and
  HDBSCAN are deterministic for a fixed input ORDER and seed, so a re-run on an
  unchanged corpus reproduces the same labels and the same map.
* :func:`write_clustering_run` — the run's OUTPUT, in three statements: delete
  the user's previous cluster rows, upsert this run's clusters on their
  deterministic ``_id``s, and stamp every listed chunk with its ``cluster_id``
  and ``viz``. Idempotent for a given ``run_id``.
* :func:`latest_run_id` — which **Clustering run** the surfaces should read.
* :func:`unchanged_run_id` — the clustering cache: would a re-run reproduce
  the latest run?
* :func:`load_embedding_map` — the **Embedding map** a surface renders, read
  from the run (ADR-007 §8): the chunks of the most-recent documents, counted
  against the whole run (ADR-013 §3). With no run yet, no points at all.
* :func:`load_query_map` — a query view: the matches over a grey reference
  cloud, both at the run's UMAP coordinates.

Raw pymongo on ``MEMORY_COLLECTION`` / ``MEMORY_CLUSTERS_COLLECTION``, following
``rag/indexing.py``'s style rather than Beanie's: these are bulk statements over
projections, not object graphs, and the ODMs (:class:`MemoryEntry`,
:class:`MemoryCluster`) stay the schema of record.

Every query is ``user_id``-scoped. There is no cross-tenant read here, and no
"latest run" that spans users.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import AsyncMongoClient, UpdateOne

from tree.config.app_config import app_config
from tree.entities.clusters import MEMORY_CLUSTERS_COLLECTION, build_cluster_id
from tree.entities.memory import (
    MEMORY_COLLECTION,
    STORED_VECTOR_QUERY,
    from_stored_vector,
)
from tree.memory.clustering.core import NOISE_LABEL
from tree.memory.clustering.types import (
    EmbeddingMap,
    MapPoint,
    MemoryClusterInfo,
    QueryMap,
)
from tree.memory.rag.structure import rank_recent_documents

logger = logging.getLogger(__name__)

_SNIPPET_CHARS = 160

_POINT_PROJECTION: dict[str, int] = {
    "_id": 1,
    "sources": 1,
    "cluster_id": 1,
    "viz": 1,
    "properties.title": 1,
    "properties.heading_path": 1,
    "properties.content": 1,
}
"""What a drawn point reads — the tooltip fields, never the vector."""

_LEGACY_CLUSTER_FIELDS: dict[str, str] = {
    "summary": "",
    "keywords": "",
    "sample_chunk_ids": "",
}
"""``memory_clusters`` fields of the LLM-labelled era, ``$unset`` on every write
so a surviving cluster id never keeps an old run's summary."""
"""How much chunk text a tooltip carries. The map draws points, not documents."""

_CHILD_ROW_FILTER: dict[str, Any] = {
    "kind": "node",
    "type": "chunk",
    "subtype": "child",
}
"""What a **Child chunk** row IS — the only row type clustering ever touches."""

_CHILD_FILTER: dict[str, Any] = {
    **_CHILD_ROW_FILTER,
    # A stored float32 ``binData`` vector (task 175); a pending child has no
    # ``embedding`` at all. The complement of the backfill's ``{"embedding":
    # None}`` in ``rag/indexing.py``.
    "embedding": STORED_VECTOR_QUERY,
}
"""The ONE definition of "a chunk a **Clustering run** may cluster"."""


def _embedded_children_filter(user_id: PydanticObjectId) -> dict[str, Any]:
    """:data:`_CHILD_FILTER`, scoped to one tenant."""

    return {"user_id": user_id, **_CHILD_FILTER}


class ChildEmbeddingRow(BaseModel):
    """One **Child chunk** as the clustering recipe consumes it.

    Only the id and the vector (the input to both UMAP fits): the run names
    clusters by size, so it reads no text.
    """

    chunk_id: str = Field(description="The child chunk row's ``_id``.")
    embedding: list[float] = Field(description="Its Voyage vector, non-empty.")


class ClusterWriteCounts(BaseModel):
    """What :func:`write_clustering_run` actually changed, for the run's log.

    ``clusters_written`` and ``chunks_updated`` count rows the writer TOUCHED,
    not rows whose values changed — a second run with the same ``run_id`` writes
    the same numbers, which is what "idempotent" means here.
    """

    clusters_written: int = Field(description="Cluster rows upserted for this run.")
    chunks_updated: int = Field(
        description="Child chunk rows that matched and got cluster_id + viz."
    )
    clusters_deleted: int = Field(
        description="Previous runs' cluster rows removed for this user."
    )


# ---------------------------------------------------------------------------
# Read — the run's input
# ---------------------------------------------------------------------------


async def load_child_embeddings(
    client: AsyncMongoClient, database: str, user_id: PydanticObjectId
) -> list[ChildEmbeddingRow]:
    """Load ``user_id``'s embedded **Child chunk**s in ``_id`` order.

    Parents, ``document`` rows, entity nodes and children that have not been
    embedded yet are excluded BY QUERY (see :data:`_CHILD_FILTER`) — an
    unembedded child has no vector to project, and every other row type is
    deliberately vector-less.

    The projection is narrow on purpose: the whole user's corpus is loaded into
    memory at once, so a run must not also drag along the fields nothing here
    reads.

    Returns:
        The rows sorted by ``_id`` — the deterministic input order the recipe's
        reproducibility rests on.

    Raises:
        ValueError: A child's ``embedding`` is binData but not a float32 vector
            (e.g. raw subtype 0). The run fails loudly rather than skipping the
            row or clustering its bytes as floats.
    """

    collection = client[database][MEMORY_COLLECTION]
    cursor = collection.find(
        _embedded_children_filter(user_id),
        {"_id": 1, "embedding": 1},
    ).sort("_id", 1)

    rows = [_to_child_row(document) async for document in cursor]
    logger.info(
        "Loaded %d embedded child chunks for user_id=%s from %s",
        len(rows),
        user_id,
        MEMORY_COLLECTION,
    )
    return rows


def _to_child_row(document: dict[str, Any]) -> ChildEmbeddingRow:
    """Project one raw Mongo row onto :class:`ChildEmbeddingRow`."""

    return ChildEmbeddingRow(
        chunk_id=str(document["_id"]),
        # ``list(Binary)`` would yield BYTE ints — decode the float32 vector.
        embedding=from_stored_vector(document.get("embedding")),
    )


# ---------------------------------------------------------------------------
# Write — the run's output
# ---------------------------------------------------------------------------


async def write_clustering_run(
    client: AsyncMongoClient,
    database: str,
    user_id: PydanticObjectId,
    *,
    run_id: str,
    chunk_ids: list[str],
    labels: list[int],
    coords: list[tuple[float, float]],
    clusters: list[MemoryClusterInfo],
    now: datetime,
    config_fingerprint: str | None = None,
) -> ClusterWriteCounts:
    """Persist one **Clustering run**, replacing the previous one wholesale.

    Three statements, in this order:

    1. ``delete_many`` every ``memory_clusters`` row of this user whose
       ``run_id`` is not this run's. A cluster id that the previous run had and
       this one does not (7 clusters became 5) is removed here — nothing else
       would ever delete it.
    2. One unordered ``bulk_write`` of ``UpdateOne(..., upsert=True)`` per
       cluster, keyed on the DETERMINISTIC ``_id`` ``{user}:cluster:{id}``. So a
       cluster id that survives from the previous run is OVERWRITTEN by this
       run's label rather than duplicated, and no stale label can outlive step 1.
    3. One unordered ``bulk_write`` per chunk setting ``cluster_id``, ``viz``
       and ``updated_at``. NOISE is written too (``cluster_id = -1`` plus
       coordinates): the map draws it grey, and a chunk left carrying an older
       run's ``viz`` would be plotted in a space nothing else shares.

    Idempotent for a given ``run_id``: re-running it writes the same values to
    the same ``_id``s and deletes nothing.

    Args:
        run_id: This run's id (the Prefect flow-run id), stamped on every row.
        chunk_ids: The chunk ``_id``s, in the order they were clustered.
        labels: One HDBSCAN label per entry of ``chunk_ids``.
        coords: One ``(x, y)`` map point per entry of ``chunk_ids``.
        clusters: One entry per NON-noise cluster of this run.
        now: Timezone-aware UTC write timestamp.
        config_fingerprint: Stamped on every cluster row — the key
            :func:`unchanged_run_id` checks before a re-run.

    Raises:
        ValueError: ``chunk_ids`` / ``labels`` / ``coords`` disagree in length
            (a silent ``zip`` truncation would leave chunks holding a previous
            run's position), or ``now`` is naive.
    """

    if not (len(chunk_ids) == len(labels) == len(coords)):
        raise ValueError(
            "chunk_ids, labels and coords must describe the same chunks: got "
            f"{len(chunk_ids)} chunk_ids, {len(labels)} labels, "
            f"{len(coords)} coords"
        )
    if now.tzinfo is None:
        raise ValueError(f"now must be timezone-aware (UTC); got {now!r}")

    db = client[database]
    clusters_collection = db[MEMORY_CLUSTERS_COLLECTION]
    memory_collection = db[MEMORY_COLLECTION]

    deleted = await clusters_collection.delete_many(
        {"user_id": user_id, "run_id": {"$ne": run_id}}
    )

    clusters_written = 0
    if clusters:
        result = await clusters_collection.bulk_write(
            [
                UpdateOne(
                    {"_id": build_cluster_id(user_id, cluster.cluster_id)},
                    {
                        "$set": _cluster_row(
                            user_id,
                            cluster,
                            run_id=run_id,
                            now=now,
                            config_fingerprint=config_fingerprint,
                        ),
                        "$unset": _LEGACY_CLUSTER_FIELDS,
                    },
                    upsert=True,
                )
                for cluster in clusters
            ],
            ordered=False,
        )
        clusters_written = result.upserted_count + result.matched_count

    chunks_updated = 0
    if chunk_ids:
        result = await memory_collection.bulk_write(
            [
                UpdateOne(
                    {"_id": chunk_id},
                    {
                        "$set": {
                            "cluster_id": int(label),
                            "viz": {"x": float(x), "y": float(y), "run_id": run_id},
                            "updated_at": now,
                        }
                    },
                )
                for chunk_id, label, (x, y) in zip(
                    chunk_ids, labels, coords, strict=True
                )
            ],
            ordered=False,
        )
        chunks_updated = result.matched_count

    counts = ClusterWriteCounts(
        clusters_written=clusters_written,
        chunks_updated=chunks_updated,
        clusters_deleted=deleted.deleted_count,
    )
    logger.info(
        "Clustering run %s for user_id=%s: wrote %d cluster rows, updated %d "
        "chunks, deleted %d stale cluster rows",
        run_id,
        user_id,
        counts.clusters_written,
        counts.chunks_updated,
        counts.clusters_deleted,
    )
    return counts


def _cluster_row(
    user_id: PydanticObjectId,
    cluster: MemoryClusterInfo,
    *,
    run_id: str,
    now: datetime,
    config_fingerprint: str | None,
) -> dict[str, Any]:
    """The FULL ``memory_clusters`` row body — every field, every write.

    ``$set`` of the whole row (rather than the changed keys), plus the ``$unset``
    of :data:`_LEGACY_CLUSTER_FIELDS`, is what makes an overwrite of a
    surviving cluster id total: an older run's LLM ``summary`` can never linger
    beside a new run's ``Cluster N`` label.
    """

    return {
        "user_id": user_id,
        "run_id": run_id,
        "cluster_id": cluster.cluster_id,
        "label": cluster.label,
        "size": cluster.size,
        "centroid": {"x": cluster.centroid_x, "y": cluster.centroid_y},
        "config_fingerprint": config_fingerprint,
        "created_at": now,
    }


# ---------------------------------------------------------------------------
# Read — the map
# ---------------------------------------------------------------------------


async def latest_run_id(
    client: AsyncMongoClient, database: str, user_id: PydanticObjectId
) -> str | None:
    """The **Clustering run** a surface should read, or ``None`` if there is none.

    Normally the newest ``memory_clusters`` row's ``run_id``. An ALL-NOISE run
    writes no cluster row at all, though, and its chunks still carry
    coordinates — so fall back to the ``viz.run_id`` of any clustered child.
    Without the fallback such a run would read as "never clustered" and the
    operator would be told to run a phase that just ran.
    """

    db = client[database]
    row = await db[MEMORY_CLUSTERS_COLLECTION].find_one(
        {"user_id": user_id}, {"run_id": 1}, sort=[("created_at", -1)]
    )
    if row is not None:
        return str(row["run_id"])

    child = await db[MEMORY_COLLECTION].find_one(
        {"user_id": user_id, **_CHILD_ROW_FILTER, "viz.run_id": {"$exists": True}},
        {"viz": 1},
    )
    if child is None:
        return None
    return str((child.get("viz") or {})["run_id"])


async def unchanged_run_id(
    client: AsyncMongoClient,
    database: str,
    user_id: PydanticObjectId,
    *,
    config_fingerprint: str,
) -> str | None:
    """The latest run's id when re-running it would reproduce it, else ``None``.

    The clustering cache: a run is seeded, so the same chunks under the same
    config give the same clusters and labels — re-running only spends a UMAP
    fit. Current means all of:

    * the run has cluster rows, every one stamped with ``config_fingerprint``
      (an all-noise run has none and always re-runs);
    * every embedded child carries the run's ``viz.run_id`` (nothing added or
      re-embedded since);
    * the rows' sizes still sum to the run's clustered children (nothing
      deleted from a cluster since).

    Counts only — no vector is loaded.
    """

    run_id = await latest_run_id(client, database, user_id)
    if run_id is None:
        return None
    db = client[database]
    rows = (
        await db[MEMORY_CLUSTERS_COLLECTION]
        .find(
            {"user_id": user_id, "run_id": run_id},
            {"size": 1, "config_fingerprint": 1},
        )
        .to_list()
    )
    if not rows or any(
        row.get("config_fingerprint") != config_fingerprint for row in rows
    ):
        return None
    collection = db[MEMORY_COLLECTION]
    children = _embedded_children_filter(user_id)
    total = await collection.count_documents(children)
    in_run = {**children, "viz.run_id": run_id}
    if await collection.count_documents(in_run) != total:
        return None
    clustered = await collection.count_documents({**in_run, "cluster_id": {"$gte": 0}})
    if clustered != sum(int(row.get("size") or 0) for row in rows):
        return None
    return run_id


async def load_embedding_map(
    client: AsyncMongoClient,
    database: str,
    user_id: PydanticObjectId,
    *,
    max_docs: int | None = None,
) -> EmbeddingMap | None:
    """Read the **Embedding map** of ``user_id``'s latest run, capped by document.

    Surfaces READ a run's coordinates (ADR-007 §8). Points are the embedded
    children carrying THIS run's ``viz.run_id`` whose document (``sources``) is
    among the ``max_docs`` most-recent ones (default
    ``query.full_graph_max_docs``; ranked by ``rank_recent_documents``, the
    **Memory structure**'s ranking — ADR-013 §3). Everything else describes the
    WHOLE run: the clusters, ``clustered``, ``noise``, and ``unclustered`` =
    children with no assignment or a stale one, computed from counts — never
    from the plotted set, or the cap would read as staleness in the warning.

    With NO run yet nothing has 2D coordinates: the map comes back with no
    points and every child ``unclustered`` (``run_id=None``), so the caller
    answers the missing-2D warning instead of a picture. Surfaces never compute
    coordinates (ADR-007 §8) — they only ever draw what a run stored.

    Returns:
        The map, or ``None`` when the user has no embedded child at all — a
        caller shows an explanatory message, never an empty picture.
    """

    run_id = await latest_run_id(client, database, user_id)

    db = client[database]
    memory_collection = db[MEMORY_COLLECTION]
    kept, documents = await _recent_documents(memory_collection, user_id, max_docs)
    plotted: dict[str, Any] = {"sources": {"$in": _sources_of(kept)}}
    children = _embedded_children_filter(user_id)
    total_children = await memory_collection.count_documents(children)

    if total_children == 0:
        return None
    if run_id is None:
        return EmbeddingMap(
            run_id=None,
            clusters=[],
            points=[],
            total_children=total_children,
            unclustered=total_children,
            clustered=0,
            noise=0,
            plotted_documents=len(kept),
            total_documents=len(documents),
        )

    cluster_rows = (
        await db[MEMORY_CLUSTERS_COLLECTION]
        .find({"user_id": user_id, "run_id": run_id})
        .to_list()
    )
    clusters = sorted(
        (_to_cluster_info(row) for row in cluster_rows),
        key=lambda cluster: cluster.size,
        reverse=True,
    )

    point_cursor = memory_collection.find(
        {**children, "viz.run_id": run_id, **plotted}, _POINT_PROJECTION
    )
    points = [_to_map_point(document) async for document in point_cursor]

    clustered = await memory_collection.count_documents(
        {**children, "viz.run_id": run_id}
    )
    noise = await memory_collection.count_documents(
        {**children, "viz.run_id": run_id, "cluster_id": NOISE_LABEL}
    )
    logger.info(
        "Embedding map %s for user_id=%s: plotting %d of %d clustered chunks "
        "(the %d most-recent of %d documents)",
        run_id,
        user_id,
        len(points),
        clustered,
        len(kept),
        len(documents),
    )
    return EmbeddingMap(
        run_id=run_id,
        clusters=clusters,
        points=points,
        total_children=total_children,
        unclustered=total_children - clustered,
        clustered=clustered,
        noise=noise,
        plotted_documents=len(kept),
        total_documents=len(documents),
    )


async def load_query_map(
    client: AsyncMongoClient,
    database: str,
    user_id: PydanticObjectId,
    chunk_ids: set[str],
    *,
    max_docs: int | None = None,
) -> QueryMap:
    """The query view: ``chunk_ids`` over the rest of the memory, at UMAP coordinates.

    Both lists are drawn at the latest **Clustering run**'s stored ``viz`` — the
    same 2D space as the whole-memory map, so a match sits where the topic map
    puts it. The reference cloud is every placed child of the ``max_docs``
    most-recent documents; a match is added even when its document is older.
    A match without this run's coordinates (ingested since, never clustered,
    or a pending child the text leg hit) is counted in ``matched_without_2d``,
    never drawn. Surfaces compute no coordinates (ADR-007 §8).
    """

    run_id = await latest_run_id(client, database, user_id)
    if run_id is None:
        return QueryMap(
            matched=[],
            reference=[],
            matched_without_2d=len(chunk_ids),
            plotted_documents=0,
            total_documents=0,
        )
    collection = client[database][MEMORY_COLLECTION]
    kept, documents = await _recent_documents(collection, user_id, max_docs)
    cursor = collection.find(
        {
            **_embedded_children_filter(user_id),
            "viz.run_id": run_id,
            "$or": [
                {"sources": {"$in": _sources_of(kept)}},
                {"_id": {"$in": list(chunk_ids)}},
            ],
        },
        _POINT_PROJECTION,
    ).sort("_id", 1)
    points = [_to_map_point(document) async for document in cursor]
    matched = [point for point in points if point.chunk_id in chunk_ids]
    return QueryMap(
        matched=matched,
        reference=[point for point in points if point.chunk_id not in chunk_ids],
        matched_without_2d=len(chunk_ids) - len(matched),
        plotted_documents=len(kept),
        total_documents=len(documents),
    )


async def _recent_documents(
    collection: Any, user_id: PydanticObjectId, max_docs: int | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(kept, all)`` document rows: the ``max_docs`` most-recent, and every one.

    ``max_docs`` defaults to ``query.full_graph_max_docs``; ranked by
    ``rank_recent_documents``, the **Memory structure**'s ranking (ADR-013 §3).
    """

    max_docs = (
        max_docs if max_docs is not None else app_config.query.full_graph_max_docs
    )
    documents = await collection.find(
        {"user_id": user_id, "kind": "node", "type": "document"},
        {"_id": 1, "sources": 1, "properties.date": 1, "created_at": 1},
    ).to_list()
    return rank_recent_documents(documents, max_docs), documents


def _sources_of(documents: list[dict[str, Any]]) -> list[Any]:
    """The ``sources[0]`` provenance id of each document — what a child names."""

    return list({row["sources"][0] for row in documents if row.get("sources")})


def _to_cluster_info(row: dict[str, Any]) -> MemoryClusterInfo:
    """Flatten one ``memory_clusters`` row onto the legend's transit shape."""

    centroid = row.get("centroid") or {}
    return MemoryClusterInfo(
        cluster_id=int(row["cluster_id"]),
        label=row.get("label") or "",
        size=int(row.get("size") or 0),
        centroid_x=float(centroid.get("x", 0.0)),
        centroid_y=float(centroid.get("y", 0.0)),
    )


def _to_map_point(document: dict[str, Any]) -> MapPoint:
    """Project one clustered child row onto a drawable point."""

    properties = document.get("properties") or {}
    viz = document.get("viz") or {}
    cluster_id = document.get("cluster_id")
    return MapPoint(
        chunk_id=str(document["_id"]),
        x=float(viz["x"]),
        y=float(viz["y"]),
        # The writer always sets both fields in the same ``$set``; a row with
        # coordinates but no label could only come from a hand-edit, and grey
        # noise is the honest way to draw "we don't know which cluster".
        cluster_id=NOISE_LABEL if cluster_id is None else int(cluster_id),
        title=properties.get("title"),
        heading_path=list(properties.get("heading_path") or []),
        snippet=(properties.get("content") or "")[:_SNIPPET_CHARS],
    )
