"""
Indexing pipeline for the knowledge graph.

Post-extraction steps that prepare the graph for querying:
1. Compute embeddings for nodes that lack them (backfill, no-op when present).
2. Ensure text and vector search indexes exist; reconcile the vector
   index's ``numDimensions`` against the live embedding model on every
   call.

The vector-search index declares ``merged_into`` as a filter path so
``$vectorSearch`` queries can exclude tombstoned nodes natively. Existing
callers (e.g. ``tree.memory.extraction.dedup.dedupe_entity``) still do a
post-``$vectorSearch`` ``$match`` for backward compatibility with seeded
fixtures; a future PR can promote that to a vector-index filter clause
now that the path is indexed.
"""

import asyncio
import logging
from typing import Any

from pymongo import AsyncMongoClient, UpdateOne

from tree.config.app_config import app_config
from tree.models.base import BaseEmbeddingModel

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"

# Index names (shared with query module).
_TEXT_INDEX_NAME = "text_index"
_VECTOR_INDEX_NAME = "vector_index"
_CANONICAL_NAME_INDEX = "canonical_name_index"


# ---------------------------------------------------------------------------
# 1. Embed nodes
# ---------------------------------------------------------------------------


def _node_to_text(node: dict[str, Any]) -> str:
    """Build an embeddable text representation from a node document."""

    parts = [f"{node.get('type', '')}: {node.get('_id', '')}"]
    props = node.get("properties", {})
    for key, value in props.items():
        if value and key != "content":
            parts.append(f"{key}: {value}")
    # Include content last (may be long).
    if props.get("content"):
        parts.append(str(props["content"]))
    return "\n".join(parts)


async def embed_nodes(
    client: AsyncMongoClient,
    database: str,
    embedding_model: BaseEmbeddingModel,
) -> int:
    """Compute embeddings for all nodes that have an empty embedding vector.

    Backfill semantics: only nodes whose ``embedding`` is missing, ``None``,
    or an empty list are re-embedded. Nodes whose embeddings were already
    written inline by the extraction pipeline (task ④ in
    ``tree.memory.extraction.pipeline``) are skipped — running this
    function repeatedly is a no-op once every node has a vector.

    Returns the number of nodes embedded.
    """

    db = client[database]
    collection = db[_KG_COLLECTION]
    batch_size = app_config.query.embedding_batch_size

    # Fetch only nodes whose embedding is missing/None/empty. Nodes with a
    # non-empty embedding vector are intentionally excluded so this stays
    # a backfill, not a re-embedder.
    docs = await collection.find(
        {"kind": "node", "embedding": {"$in": [[], None]}},
    ).to_list()

    embedded_count = 0

    for i in range(0, len(docs), batch_size):
        batch = docs[i : i + batch_size]
        embedded_count += await _embed_batch(collection, batch, embedding_model)

    logger.info("Embedded %d nodes in %s", embedded_count, _KG_COLLECTION)
    return embedded_count


async def _embed_batch(
    collection: Any,
    batch: list[dict[str, Any]],
    embedding_model: BaseEmbeddingModel,
) -> int:
    """Embed a batch of node documents and write vectors back."""

    texts = [_node_to_text(doc) for doc in batch]
    vectors = await embedding_model.embed(texts)

    ops = [
        UpdateOne({"_id": doc["_id"]}, {"$set": {"embedding": vector}})
        for doc, vector in zip(batch, vectors)
    ]
    if ops:
        await collection.bulk_write(ops)

    return len(ops)


# ---------------------------------------------------------------------------
# 3. Ensure search indexes
# ---------------------------------------------------------------------------


# Fields included in the text index, in the order they should appear in the
# composite definition. ``aliases`` is the top-level array of alternate
# surface forms introduced by the resolution/dedup port (#007);
# ``properties.aliases`` is kept for backward compat with documents that
# still carry the old nested shape.
_TEXT_INDEX_FIELDS: list[tuple[str, str]] = [
    ("name", "text"),
    ("aliases", "text"),
    ("properties.content", "text"),
    ("properties.aliases", "text"),
]

# Filter paths the vector-search index must expose so $vectorSearch
# queries can prune candidates server-side. ``merged_into`` lets dedup
# exclude tombstones without a post-aggregation $match.
_VECTOR_INDEX_FILTER_PATHS: tuple[str, ...] = ("kind", "type", "merged_into")


async def ensure_indexes(
    client: AsyncMongoClient,
    database: str,
    *,
    embedding_model: BaseEmbeddingModel,
) -> None:
    """Create classic and search indexes on the knowledge_graph collection.

    Reads ``embedding_model.dimensions`` ONCE and uses it to drive the
    vector-search index's ``numDimensions``. If a ``vector_index`` already
    exists with a different dimension, logs a WARNING naming both numbers
    and drops + recreates it.

    Idempotent: every step inspects live state and skips when the desired
    configuration is already in place.
    """

    db = client[database]
    collection = db[_KG_COLLECTION]

    # Snapshot the live model's output dimension once so the reconcile
    # logic and the index definition agree even if the model is swapped
    # under us mid-call.
    target_dimensions = embedding_model.dimensions

    # --- Classic indexes ---

    await collection.create_index(
        _TEXT_INDEX_FIELDS,
        name=_TEXT_INDEX_NAME,
    )
    logger.info("Text index '%s' ensured on %s", _TEXT_INDEX_NAME, _KG_COLLECTION)

    # Compound indexes for common query patterns.
    await collection.create_index(
        [("kind", 1), ("source_node_id", 1)],
        name="kind_source_node",
    )
    await collection.create_index(
        [("kind", 1), ("target_node_id", 1)],
        name="kind_target_node",
    )
    await collection.create_index(
        [("kind", 1), ("embedding", 1)],
        name="kind_embedding",
    )
    # Non-unique, sparse index on the top-level ``canonical_name`` field —
    # nodes share canonicals (alias families collapse onto the same
    # canonical) and edges have ``canonical_name=None``, so sparse + non-
    # unique is the right shape for soft-join lookups.
    await collection.create_index(
        [("canonical_name", 1)],
        name=_CANONICAL_NAME_INDEX,
        sparse=True,
        unique=False,
    )
    logger.info("Compound indexes ensured on %s", _KG_COLLECTION)

    # --- Vector search index (for $vectorSearch) ---
    await _ensure_vector_index(collection, target_dimensions)


def _build_vector_index_definition(dimensions: int) -> dict[str, Any]:
    """Atlas Vector Search definition: one vector field + filter paths."""

    fields: list[dict[str, Any]] = [
        {
            "type": "vector",
            "path": "embedding",
            "numDimensions": dimensions,
            "similarity": "cosine",
        }
    ]
    for path in _VECTOR_INDEX_FILTER_PATHS:
        fields.append({"type": "filter", "path": path})
    return {"fields": fields}


def _extract_existing_vector_index_dimensions(
    existing: dict[str, Any],
) -> int | None:
    """Pull ``numDimensions`` from a live ``list_search_indexes`` entry.

    Returns ``None`` when the field is absent or unparseable.
    """

    existing_fields = (
        existing.get("latestDefinition", {}).get("fields")
        or existing.get("definition", {}).get("fields")
        or []
    )
    for field in existing_fields:
        if field.get("type") == "vector" and "numDimensions" in field:
            try:
                return int(field["numDimensions"])
            except TypeError, ValueError:
                return None
    return None


def _extract_existing_vector_index_filter_paths(
    existing: dict[str, Any],
) -> set[str]:
    """Set of declared filter paths in a live ``list_search_indexes`` entry."""

    existing_fields = (
        existing.get("latestDefinition", {}).get("fields")
        or existing.get("definition", {}).get("fields")
        or []
    )
    return {
        field["path"]
        for field in existing_fields
        if field.get("type") == "filter" and field.get("path")
    }


async def _ensure_vector_index(collection: Any, target_dimensions: int) -> None:
    """Create or reconcile the vector-search index.

    The live index is considered up-to-date when (a) its ``numDimensions``
    matches ``target_dimensions`` and (b) every path in
    ``_VECTOR_INDEX_FILTER_PATHS`` is declared as a filter. A dimension
    mismatch triggers a WARNING-logged drop + recreate; missing filter
    paths trigger a quiet recreate.
    """

    required_definition = _build_vector_index_definition(target_dimensions)

    cursor = await collection.list_search_indexes()
    existing_indexes = {idx["name"]: idx async for idx in cursor}

    if _VECTOR_INDEX_NAME in existing_indexes:
        existing = existing_indexes[_VECTOR_INDEX_NAME]
        existing_dimensions = _extract_existing_vector_index_dimensions(existing)
        existing_filter_paths = _extract_existing_vector_index_filter_paths(existing)

        dimension_mismatch = (
            existing_dimensions is not None and existing_dimensions != target_dimensions
        )
        filters_complete = set(_VECTOR_INDEX_FILTER_PATHS).issubset(
            existing_filter_paths
        )

        if not dimension_mismatch and filters_complete:
            logger.info(
                "Vector search index '%s' already up-to-date "
                "(dimensions=%s, filters=%s)",
                _VECTOR_INDEX_NAME,
                existing_dimensions,
                sorted(existing_filter_paths),
            )
            return

        if dimension_mismatch:
            logger.warning(
                "Vector search index '%s' dimension mismatch: "
                "existing=%d, target=%d. Dropping and recreating.",
                _VECTOR_INDEX_NAME,
                existing_dimensions,
                target_dimensions,
            )
        else:
            logger.info(
                "Vector search index '%s' missing filter paths "
                "(have=%s, want=%s) — recreating",
                _VECTOR_INDEX_NAME,
                sorted(existing_filter_paths),
                sorted(_VECTOR_INDEX_FILTER_PATHS),
            )

        await collection.drop_search_index(_VECTOR_INDEX_NAME)
        # Allow mongot to process the drop before recreating.
        await asyncio.sleep(2)

    await collection.create_search_index(
        model={
            "name": _VECTOR_INDEX_NAME,
            "type": "vectorSearch",
            "definition": required_definition,
        }
    )

    # Wait for mongot to sync the index.
    logger.info("Waiting for vector search index to be ready...")
    for _ in range(30):
        cursor = await collection.list_search_indexes(_VECTOR_INDEX_NAME)
        results = await cursor.to_list()
        if results:
            await asyncio.sleep(3)
            logger.info("Vector search index '%s' ready", _VECTOR_INDEX_NAME)
            return
        await asyncio.sleep(2)

    logger.warning(
        "Vector search index '%s' did not appear in time", _VECTOR_INDEX_NAME
    )
