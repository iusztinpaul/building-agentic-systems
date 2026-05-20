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

from beanie import PydanticObjectId
from pymongo import AsyncMongoClient, UpdateOne

from tree.config.app_config import app_config
from tree.memory.embedding_text import embed_node_texts
from tree.models.base import BaseEmbeddingModel

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"

# Index names (shared with query module).
_TEXT_INDEX_NAME = "text_index"
_VECTOR_INDEX_NAME = "vector_index"
_CANONICAL_NAME_INDEX = "user_canonical_name_index"

# Legacy compound index names that the ``user_*``-prefixed versions
# replace. The reconcile loop in :func:`ensure_indexes` drops these on
# first run so callers don't carry two parallel sets of indexes.
_LEGACY_COMPOUND_INDEX_NAMES: tuple[str, ...] = (
    "kind_source_node",
    "kind_target_node",
    "kind_embedding",
    "canonical_name_index",
)


# ---------------------------------------------------------------------------
# 1. Embed nodes
# ---------------------------------------------------------------------------


async def embed_nodes(
    client: AsyncMongoClient,
    database: str,
    embedding_model: BaseEmbeddingModel,
    user_id: PydanticObjectId,
) -> int:
    """Compute embeddings for nodes belonging to ``user_id`` that lack one.

    Backfill semantics: only nodes whose ``embedding`` is missing, ``None``,
    or an empty list are re-embedded. Nodes whose embeddings were already
    written inline by the extraction pipeline (task ④ in
    ``tree.memory.extraction.pipeline``) are skipped — running this
    function repeatedly is a no-op once every node has a vector.

    Cross-tenant rows are invisible to this function. A two-tenant database
    that runs ``embed_nodes`` once per user produces two disjoint
    embedding batches.

    Returns the number of nodes embedded.
    """

    db = client[database]
    collection = db[_KG_COLLECTION]

    # Fetch only nodes whose embedding is missing/None/empty AND that
    # belong to ``user_id``. Nodes with a non-empty embedding vector are
    # intentionally excluded so this stays a backfill, not a re-embedder.
    docs = await collection.find(
        {
            "user_id": user_id,
            "kind": "node",
            "embedding": {"$in": [[], None]},
        },
    ).to_list()

    embedded_count = await _embed_batch(collection, docs, embedding_model)

    logger.info("Embedded %d nodes in %s", embedded_count, _KG_COLLECTION)
    return embedded_count


async def _embed_batch(
    collection: Any,
    docs: list[dict[str, Any]],
    embedding_model: BaseEmbeddingModel,
) -> int:
    """Embed node documents and write vectors back.

    Embedding is delegated to
    :func:`tree.memory.embedding_text.embed_node_texts`, which packs the
    node-texts into as few synchronous Voyage requests as the per-request
    caps allow (1000 inputs / 320K tokens). The returned vectors are
    positionally aligned with ``docs`` (across multiple requests), so the
    zip below is safe.
    """

    vectors = await embed_node_texts(docs, embedding_model)

    ops = [
        UpdateOne({"_id": doc["_id"]}, {"$set": {"embedding": vector}})
        for doc, vector in zip(docs, vectors)
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
# queries can prune candidates server-side. ``user_id`` is first — every
# tenant-scoped $vectorSearch carries a ``user_id`` filter; ``merged_into``
# lets dedup exclude tombstones without a post-aggregation $match.
_VECTOR_INDEX_FILTER_PATHS: tuple[str, ...] = (
    "user_id",
    "kind",
    "type",
    "merged_into",
)


async def ensure_indexes(
    client: AsyncMongoClient,
    database: str,
    *,
    embedding_model: BaseEmbeddingModel,
    user_id: PydanticObjectId,
) -> None:
    """Create classic and search indexes on the knowledge_graph collection.

    ``user_id`` is the **leading key** of every compound index — that
    pattern matches every tenant-scoped read in this codebase, so a
    ``find({"user_id": X, ...})`` lookup hits the index prefix without a
    full collection scan. The actual indexes themselves are global to the
    collection; ``user_id`` is the first key, not a separate index per
    tenant. ``user_id`` is passed (rather than read from settings) so the
    parameter shape mirrors the other pipeline entry points and tests can
    drive index creation deterministically.

    Reads ``embedding_model.dimensions`` ONCE and uses it to drive the
    vector-search index's ``numDimensions``. If a ``vector_index`` already
    exists with a different dimension, logs a WARNING naming both numbers
    and drops + recreates it.

    Idempotent: every step inspects live state and skips when the desired
    configuration is already in place. Legacy compound indexes
    (``kind_source_node`` etc.) are dropped on first run.
    """

    # ``user_id`` is bound by the caller; ``ensure_indexes`` is parameterised
    # on it so the signature mirrors the rest of the pipeline. The actual
    # compound indexes are global to the collection (one index covers every
    # tenant) — the parameter exists for shape consistency and to surface a
    # ``TypeError`` when a caller forgets it. We log it here so the operator
    # can correlate an index-reconcile run with the tenant that triggered it.
    logger.info(
        "Ensuring indexes on %s (triggered by tenant user_id=%s; indexes "
        "themselves are global to the collection)",
        _KG_COLLECTION,
        user_id,
    )

    db = client[database]
    collection = db[_KG_COLLECTION]

    # Snapshot the live model's output dimension once so the reconcile
    # logic and the index definition agree even if the model is swapped
    # under us mid-call.
    target_dimensions = embedding_model.dimensions

    # --- Drop legacy non-tenant-prefixed compound indexes (idempotent) ---
    await _drop_legacy_compound_indexes(collection)

    # --- Classic indexes ---

    await collection.create_index(
        _TEXT_INDEX_FIELDS,
        name=_TEXT_INDEX_NAME,
    )
    logger.info("Text index '%s' ensured on %s", _TEXT_INDEX_NAME, _KG_COLLECTION)

    # Compound indexes for common query patterns. Every key starts with
    # ``user_id`` so tenant-scoped reads hit the index prefix.
    await collection.create_index(
        [("user_id", 1), ("kind", 1), ("source_node_id", 1)],
        name="user_kind_source_node",
    )
    await collection.create_index(
        [("user_id", 1), ("kind", 1), ("target_node_id", 1)],
        name="user_kind_target_node",
    )
    await collection.create_index(
        [("user_id", 1), ("kind", 1), ("embedding", 1)],
        name="user_kind_embedding",
    )
    # Non-unique, sparse index on (user_id, canonical_name) — nodes share
    # canonicals (alias families collapse onto the same canonical) and
    # edges have ``canonical_name=None``, so sparse + non-unique is the
    # right shape for soft-join lookups.
    await collection.create_index(
        [("user_id", 1), ("canonical_name", 1)],
        name=_CANONICAL_NAME_INDEX,
        sparse=True,
        unique=False,
    )
    # #029: partial index for the ``related_to`` umbrella edge.
    # Filter ``semantic_type`` non-null so only ``related_to`` rows
    # carry the index cost. Idempotent on re-create. Also declared on
    # :class:`tree.entities.knowledge_graph.KnowledgeGraphEntry`; the
    # dynamic create here keeps the indexing-pipeline run-path
    # authoritative (it's the surface CI/integration tests assert on).
    # ``$ne: null`` is not a valid partial-filter expression in
    # MongoDB; ``$type: "string"`` is the supported equivalent (every
    # ``semantic_type`` value is a string by validator contract).
    await collection.create_index(
        [("user_id", 1), ("type", 1), ("semantic_type", 1)],
        name="user_type_semantic_type",
        partialFilterExpression={"semantic_type": {"$type": "string"}},
    )
    logger.info("Compound indexes ensured on %s", _KG_COLLECTION)

    # --- Vector search index (for $vectorSearch) ---
    await _ensure_vector_index(collection, target_dimensions)


async def _drop_legacy_compound_indexes(collection: Any) -> None:
    """Drop any pre-#019 compound indexes that lacked the ``user_id`` prefix.

    Safe to call repeatedly: each drop is wrapped so a missing index is a
    no-op, and the function only targets the known legacy names.
    """

    try:
        existing = await collection.index_information()
    except Exception:  # noqa: BLE001 — never block startup on this
        logger.debug("Could not list classic indexes; skipping legacy drop")
        return

    for name in _LEGACY_COMPOUND_INDEX_NAMES:
        if name in existing:
            try:
                await collection.drop_index(name)
                logger.info(
                    "Dropped legacy compound index '%s' on %s",
                    name,
                    _KG_COLLECTION,
                )
            except Exception:  # noqa: BLE001 — drop failures are non-fatal
                logger.warning(
                    "Failed to drop legacy compound index '%s' (will retry next run)",
                    name,
                    exc_info=True,
                )


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


# ---------------------------------------------------------------------------
# 4. Startup-time settings vs. live vector index check
# ---------------------------------------------------------------------------


async def assert_settings_match_live_vector_index(
    client: AsyncMongoClient,
    database: str,
) -> None:
    """Hard-error gate between ``app_config.models.search_embedding.dimensions``
    and the live mongot index.

    The YAML is the authoritative source for the embedding dimension. This
    gate is pinned to the **search** embedding because that is the model
    whose output is persisted to the node ``embedding`` field (the
    resolution embedding is transient and never written, so its dimension
    is not index-coupled). The Atlas Vector Search index under
    ``docker/mongot/`` must reflect
    ``app_config.models.search_embedding.dimensions`` — a mismatch silently
    corrupts every ``$vectorSearch`` write. This helper inspects the live
    ``vector_index`` definition for ``database.knowledge_graph`` and:

    * Returns ``None`` if ``numDimensions`` on the live index equals
      ``app_config.models.search_embedding.dimensions``.
    * Raises :class:`RuntimeError` (with both numbers in the message) on
      mismatch. The literal substring ``Embedding dimension mismatch``
      is preserved as a grep anchor for the rebuild runbook.
    * Raises :class:`RuntimeError` (``"vector_index not found"``) when no
      index named ``vector_index`` is present — caller decides whether to
      bootstrap one via :func:`ensure_indexes` or fail.

    Intended call site: indexing-pipeline boot, before any embedding write.
    """

    expected_dim = app_config.models.search_embedding.dimensions

    collection = client[database][_KG_COLLECTION]
    cursor = await collection.list_search_indexes()
    indexes: list[dict[str, Any]] = [idx async for idx in cursor]

    live: dict[str, Any] | None = next(
        (idx for idx in indexes if idx.get("name") == _VECTOR_INDEX_NAME),
        None,
    )
    if live is None:
        raise RuntimeError(
            f"vector_index not found in database '{database}'; expected an "
            f"Atlas Vector Search index named '{_VECTOR_INDEX_NAME}' with "
            f"numDimensions={expected_dim}. Run the indexing "
            f"pipeline to bootstrap it."
        )

    live_dimensions = _extract_existing_vector_index_dimensions(live)
    if live_dimensions is None:
        raise RuntimeError(
            f"vector_index '{_VECTOR_INDEX_NAME}' in database '{database}' has "
            f"no parseable numDimensions; expected "
            f"app_config.models.search_embedding.dimensions={expected_dim}."
        )

    if live_dimensions != expected_dim:
        raise RuntimeError(
            f"Embedding dimension mismatch: "
            f"app_config.models.search_embedding.dimensions={expected_dim} but "
            f"live vector_index numDimensions={live_dimensions}. Rebuild the "
            f"mongot index (drop + ensure_indexes) so it matches the YAML "
            f"value, or set apps/memory/configs/default.yaml's "
            f"models.search_embedding.dimensions to {live_dimensions}."
        )
