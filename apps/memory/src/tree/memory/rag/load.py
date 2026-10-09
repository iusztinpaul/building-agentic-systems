"""The RAG load stage: one document + its **Parent chunk** / **Child chunk** rows.

ADR-006 decisions 2 and 4. This module is PURE except for the
``bulk_write``s at the bottom: :func:`build_rag_row_ops` turns one split document
into the ``UpdateOne`` upserts that materialise the hierarchy

    document  <--parent_id--  chunk/parent  <--parent_id--  chunk/child

inside the ONE :data:`tree.entities.memory.MEMORY_COLLECTION` collection, and
:func:`load_rag_rows` flushes them in small ``bulk_write(ordered=False)``
batches of WHOLE documents. Ordering is irrelevant: the ``_id``s are
deterministic (``build_rag_row_id`` over ``source_uri`` + position) and distinct, so a
re-run of the same document rewrites the same rows instead of duplicating them.

Row contract (the loader writes these rows in BOTH memory modes):

* ``document`` — metadata root. Never embedded.
* ``chunk/parent`` — the retrieval + LLM-extraction unit. Never embedded.
* ``chunk/child`` — the search unit. Carries the **Contextual header** vector,
  stored as float32 ``binData`` (:func:`tree.entities.memory.to_stored_vector`).

Rows without a vector have NO ``embedding`` field (task 175) — never an empty one.

Both chunk levels denormalise ``title`` and ``heading_path`` onto the row so the
indexing backfill rebuilds a byte-identical embedding text without a join.

The rows carry no ``name`` and no entity-resolution fields (``canonical_name`` /
``aliases`` / ``confidence``): their ``_id`` is derived from ``source_uri`` +
position, so nothing ever resolves or merges them. The names below are only the
graphrag extractor's edge-endpoint keys, remapped to these ``_id``s. A stored
URI-shaped ``name`` would also be tokenised by the **Text search index**
(``name`` is one of its text paths), letting a query match a document through
its URL instead of its content; the URI already lives in
``properties.source_uri``. Lineage back to the
``Document`` collection is ``sources``.

The loader writes node types in :data:`tree.entities.memory.RAG_NODE_TYPES` and
nothing else — :func:`_build_node_op` raises on any other type, so a future edit
that tries to smuggle an entity row through the RAG path fails loudly instead of
silently making ``rag`` mode write graph rows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from pymongo import UpdateOne
from pymongo.errors import AutoReconnect, NetworkTimeout

from tree.entities.memory import (
    MEMORY_COLLECTION,
    RAG_NODE_TYPES,
    build_rag_row_id,
    to_stored_vector,
)
from tree.memory.rag.embedding import child_embedding_text
from tree.memory.rag.types import ParentChunk

logger = logging.getLogger(__name__)

LOAD_BATCH_OPS = 500
"""Target ops per ``bulk_write`` (~3 MB at ~5.7 KB a child row). One write per
run (~37k ops, ~200 MB for the prod corpus) ran ~6 min on Atlas M0 until the
server closed the connection and NOTHING landed; small batches finish in
seconds and keep the progress of the ones that did."""

LOAD_RETRIES = 3
LOAD_RETRY_BASE_DELAY_S = 2.0


def parent_chunk_name(source_uri: str, parent_index: int) -> str:
    """Extractor endpoint name of a **Parent chunk** row: ``"{uri}#parent-{i}"``."""

    return f"{source_uri}#parent-{parent_index}"


def child_chunk_name(source_uri: str, parent_index: int, child_index: int) -> str:
    """Extractor endpoint name of a **Child chunk** row: ``"{uri}#parent-{i}#child-{j}"``."""

    return f"{parent_chunk_name(source_uri, parent_index)}#child-{child_index}"


def document_row_id(user_id: PydanticObjectId, source_uri: str) -> str:
    """``_id`` of the ``document`` row."""

    return build_rag_row_id(user_id, "document", source_uri)


def parent_row_id(user_id: PydanticObjectId, source_uri: str, parent_index: int) -> str:
    """``_id`` of a parent-chunk row.

    Also the ``chunk_id`` provenance stamped on every LLM emission extracted
    from that parent (ADR-006 decision 2 — it replaced a per-run ``uuid4()``),
    so an extracted entity points back at a row that actually exists.
    """

    return build_rag_row_id(user_id, "chunk", source_uri, parent_index)


def child_row_id(
    user_id: PydanticObjectId, source_uri: str, parent_index: int, child_index: int
) -> str:
    """``_id`` of a child-chunk row."""

    return build_rag_row_id(user_id, "chunk", source_uri, parent_index, child_index)


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
    written without an ``embedding`` so the indexing backfill picks it up on the
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
            subtype=None,
            parent_id=None,
            chunk_index=None,
            properties={
                "source_type": source_type,
                "source_uri": source_uri,
                "title": title,
                "date": date,
            },
            embedding=None,
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
                embedding=None,
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
                    embedding=child_vectors.get(text),
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
    subtype: str | None,
    parent_id: str | None,
    chunk_index: int | None,
    properties: dict[str, Any],
    embedding: list[float] | None,
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

    ``embedding`` is a float32 ``binData`` constant (no ``$literal`` needed: a
    ``Binary`` is not an expression) or ``$$REMOVE`` when there is no vector, so
    a re-run that lost a child's vector leaves it pending for the backfill
    instead of keeping a stale one.

    The entity-naming fields (``name`` / ``canonical_name`` / ``aliases`` /
    ``confidence``) are never written: a RAG row's ``_id`` is positional, so it
    is never resolved or merged.
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
                    "subtype": subtype,
                    "parent_id": parent_id,
                    "chunk_index": chunk_index,
                    "properties": {"$literal": properties},
                    "embedding": (
                        to_stored_vector(embedding) if embedding else "$$REMOVE"
                    ),
                    "sources": {
                        "$setUnion": [
                            {"$ifNull": ["$sources", []]},
                            [PydanticObjectId(source_document_id)],
                        ]
                    },
                    "created_at": {"$ifNull": ["$created_at", now]},
                    "updated_at": now,
                }
            },
        ],
        upsert=True,
    )


def batch_document_ops(
    documents_ops: list[list[UpdateOne]], batch_ops: int = LOAD_BATCH_OPS
) -> list[list[UpdateOne]]:
    """Pack per-document op lists into batches of about ``batch_ops`` ops.

    A document is NEVER split across batches: a document counts as ingested as
    soon as ANY of its rows carries it in ``sources``, so a batch that failed
    halfway through a document would mark it done with rows missing, and the
    next run would never pick it up again. A document larger than ``batch_ops``
    gets a batch of its own.
    """

    batches: list[list[UpdateOne]] = []
    current: list[UpdateOne] = []
    for ops in documents_ops:
        if current and len(current) + len(ops) > batch_ops:
            batches.append(current)
            current = []
        current.extend(ops)
    if current:
        batches.append(current)
    return batches


async def _write_batch(collection: Any, ops: list[UpdateOne]) -> None:
    """One ``bulk_write``, retried with backoff on a dropped connection.

    Safe to retry: every op is an upsert on a deterministic ``_id``, so a batch
    the server partly applied before the drop is rewritten, not duplicated.
    """

    for attempt in range(1, LOAD_RETRIES + 1):
        try:
            await collection.bulk_write(ops, ordered=False)
            return
        except AutoReconnect, NetworkTimeout:
            if attempt == LOAD_RETRIES:
                raise
            delay = LOAD_RETRY_BASE_DELAY_S * 2 ** (attempt - 1)
            logger.warning(
                "load_rag_rows: bulk_write of %d ops dropped (attempt %d/%d); "
                "retrying in %.0fs",
                len(ops),
                attempt,
                LOAD_RETRIES,
                delay,
                exc_info=True,
            )
            await asyncio.sleep(delay)


async def load_rag_rows(
    *,
    database: Any,
    documents_ops: list[list[UpdateOne]],
    batch_ops: int = LOAD_BATCH_OPS,
) -> int:
    """Flush the RAG rows of a run in batches of WHOLE documents.

    ``documents_ops`` holds one :func:`build_rag_row_ops` list per document.
    Each batch is one ``bulk_write(ordered=False)`` retried on a dropped
    connection (:func:`_write_batch`); the batches run in sequence so a failure
    leaves every earlier batch written and every later document pending.

    Returns the number of ops issued (== rows written, since every op is an
    upsert on a distinct deterministic ``_id``). No ops skips the round-trip
    entirely — ``bulk_write([])`` raises.
    """

    batches = batch_document_ops(documents_ops, batch_ops)
    collection = database[MEMORY_COLLECTION]
    written = 0
    for index, ops in enumerate(batches, start=1):
        await _write_batch(collection, ops)
        written += len(ops)
        logger.info(
            "load_rag_rows: batch %d/%d written (%d rows so far)",
            index,
            len(batches),
            written,
        )
    return written
