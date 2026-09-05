"""The RAG load stage: one document + its **Parent chunk** / **Child chunk** rows.

ADR-006 decisions 2 and 4. This module is PURE except for the single
``bulk_write`` at the bottom: :func:`build_rag_row_ops` turns one split document
into the ``UpdateOne`` upserts that materialise the hierarchy

    document  <--parent_id--  chunk/parent  <--parent_id--  chunk/child

inside the ONE :data:`tree.entities.memory.MEMORY_COLLECTION` collection, and
:func:`load_rag_rows` flushes every op of a run in ONE
``bulk_write(ordered=False)``. Ordering is irrelevant: the ``_id``s are
deterministic (``build_node_id`` over deterministic names) and distinct, so a
re-run of the same document rewrites the same rows instead of duplicating them.

Row contract (the loader writes these rows in BOTH memory modes):

* ``document`` — metadata root. Never embedded.
* ``chunk/parent`` — the retrieval + LLM-extraction unit. Never embedded.
* ``chunk/child`` — the search unit. Carries the **Contextual header** vector.

Both chunk levels denormalise ``title`` and ``heading_path`` onto the row so the
indexing backfill rebuilds a byte-identical embedding text without a join.

The loader writes node types in :data:`tree.entities.memory.RAG_NODE_TYPES` and
nothing else — :func:`_build_node_op` raises on any other type, so a future edit
that tries to smuggle an entity row through the RAG path fails loudly instead of
silently making ``rag`` mode write graph rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from pymongo import UpdateOne

from tree.entities.memory import MEMORY_COLLECTION, RAG_NODE_TYPES, build_node_id
from tree.memory.rag.embedding import child_embedding_text
from tree.memory.rag.types import ParentChunk


def parent_chunk_name(source_uri: str, parent_index: int) -> str:
    """Deterministic ``name`` of a **Parent chunk** row: ``"{uri}#parent-{i}"``."""

    return f"{source_uri}#parent-{parent_index}"


def child_chunk_name(source_uri: str, parent_index: int, child_index: int) -> str:
    """Deterministic ``name`` of a **Child chunk** row: ``"{uri}#parent-{i}#child-{j}"``."""

    return f"{parent_chunk_name(source_uri, parent_index)}#child-{child_index}"


def document_row_id(user_id: PydanticObjectId, source_uri: str) -> str:
    """``_id`` of the ``document`` row — unchanged from the pre-ADR-006 pipeline."""

    return build_node_id(user_id, "document", source_uri)


def parent_row_id(user_id: PydanticObjectId, source_uri: str, parent_index: int) -> str:
    """``_id`` of a parent-chunk row.

    Also the ``chunk_id`` provenance stamped on every LLM emission extracted
    from that parent (ADR-006 decision 2 — it replaced a per-run ``uuid4()``),
    so an extracted entity points back at a row that actually exists.
    """

    return build_node_id(user_id, "chunk", parent_chunk_name(source_uri, parent_index))


def child_row_id(
    user_id: PydanticObjectId, source_uri: str, parent_index: int, child_index: int
) -> str:
    """``_id`` of a child-chunk row."""

    return build_node_id(
        user_id, "chunk", child_chunk_name(source_uri, parent_index, child_index)
    )


def build_rag_row_ops(
    *,
    user_id: PydanticObjectId,
    document_id: str,
    source_uri: str,
    source_type: str,
    title: str | None,
    date: str | None,
    parents: list[ParentChunk],
    child_vectors: dict[str, list[float]],
) -> list[UpdateOne]:
    """Build every upsert op for ONE split document, parents before children.

    ``child_vectors`` maps the **Contextual header** text (the key task ②
    embedded) to its vector; a child whose text is absent from the map is
    written with ``embedding=[]`` so the indexing backfill picks it up on the
    next run rather than failing the load.

    A document with no parents still yields its ``document`` row: the row is the
    provenance anchor (``sources``) the coordinator's pending-doc resolution
    reads, so an empty document must not look "pending" forever.
    """

    now = datetime.now(tz=UTC)
    doc_id = document_row_id(user_id, source_uri)
    ops = [
        _build_node_op(
            user_id=user_id,
            node_id=doc_id,
            node_type="document",
            name=source_uri,
            subtype=None,
            parent_id=None,
            chunk_index=None,
            properties={
                "source_type": source_type,
                "source_uri": source_uri,
                "title": title,
                "date": date,
            },
            embedding=[],
            source_document_id=document_id,
            now=now,
        )
    ]

    for parent in parents:
        pid = parent_row_id(user_id, source_uri, parent.index)
        ops.append(
            _build_node_op(
                user_id=user_id,
                node_id=pid,
                node_type="chunk",
                name=parent_chunk_name(source_uri, parent.index),
                subtype="parent",
                parent_id=doc_id,
                chunk_index=parent.index,
                properties=_chunk_properties(
                    source_type=source_type,
                    source_uri=source_uri,
                    date=date,
                    title=title,
                    heading_path=parent.heading_path,
                    content=parent.content,
                ),
                embedding=[],
                source_document_id=document_id,
                now=now,
            )
        )
        for child in parent.children:
            text = child_embedding_text(
                title=title,
                heading_path=parent.heading_path,
                content=child.content,
            )
            ops.append(
                _build_node_op(
                    user_id=user_id,
                    node_id=child_row_id(
                        user_id, source_uri, parent.index, child.index
                    ),
                    node_type="chunk",
                    name=child_chunk_name(source_uri, parent.index, child.index),
                    subtype="child",
                    parent_id=pid,
                    chunk_index=child.index,
                    properties=_chunk_properties(
                        source_type=source_type,
                        source_uri=source_uri,
                        date=date,
                        title=title,
                        heading_path=parent.heading_path,
                        content=child.content,
                    ),
                    embedding=child_vectors.get(text, []),
                    source_document_id=document_id,
                    now=now,
                )
            )

    return ops


def _chunk_properties(
    *,
    source_type: str,
    source_uri: str,
    date: str | None,
    title: str | None,
    heading_path: list[str],
    content: str,
) -> dict[str, Any]:
    """The denormalised :class:`~tree.entities.ontology.ChunkProperties` payload.

    ``title`` + ``heading_path`` are the two **Contextual header** inputs; they
    live on BOTH levels so a child row alone is enough to rebuild its embedding
    text and a parent row alone is enough to render a retrieval hit.
    """

    return {
        "source_type": source_type,
        "source_uri": source_uri,
        "date": date,
        "title": title,
        "heading_path": list(heading_path),
        "content": content,
    }


def _build_node_op(
    *,
    user_id: PydanticObjectId,
    node_id: str,
    node_type: str,
    name: str,
    subtype: str | None,
    parent_id: str | None,
    chunk_index: int | None,
    properties: dict[str, Any],
    embedding: list[float],
    source_document_id: str,
    now: datetime,
) -> UpdateOne:
    """One idempotent RAG-row upsert.

    An aggregation-pipeline update (same shape as the graph layer's structural
    upsert) so a re-run preserves ``created_at`` and unions ``sources`` instead
    of resetting them, while ``properties`` / ``embedding`` are REPLACED — the
    splitter is deterministic, so whatever it produced this run is the truth for
    this ``_id``, and a stale ``content`` merged in from a previous chunking
    config would silently poison retrieval.

    ``properties`` MUST go through ``$literal`` to actually replace. A bare dict
    on the right-hand side of a pipeline ``$set`` is an *object specification*:
    MongoDB (verified on 8.2.5) assigns it key by key and KEEPS every
    pre-existing key of the embedded document, so ``{"$set": {"properties":
    {"content": "new"}}}`` merges instead of replacing and a ``heading_path``
    written under an older chunking config survives forever. ``$literal`` makes
    the dict a constant, which overwrites the sub-document wholesale — the same
    wrapper, for the same reason, as ``add_entity._per_key_merge_expr``.
    """

    if node_type not in RAG_NODE_TYPES:
        raise ValueError(
            f"the RAG loader only writes {sorted(RAG_NODE_TYPES)} rows; "
            f"got type={node_type!r}"
        )
    return UpdateOne(
        {"_id": node_id},
        [
            {
                "$set": {
                    "user_id": user_id,
                    "kind": "node",
                    "type": node_type,
                    "name": name,
                    "subtype": subtype,
                    "parent_id": parent_id,
                    "chunk_index": chunk_index,
                    "canonical_name": {"$ifNull": ["$canonical_name", name]},
                    "properties": {"$literal": properties},
                    "aliases": {"$ifNull": ["$aliases", []]},
                    "confidence": {"$ifNull": ["$confidence", 1.0]},
                    "embedding": embedding,
                    "sources": {
                        "$setUnion": [
                            {"$ifNull": ["$sources", []]},
                            [PydanticObjectId(source_document_id)],
                        ]
                    },
                    "created_at": {"$ifNull": ["$created_at", now]},
                    "updated_at": now,
                }
            }
        ],
        upsert=True,
    )


async def load_rag_rows(*, database: Any, ops: list[UpdateOne]) -> int:
    """Flush every RAG row of a run in ONE ``bulk_write(ordered=False)``.

    Returns the number of ops issued (== rows written, since every op is an
    upsert on a distinct deterministic ``_id``). An empty op list skips the
    round-trip entirely — ``bulk_write([])`` raises.
    """

    if not ops:
        return 0
    await database[MEMORY_COLLECTION].bulk_write(ops, ordered=False)
    return len(ops)
