"""Human-review API for flagged SAME_AS pairs (#014).

Three async functions form the entire review surface:

* :func:`find_pending_duplicates` — list pending SAME_AS edges joined with
  their endpoints.
* :func:`review_duplicate` — confirm (merge) or reject a pending pair.
* :func:`get_same_as_cluster` — single-hop SAME_AS neighborhood of a node.

The confirm path **reuses** the same ``_apply_merge`` strategy dispatcher
from :mod:`tree.memory.extraction.add_entity` so the auto-merge surface
(#011) and the human-merge surface (#014) cannot drift. After applying
the strategy on the winner, the function:

1. Transfers every non-SAME_AS edge whose source or target was the loser
   to the winner. Edges that collide after re-keying (the winner already
   has the same edge) are merged via ``$setUnion`` on ``sources``; the
   original loser-keyed edge is then deleted.
2. Tombstones the loser with ``merged_into`` + ``merged_at`` so the
   dedup pipeline's ``$vectorSearch`` filter excludes it on future runs.
3. Marks the SAME_AS audit edge ``status="confirmed"`` and stamps the
   reviewer + timestamps.

The reject path only writes ``status="rejected"`` on the audit edge;
``dedupe_entity`` (#010) consumes that edge to skip the pair on future
ingests.

Confirm is **idempotent**: a second call with the same args observes
``status="confirmed"`` and returns the persisted :class:`ReviewResult`
without re-merging. Cross-decision transitions (confirm-after-reject or
reject-after-confirm) raise :class:`ValueError` — accidental undo is
impossible.

``get_same_as_cluster`` is **single-hop only**; transitive
``SAME_AS*1..3`` traversal is explicit out-of-scope.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from beanie import PydanticObjectId

from tree.entities.knowledge_graph import (
    EdgeType,
    NodeType,
    build_edge_id,
)
from tree.memory.extraction.add_entity import _apply_merge
from tree.memory.review.types import (
    MergeStrategy,
    PendingDuplicate,
    ReviewDecision,
    ReviewResult,
)

if TYPE_CHECKING:
    from pymongo.asynchronous.database import AsyncDatabase


logger = logging.getLogger(__name__)


_KG_COLLECTION = "knowledge_graph"


# ---------------------------------------------------------------------------
# find_pending_duplicates
# ---------------------------------------------------------------------------


async def find_pending_duplicates(
    database: AsyncDatabase,
    *,
    user_id: PydanticObjectId,
    entity_type: NodeType | None = None,
    limit: int = 50,
) -> list[PendingDuplicate]:
    """List SAME_AS edges with ``status="pending"`` for ``user_id``.

    Sorts by similarity score (``properties.confidence``) descending so
    the most-likely duplicates surface first. The optional ``entity_type``
    filter is applied via a ``$lookup`` join onto the source node — only
    pairs where the source node has that type are returned (callers can
    rely on source/target type being equal because the dedup pipeline
    only emits SAME_AS between same-typed nodes).

    Tenant scoping:
        Every ``$match`` (including the two ``$lookup`` pipelines that
        hydrate source/target nodes) carries an explicit ``user_id``
        predicate. Cross-tenant rows are invisible by construction.

    Args:
        database: ``AsyncDatabase`` handle.
        user_id: The tenant whose pending pairs are returned. Required.
        entity_type: Optional :class:`NodeType` filter. ``None`` returns
            pending pairs of every type.
        limit: Maximum number of pairs to return.

    Returns:
        A list of :class:`PendingDuplicate`, possibly empty.
    """

    if limit <= 0:
        return []

    collection = database[_KG_COLLECTION]
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "user_id": user_id,
                "kind": "edge",
                "type": EdgeType.SAME_AS.value,
                "properties.status": "pending",
            }
        },
        {"$sort": {"properties.confidence": -1, "_id": 1}},
        {"$limit": limit},
        {
            "$lookup": {
                "from": _KG_COLLECTION,
                "let": {"src_id": "$source_node_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$_id", "$$src_id"]},
                            "user_id": user_id,
                        }
                    }
                ],
                "as": "_source_node",
            }
        },
        {
            "$lookup": {
                "from": _KG_COLLECTION,
                "let": {"tgt_id": "$target_node_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$_id", "$$tgt_id"]},
                            "user_id": user_id,
                        }
                    }
                ],
                "as": "_target_node",
            }
        },
        {
            "$match": {
                "_source_node": {"$ne": []},
                "_target_node": {"$ne": []},
            }
        },
    ]

    if entity_type is not None:
        pipeline.append(
            {
                "$match": {
                    "_source_node.0.type": entity_type.value,
                }
            }
        )

    cursor = await collection.aggregate(pipeline)
    rows: list[PendingDuplicate] = []
    async for doc in cursor:
        source_node = doc["_source_node"][0]
        target_node = doc["_target_node"][0]
        props = doc.get("properties") or {}

        match_type_raw = props.get("match_type") or "embedding"
        match_type = cast("str", match_type_raw)
        if match_type not in {"embedding", "fuzzy", "both"}:
            match_type = "embedding"

        flagged_at = props.get("created_at") or doc.get("created_at")
        if flagged_at is None:
            flagged_at = datetime.now(tz=UTC)

        source_type_raw = source_node.get("type") or doc.get("source_type")
        node_type = NodeType(source_type_raw)

        rows.append(
            PendingDuplicate(
                source_node_id=str(doc["source_node_id"]),
                target_node_id=str(doc["target_node_id"]),
                source_name=str(source_node.get("name") or doc["source_node_id"]),
                target_name=str(target_node.get("name") or doc["target_node_id"]),
                entity_type=node_type,
                similarity_score=float(props.get("confidence", 0.0)),
                match_type=cast(
                    "Any", match_type
                ),  # narrowed above; cast for the dataclass Literal.
                flagged_at=flagged_at,
                edge_id=str(doc["_id"]),
            )
        )

    return rows


# ---------------------------------------------------------------------------
# get_same_as_cluster
# ---------------------------------------------------------------------------


async def get_same_as_cluster(
    database: AsyncDatabase,
    node_id: str,
    *,
    user_id: PydanticObjectId,
) -> set[str]:
    """Return ``node_id`` plus every node it shares a SAME_AS edge with.

    Single-hop only. Status is ignored — confirmed, pending, and rejected
    SAME_AS edges all contribute neighbors. The input node id is always
    in the returned set so callers can pass the set directly to query
    helpers without re-adding the seed.

    Tenant scoping:
        The ``find(...)`` over SAME_AS edges carries an explicit
        ``user_id`` predicate. A cross-tenant ``node_id`` returns just
        the seed (no edges visible under the bound tenant).

    Args:
        database: ``AsyncDatabase`` handle.
        node_id: The ``_id`` of the node to centre the cluster on.
        user_id: The tenant whose SAME_AS edges are traversed. Required.

    Returns:
        A set of node ``_id`` strings.
    """

    collection = database[_KG_COLLECTION]
    cluster: set[str] = {node_id}

    cursor = collection.find(
        {
            "user_id": user_id,
            "kind": "edge",
            "type": EdgeType.SAME_AS.value,
            "$or": [
                {"source_node_id": node_id},
                {"target_node_id": node_id},
            ],
        },
        {"source_node_id": 1, "target_node_id": 1},
    )
    async for doc in cursor:
        src = doc.get("source_node_id")
        tgt = doc.get("target_node_id")
        if src and src != node_id:
            cluster.add(str(src))
        if tgt and tgt != node_id:
            cluster.add(str(tgt))

    return cluster


# ---------------------------------------------------------------------------
# review_duplicate
# ---------------------------------------------------------------------------


async def review_duplicate(
    database: AsyncDatabase,
    *,
    user_id: PydanticObjectId,
    source_node_id: str,
    target_node_id: str,
    decision: ReviewDecision,
    reviewed_by: str,
    merge_strategy: MergeStrategy = MergeStrategy.KEEP_PRIMARY,
) -> ReviewResult:
    """Confirm or reject a pending SAME_AS pair belonging to ``user_id``.

    Locates the SAME_AS edge between the two nodes (either direction). On
    ``CONFIRM`` picks a winner via the tiebreaker rule (older
    ``created_at`` wins → higher ``confidence`` wins → lexicographically
    smaller ``_id`` wins), applies the merge strategy through the shared
    :func:`tree.memory.extraction.add_entity._apply_merge` dispatcher,
    transfers every loser-keyed edge to the winner, tombstones the loser,
    and stamps the audit edge ``status="confirmed"``.

    On ``REJECT`` only the audit edge is mutated (``status="rejected"``);
    nodes and edges stay put. ``dedupe_entity`` (#010) consumes the
    rejected audit edge on future runs to skip the pair.

    Idempotency:

    * A second ``CONFIRM`` after a successful ``CONFIRM`` returns the
      persisted :class:`ReviewResult` without re-doing the merge. The
      winner/loser identities are recovered from the loser's
      ``merged_into`` tombstone.
    * A second ``REJECT`` after a successful ``REJECT`` is a no-op that
      refreshes ``reviewed_by``/``reviewed_at`` and returns.
    * A cross-decision transition (confirm-after-reject or
      reject-after-confirm) raises :class:`ValueError`.

    Tenant scoping:
        Every ``find_one`` / ``update_one`` carries an explicit
        ``user_id`` predicate. Calling this with a pair whose SAME_AS
        edge belongs to a different tenant raises ``ValueError`` (the
        edge is invisible under ``user_id``).

    Args:
        database: ``AsyncDatabase`` handle.
        user_id: The tenant the SAME_AS edge belongs to. Required.
        source_node_id: One endpoint of the SAME_AS edge.
        target_node_id: The other endpoint of the SAME_AS edge.
        decision: :class:`ReviewDecision.CONFIRM` or
            :class:`ReviewDecision.REJECT`.
        reviewed_by: Free-form reviewer identifier (e.g. an email or an
            agent handle); persisted on the audit edge.
        merge_strategy: Which :class:`MergeStrategy` to dispatch on the
            confirm path. Ignored on reject.

    Returns:
        A populated :class:`ReviewResult`.

    Raises:
        ValueError: When no SAME_AS edge exists between the two nodes
            (under ``user_id``), either node is missing, or the
            persisted status disagrees with ``decision`` (cross-decision
            transition).
    """

    collection = database[_KG_COLLECTION]
    now = datetime.now(tz=UTC)

    # 1. Locate the SAME_AS edge in either direction. We don't pre-compute
    #    build_edge_id because we don't know which side is source vs target.
    edge_doc = await collection.find_one(
        {
            "user_id": user_id,
            "kind": "edge",
            "type": EdgeType.SAME_AS.value,
            "$or": [
                {
                    "source_node_id": source_node_id,
                    "target_node_id": target_node_id,
                },
                {
                    "source_node_id": target_node_id,
                    "target_node_id": source_node_id,
                },
            ],
        }
    )
    if edge_doc is None:
        raise ValueError(
            f"review_duplicate: no SAME_AS edge between "
            f"{source_node_id!r} and {target_node_id!r}"
        )

    edge_id = str(edge_doc["_id"])
    current_status = (edge_doc.get("properties") or {}).get("status")

    if decision is ReviewDecision.REJECT:
        return await _handle_reject(
            collection=collection,
            user_id=user_id,
            edge_id=edge_id,
            current_status=current_status,
            reviewed_by=reviewed_by,
            now=now,
        )

    return await _handle_confirm(
        collection=collection,
        user_id=user_id,
        edge_doc=edge_doc,
        edge_id=edge_id,
        current_status=current_status,
        reviewed_by=reviewed_by,
        merge_strategy=merge_strategy,
        now=now,
    )


# ---------------------------------------------------------------------------
# Internals — reject
# ---------------------------------------------------------------------------


async def _handle_reject(
    *,
    collection: Any,
    user_id: PydanticObjectId,
    edge_id: str,
    current_status: str | None,
    reviewed_by: str,
    now: datetime,
) -> ReviewResult:
    if current_status == "confirmed":
        raise ValueError(
            f"SAME_AS pair {edge_id!r} is already in terminal state "
            f"{current_status!r}; cannot transition to 'reject'"
        )

    await collection.update_one(
        {"_id": edge_id, "user_id": user_id},
        {
            "$set": {
                "properties.status": "rejected",
                "properties.reviewed_by": reviewed_by,
                "properties.reviewed_at": now,
                "properties.updated_at": now,
                "updated_at": now,
            }
        },
    )

    return ReviewResult(
        decision=ReviewDecision.REJECT,
        winner_node_id=None,
        loser_node_id=None,
        applied_strategy=None,
        edges_transferred=0,
        same_as_edge_id=edge_id,
    )


# ---------------------------------------------------------------------------
# Internals — confirm
# ---------------------------------------------------------------------------


async def _handle_confirm(
    *,
    collection: Any,
    user_id: PydanticObjectId,
    edge_doc: dict[str, Any],
    edge_id: str,
    current_status: str | None,
    reviewed_by: str,
    merge_strategy: MergeStrategy,
    now: datetime,
) -> ReviewResult:
    if current_status == "rejected":
        raise ValueError(
            f"SAME_AS pair {edge_id!r} is already in terminal state "
            f"{current_status!r}; cannot transition to 'confirm'"
        )

    src_id = str(edge_doc["source_node_id"])
    tgt_id = str(edge_doc["target_node_id"])

    # Idempotency: if already confirmed, recover state and return.
    if current_status == "confirmed":
        return await _build_idempotent_confirm_result(
            collection=collection,
            user_id=user_id,
            edge_doc=edge_doc,
            edge_id=edge_id,
            src_id=src_id,
            tgt_id=tgt_id,
        )

    # Load both endpoint nodes (tenant-scoped).
    src_node = await collection.find_one({"_id": src_id, "user_id": user_id})
    tgt_node = await collection.find_one({"_id": tgt_id, "user_id": user_id})
    if src_node is None or tgt_node is None:
        raise ValueError(
            f"review_duplicate: cannot confirm — one or both endpoint "
            f"nodes missing (source={src_id!r}, target={tgt_id!r})"
        )

    winner, loser = _decide_winner(src_node, tgt_node)
    winner_id = str(winner["_id"])
    loser_id = str(loser["_id"])

    # Read entity_type off the winner (source_type/target_type fall back).
    winner_type_raw = (
        winner.get("type") or edge_doc.get("source_type") or edge_doc.get("target_type")
    )
    entity_type = NodeType(winner_type_raw)

    # Build the "incoming" view from the loser so the existing strategy
    # handlers append the loser's name to aliases and copy in its
    # properties for MERGE_PROPERTIES.
    loser_name = str(loser.get("name") or loser_id)
    loser_properties: dict[str, Any] = dict(loser.get("properties") or {})
    loser_sources = loser.get("sources") or []
    loser_confidence = loser.get("confidence")
    if loser_confidence is not None and "confidence" not in loser_properties:
        loser_properties["confidence"] = loser_confidence

    # ------------------------------------------------------------------
    # Step 1 — apply merge strategy on winner.
    # ------------------------------------------------------------------
    await _apply_merge(
        collection=collection,
        target_id=winner_id,
        entity_type=entity_type,
        incoming_name=loser_name,
        incoming_properties=loser_properties,
        # Use a synthetic source id so the union step stays well-defined
        # even when the loser has no sources of its own.
        source_id=str(loser_sources[0]) if loser_sources else f"merge:{loser_id}",
        strategy=merge_strategy,
        now=now,
    )

    # If the loser has additional sources, union them into the winner.
    # This is a one-shot post-merge $set so we keep the audit trail.
    if len(loser_sources) > 1:
        await collection.update_one(
            {"_id": winner_id, "user_id": user_id},
            [
                {
                    "$set": {
                        "sources": {
                            "$setUnion": [
                                {"$ifNull": ["$sources", []]},
                                list(loser_sources),
                            ]
                        },
                        "updated_at": now,
                    }
                }
            ],
        )

    # ------------------------------------------------------------------
    # Step 2 — transfer edges from loser → winner.
    # ------------------------------------------------------------------
    edges_transferred = await _transfer_edges(
        collection=collection,
        user_id=user_id,
        loser_id=loser_id,
        winner_id=winner_id,
        audit_edge_id=edge_id,
        now=now,
    )

    # ------------------------------------------------------------------
    # Step 3 — tombstone the loser.
    # ------------------------------------------------------------------
    await collection.update_one(
        {"_id": loser_id, "user_id": user_id},
        {
            "$set": {
                "merged_into": winner_id,
                "merged_at": now,
                "updated_at": now,
            }
        },
    )

    # ------------------------------------------------------------------
    # Step 4 — stamp the audit edge.
    # ------------------------------------------------------------------
    await collection.update_one(
        {"_id": edge_id, "user_id": user_id},
        {
            "$set": {
                "properties.status": "confirmed",
                "properties.reviewed_by": reviewed_by,
                "properties.reviewed_at": now,
                "properties.updated_at": now,
                "properties.winner_node_id": winner_id,
                "properties.loser_node_id": loser_id,
                "properties.applied_strategy": merge_strategy.value,
                "properties.edges_transferred": edges_transferred,
                "updated_at": now,
            }
        },
    )

    return ReviewResult(
        decision=ReviewDecision.CONFIRM,
        winner_node_id=winner_id,
        loser_node_id=loser_id,
        applied_strategy=merge_strategy,
        edges_transferred=edges_transferred,
        same_as_edge_id=edge_id,
    )


async def _build_idempotent_confirm_result(
    *,
    collection: Any,
    user_id: PydanticObjectId,
    edge_doc: dict[str, Any],
    edge_id: str,
    src_id: str,
    tgt_id: str,
) -> ReviewResult:
    """Reconstruct the original confirm's :class:`ReviewResult` from disk.

    Prefers the audit fields written on the SAME_AS edge at confirm time
    (``properties.winner_node_id``, ``properties.loser_node_id``,
    ``properties.applied_strategy``, ``properties.edges_transferred``).
    Falls back to the loser's ``merged_into`` tombstone when the audit
    fields are missing (older confirms written before this code shipped).
    """

    props = edge_doc.get("properties") or {}
    winner_id = props.get("winner_node_id")
    loser_id = props.get("loser_node_id")
    applied_strategy_raw = props.get("applied_strategy")
    edges_transferred = props.get("edges_transferred")

    if winner_id is None or loser_id is None:
        # Fallback: read the merged_into tombstone on whichever endpoint
        # was the loser. The remaining endpoint must be the winner.
        src_node = await collection.find_one(
            {"_id": src_id, "user_id": user_id}, {"merged_into": 1}
        )
        tgt_node = await collection.find_one(
            {"_id": tgt_id, "user_id": user_id}, {"merged_into": 1}
        )
        if src_node and src_node.get("merged_into") == tgt_id:
            loser_id = src_id
            winner_id = tgt_id
        elif tgt_node and tgt_node.get("merged_into") == src_id:
            loser_id = tgt_id
            winner_id = src_id
        else:
            raise ValueError(
                f"SAME_AS pair {edge_id!r} is marked 'confirmed' but no "
                "tombstone or audit fields are present; database is in an "
                "inconsistent state."
            )

    applied_strategy: MergeStrategy | None
    if applied_strategy_raw is None:
        applied_strategy = None
    else:
        applied_strategy = MergeStrategy(applied_strategy_raw)

    return ReviewResult(
        decision=ReviewDecision.CONFIRM,
        winner_node_id=str(winner_id),
        loser_node_id=str(loser_id),
        applied_strategy=applied_strategy,
        edges_transferred=int(edges_transferred or 0),
        same_as_edge_id=edge_id,
    )


def _decide_winner(
    a: dict[str, Any], b: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pick the winner between two node dicts.

    Tiebreaker:
        1. Older ``created_at`` wins.
        2. Higher ``confidence`` wins.
        3. Lexicographically smaller ``_id`` wins (final, deterministic).
    """

    a_created = a.get("created_at")
    b_created = b.get("created_at")
    if a_created is not None and b_created is not None and a_created != b_created:
        return (a, b) if a_created < b_created else (b, a)

    a_conf = float(a.get("confidence") or 0.0)
    b_conf = float(b.get("confidence") or 0.0)
    if a_conf != b_conf:
        return (a, b) if a_conf > b_conf else (b, a)

    return (a, b) if str(a["_id"]) <= str(b["_id"]) else (b, a)


# ---------------------------------------------------------------------------
# Internals — edge transfer
# ---------------------------------------------------------------------------


async def _transfer_edges(
    *,
    collection: Any,
    user_id: PydanticObjectId,
    loser_id: str,
    winner_id: str,
    audit_edge_id: str,
    now: datetime,
) -> int:
    """Re-key every loser-incident edge to the winner.

    Walks every edge (excluding the SAME_AS audit edge being mutated)
    whose ``source_node_id`` or ``target_node_id`` is the loser. For
    each:

    1. Compute the new ``_id`` by substituting ``winner_id`` for
       ``loser_id`` on whichever endpoint matched.
    2. If the new ``_id`` equals the audit edge id, skip — that edge
       is the SAME_AS audit row itself (defensive; the audit-edge guard
       above usually excludes it).
    3. If the new edge would be a self-loop (winner→winner), drop the
       loser-keyed row outright — self-edges are meaningless.
    4. Upsert the winner-keyed row with ``$setOnInsert`` for identity
       fields and ``$set`` for ``updated_at``. Existing winner-keyed
       rows merge ``sources`` via ``$setUnion`` so provenance is
       preserved without duplicates.
    5. Delete the loser-keyed row.

    Returns:
        The number of loser-incident edges that were processed (transfer
        + delete and direct delete count). The SAME_AS audit edge is not
        counted.
    """

    transferred = 0

    cursor = collection.find(
        {
            "user_id": user_id,
            "kind": "edge",
            "_id": {"$ne": audit_edge_id},
            "$or": [
                {"source_node_id": loser_id},
                {"target_node_id": loser_id},
            ],
        }
    )

    async for edge in cursor:
        old_id = str(edge["_id"])
        old_src = str(edge.get("source_node_id") or "")
        old_tgt = str(edge.get("target_node_id") or "")
        edge_type_raw = edge.get("type")
        if edge_type_raw is None:
            # Defensive — skip malformed edges.
            await collection.delete_one({"_id": old_id, "user_id": user_id})
            continue

        new_src = winner_id if old_src == loser_id else old_src
        new_tgt = winner_id if old_tgt == loser_id else old_tgt

        # Self-loop after substitution → drop without re-keying.
        if new_src == new_tgt:
            await collection.delete_one({"_id": old_id, "user_id": user_id})
            transferred += 1
            continue

        edge_type = EdgeType(edge_type_raw)
        new_id = build_edge_id(new_src, edge_type, new_tgt)

        if new_id == audit_edge_id:
            # Defensive: never overwrite the audit edge.
            await collection.delete_one({"_id": old_id, "user_id": user_id})
            continue

        if new_id == old_id:
            # No-op (already keyed on winner; should not happen because
            # the find filter requires loser membership, but defensive).
            continue

        # Carry the loser-edge sources forward via $setUnion on upsert.
        loser_sources = list(edge.get("sources") or [])
        loser_properties = edge.get("properties") or {}

        await collection.update_one(
            {"_id": new_id, "user_id": user_id},
            [
                {
                    "$set": {
                        "user_id": user_id,
                        "kind": "edge",
                        "type": edge_type.value,
                        "source_node_id": new_src,
                        "source_type": {
                            "$ifNull": [
                                "$source_type",
                                edge.get("source_type"),
                            ]
                        },
                        "target_node_id": new_tgt,
                        "target_type": {
                            "$ifNull": [
                                "$target_type",
                                edge.get("target_type"),
                            ]
                        },
                        "sources": {
                            "$setUnion": [
                                {"$ifNull": ["$sources", []]},
                                loser_sources,
                            ]
                        },
                        "properties": {
                            "$mergeObjects": [
                                loser_properties,
                                {"$ifNull": ["$properties", {}]},
                            ]
                        },
                        "created_at": {
                            "$ifNull": [
                                "$created_at",
                                edge.get("created_at") or now,
                            ]
                        },
                        "updated_at": now,
                    }
                }
            ],
            upsert=True,
        )

        await collection.delete_one({"_id": old_id, "user_id": user_id})
        transferred += 1

    return transferred
