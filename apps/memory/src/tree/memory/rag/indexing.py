"""Indexing over the ``memory`` collection: embedding backfill + search indexes.

Post-ingestion steps that prepare the collection for querying, in BOTH memory
modes (ADR-006 decision 4):

1. Backfill embeddings for the rows that are SUPPOSED to carry one and don't —
   **Child chunk**s and LLM-extractable entity nodes. Parents and documents are
   never selected: they are deliberately vector-less, so embedding them would
   pull them into ``$vectorSearch`` results and break parent-document retrieval.
2. Ensure the text and vector search indexes exist; reconcile the vector index's
   ``numDimensions`` against the live embedding model on every call.

The vector-search index declares ``merged_into`` as a filter path so
``$vectorSearch`` queries can exclude tombstoned nodes natively. Existing
callers (e.g. ``tree.memory.graph.dedup.dedupe_entity``) still do a
post-``$vectorSearch`` ``$match`` for backward compatibility with seeded
fixtures; a future PR can promote that to a vector-index filter clause
now that the path is indexed.
"""

import asyncio
import logging
from typing import Any

from beanie import PydanticObjectId
from pymongo import AsyncMongoClient, UpdateOne

from tree.entities.memory import MEMORY_COLLECTION
from tree.entities.ontology import LLM_EXTRACTABLE_NODE_TYPES
from tree.config.app_config import app_config
from tree.memory.embedding_text import embed_texts, node_to_embedding_text
from tree.memory.rag.embedding import child_embedding_text
from tree.models.base import BaseEmbeddingModel

logger = logging.getLogger(__name__)


# Index names (shared with query module).
_TEXT_INDEX_NAME = "text_index"
VECTOR_INDEX_NAME = "vector_index"
_CANONICAL_NAME_INDEX = "user_canonical_name_index"

# How long ``_ensure_vector_index`` waits for a freshly created index to answer
# queries, and how long it sleeps between two catalogue reads. Constants, not
# YAML knobs: a measured need would promote them (ADR-008 grooming). 300 s is
# the fail-OPEN cap — past it the index is not "broken", just slow, and
# retrieval already reports the degraded leg as ``text_only`` (task #124).
_VECTOR_INDEX_READY_TIMEOUT_S = 300
_VECTOR_INDEX_POLL_S = 5

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
    """Backfill embeddings for ``user_id``'s rows that should carry one and don't.

    Selection rule (ADR-006 decision 4) — a row is embedded when its
    ``embedding`` is missing / ``None`` / ``[]`` **and** it is either

    * a **Child chunk** (``type="chunk"``, ``subtype="child"``), embedded on its
      **Contextual header** text rebuilt from its OWN denormalised
      ``properties.title`` / ``properties.heading_path`` — no join back to the
      document, so the text is byte-identical to the one the worker's
      ``embed_children`` task produced; or
    * an LLM-extractable entity node (``person``, ``organization``, ...),
      embedded on its generic node-text exactly as before.

    ``document`` rows and PARENT chunks are excluded BY QUERY: they carry no
    vector by design (parent-document retrieval searches children only), so a
    backfill that embedded them would make every run write the same rows again
    and pollute ``$vectorSearch`` with duplicates of their children.

    Rows whose embedding was written inline by the worker are skipped — running
    this repeatedly is a no-op once every eligible row has a vector. Cross-tenant
    rows are invisible: a two-tenant database that runs ``embed_nodes`` once per
    user produces two disjoint embedding batches.

    Returns the number of rows embedded.
    """

    db = client[database]
    collection = db[MEMORY_COLLECTION]

    docs = await collection.find(_backfill_filter(user_id)).to_list()

    embedded_count = await _embed_batch(collection, docs, embedding_model)

    # A node fetched but not persisted was skipped as un-embeddable (Voyage 400
    # content rejection -> empty placeholder); surface the gap so operators know
    # a backfill retry is pending rather than reading the count as "all done".
    skipped = len(docs) - embedded_count
    skipped_note = f" ({skipped} skipped, will retry)" if skipped else ""
    logger.info(
        "Embedded %d nodes in %s%s", embedded_count, MEMORY_COLLECTION, skipped_note
    )
    return embedded_count


def _backfill_filter(user_id: PydanticObjectId) -> dict[str, Any]:
    """The ONE query that decides what the backfill embeds (see ``embed_nodes``)."""

    return {
        "user_id": user_id,
        "kind": "node",
        "embedding": {"$in": [[], None]},
        "$or": [
            {"type": "chunk", "subtype": "child"},
            {"type": {"$in": sorted(t.value for t in LLM_EXTRACTABLE_NODE_TYPES)}},
        ],
    }


def node_embedding_text(node: dict[str, Any]) -> str:
    """The text ONE fetched row is embedded on.

    Child chunks embed their **Contextual header** (title + heading path +
    content); every other eligible row embeds the generic node-text. Keeping the
    two in ONE function is what stops the backfill and the inline
    ``embed_children`` task from drifting into two different vectors for the
    same row.
    """

    properties = node.get("properties") or {}
    if node.get("type") == "chunk" and node.get("subtype") == "child":
        return child_embedding_text(
            title=properties.get("title"),
            heading_path=properties.get("heading_path") or [],
            content=properties.get("content") or "",
        )
    return node_to_embedding_text(node)


async def _embed_batch(
    collection: Any,
    docs: list[dict[str, Any]],
    embedding_model: BaseEmbeddingModel,
) -> int:
    """Embed fetched rows and write the vectors back.

    Embedding is delegated to :func:`tree.memory.embedding_text.embed_texts`,
    which packs the texts into as few synchronous Voyage requests as the
    per-request caps allow (1000 inputs / 320K tokens). The returned vectors are
    positionally aligned with ``docs`` (across multiple requests), so the zip
    below is safe.
    """

    vectors = await embed_texts(
        [node_embedding_text(doc) for doc in docs], embedding_model
    )

    # Skip inputs the batcher could not embed (empty placeholder from a Voyage
    # content rejection) — leave the node unembedded so a later run retries it.
    ops = [
        UpdateOne({"_id": doc["_id"]}, {"$set": {"embedding": vector}})
        for doc, vector in zip(docs, vectors)
        if vector
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
# lets dedup exclude tombstones without a post-aggregation $match; ``subtype``
# (ADR-006 decision 2) is what restricts the seed search to CHILD chunks. Adding
# ``subtype`` makes the live index miss a required path once per environment,
# which the quiet drop-and-recreate branch in ``_ensure_vector_index`` heals on
# the next indexing run.
_VECTOR_INDEX_FILTER_PATHS: tuple[str, ...] = (
    "user_id",
    "kind",
    "type",
    "subtype",
    "merged_into",
)


async def ensure_indexes(
    client: AsyncMongoClient,
    database: str,
    *,
    embedding_model: BaseEmbeddingModel,
    user_id: PydanticObjectId,
) -> None:
    """Create classic and search indexes on the memory collection.

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
        MEMORY_COLLECTION,
        user_id,
    )

    db = client[database]
    collection = db[MEMORY_COLLECTION]

    # Snapshot the live model's output dimension once so the reconcile
    # logic and the index definition agree even if the model is swapped
    # under us mid-call.
    target_dimensions = embedding_model.dimensions

    # --- Drop legacy non-tenant-prefixed compound indexes (idempotent) ---
    await _drop_legacy_compound_indexes(collection)

    # --- Classic indexes ---

    # No readiness poll here, unlike the vector index below.
    # create_index is synchronous: the standard $text index is built by mongod
    # itself and the await returns only once it can serve queries. Only the
    # Atlas Search indexes mongot builds out-of-band need polling.
    await collection.create_index(
        _TEXT_INDEX_FIELDS,
        name=_TEXT_INDEX_NAME,
    )
    logger.info("Text index '%s' ensured on %s", _TEXT_INDEX_NAME, MEMORY_COLLECTION)

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
    # :class:`tree.entities.memory.MemoryEntry`; the
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
    logger.info("Compound indexes ensured on %s", MEMORY_COLLECTION)

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
                    MEMORY_COLLECTION,
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

    if VECTOR_INDEX_NAME in existing_indexes:
        existing = existing_indexes[VECTOR_INDEX_NAME]
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
                VECTOR_INDEX_NAME,
                existing_dimensions,
                sorted(existing_filter_paths),
            )
            return

        if dimension_mismatch:
            logger.warning(
                "Vector search index '%s' dimension mismatch: "
                "existing=%d, target=%d. Dropping and recreating.",
                VECTOR_INDEX_NAME,
                existing_dimensions,
                target_dimensions,
            )
        else:
            logger.info(
                "Vector search index '%s' missing filter paths "
                "(have=%s, want=%s) — recreating",
                VECTOR_INDEX_NAME,
                sorted(existing_filter_paths),
                sorted(_VECTOR_INDEX_FILTER_PATHS),
            )

        await collection.drop_search_index(VECTOR_INDEX_NAME)
        # Allow mongot to process the drop before recreating.
        await asyncio.sleep(2)

    await collection.create_search_index(
        model={
            "name": VECTOR_INDEX_NAME,
            "type": "vectorSearch",
            "definition": required_definition,
        }
    )

    await _wait_for_vector_index_ready(collection)


def index_entry_is_queryable(entry: dict[str, Any]) -> bool | None:
    """Can ONE ``$listSearchIndexes`` entry serve queries yet?

    ``$listSearchIndexes`` reports readiness in two fields — ``status``
    (``PENDING | BUILDING | READY | STALE | FAILED | DELETING |
    DOES_NOT_EXIST``) and ``queryable`` (bool, "the index is ready to be
    queried"):
    https://www.mongodb.com/docs/manual/reference/operator/aggregation/listSearchIndexes/

    Atlas sends both. The LOCAL mongot dev container sends NEITHER — verified
    2026-09-12 with ``mongosh`` against ``docker/mongot``, whose entry is
    exactly ``{id, name, type, latestDefinition}``. So "both absent" is its own
    answer rather than "not ready": a literal wait-for-``queryable`` would burn
    the full 300 s cap on every local indexing run.

    Three-valued ON PURPOSE, and ``None`` is FALSY — callers MUST branch on
    ``is None`` BEFORE any truthiness test:

    * ``True`` — queryable now.
    * ``False`` — present but not serving queries yet (keep polling / treat the
      vector leg as unavailable).
    * ``None`` — this deployment does not report readiness (local mongot);
      read it as ready. Truthiness alone would read it as ``False`` and turn
      every healthy local run into a 5-minute wait (here) or a permanent
      ``text_only`` (``rag/search.py``).

    The ONE reader of this catalogue shape: ``_wait_for_vector_index_ready``
    below and ``tree.memory.rag.search._vector_index_is_queryable`` both decide
    here, so the two cannot drift apart.
    """

    queryable = entry.get("queryable")
    status = entry.get("status")

    if queryable is None and status is None:
        return None
    if queryable is not None:
        return queryable is True
    return status == "READY"


async def _wait_for_vector_index_ready(collection: Any) -> None:
    """Poll the catalogue until the new vector index can actually serve queries.

    mongot builds Atlas Search indexes out-of-band, so ``create_search_index``
    returning says nothing about readiness — the previous version of this loop
    waited for the entry to merely EXIST and then logged "ready", which is how
    an indexing run could report success while ``$vectorSearch`` still answered
    nothing (ADR-008: readiness is observed, not guessed).

    Three exits:

    * **ready** — :func:`index_entry_is_queryable` says ``True`` (Atlas) or
      ``None`` (local mongot, which reports no readiness fields at all).
    * **raise** — ``status == "FAILED"``: the build will never finish on its
      own, so the indexing phase fails loudly instead of leaving a silent
      "ready". Checked BEFORE ``queryable`` because the docs allow a FAILED
      index to still report ``queryable: true`` — it is then serving the
      PREVIOUS definition, i.e. exactly the stale-dimensions state
      :func:`assert_settings_match_live_vector_index` exists to catch.
    * **fail-open** — past the 300 s cap, WARN and return. A timeout is "not
      yet", not "never": retrieval reports ``text_only`` until mongot catches
      up, and the next indexing run finds the index up-to-date.
    """

    logger.info(
        "Waiting for vector search index '%s' to be ready (up to %d s)...",
        VECTOR_INDEX_NAME,
        _VECTOR_INDEX_READY_TIMEOUT_S,
    )

    last_status: str | None = None
    for _ in range(_VECTOR_INDEX_READY_TIMEOUT_S // _VECTOR_INDEX_POLL_S):
        cursor = await collection.list_search_indexes(VECTOR_INDEX_NAME)
        entries = await cursor.to_list()

        if entries:
            entry = entries[0]
            last_status = entry.get("status")

            if last_status == "FAILED":
                raise RuntimeError(
                    f"Vector search index '{VECTOR_INDEX_NAME}' build failed "
                    f"(status=FAILED)"
                )

            queryable = index_entry_is_queryable(entry)
            if queryable is None:
                logger.info(
                    "Vector search index '%s' reports neither 'status' nor "
                    "'queryable' (local mongot); treating it as ready",
                    VECTOR_INDEX_NAME,
                )
                return
            if queryable:
                logger.info(
                    "Vector search index '%s' ready (status=%s)",
                    VECTOR_INDEX_NAME,
                    last_status,
                )
                return

        logger.debug(
            "Vector search index '%s' not queryable yet (status=%s); "
            "polling again in %d s",
            VECTOR_INDEX_NAME,
            last_status,
            _VECTOR_INDEX_POLL_S,
        )
        await asyncio.sleep(_VECTOR_INDEX_POLL_S)

    logger.warning(
        "Vector search index '%s' not queryable after %d s (last status=%s); "
        "retrieval runs text_only until it is",
        VECTOR_INDEX_NAME,
        _VECTOR_INDEX_READY_TIMEOUT_S,
        last_status,
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
    ``vector_index`` definition for ``database.memory`` and:

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

    collection = client[database][MEMORY_COLLECTION]
    cursor = await collection.list_search_indexes()
    indexes: list[dict[str, Any]] = [idx async for idx in cursor]

    live: dict[str, Any] | None = next(
        (idx for idx in indexes if idx.get("name") == VECTOR_INDEX_NAME),
        None,
    )
    if live is None:
        raise RuntimeError(
            f"vector_index not found in database '{database}'; expected an "
            f"Atlas Vector Search index named '{VECTOR_INDEX_NAME}' with "
            f"numDimensions={expected_dim}. Run the indexing "
            f"pipeline to bootstrap it."
        )

    live_dimensions = _extract_existing_vector_index_dimensions(live)
    if live_dimensions is None:
        raise RuntimeError(
            f"vector_index '{VECTOR_INDEX_NAME}' in database '{database}' has "
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
