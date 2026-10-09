"""Indexing over the ``memory`` collection: embedding backfill + search indexes.

Post-ingestion steps that prepare the collection for querying, in BOTH memory
modes (ADR-006 decision 4):

1. Backfill embeddings for the rows that are SUPPOSED to carry one and don't —
   **Child chunk**s always; LLM-extractable entity nodes in ``graphrag``.
   Parents and documents are never selected: they are deliberately vector-less,
   so embedding them would pull them into ``$vectorSearch`` results and break
   parent-document retrieval.
2. Ensure BOTH mongot search indexes exist and match their declared
   definitions: ``vector_index`` (``$vectorSearch``; ``numDimensions``
   reconciled against the live embedding model on every call) and
   ``text_search_index``, the **Text search index** (``$search``,
   ``lucene.english``; definition drift → drop + recreate, ADR-015 §5–§7) —
   then drop the classic ``$text`` index ``text_index`` it replaced, if a
   database still carries one.

Plus one operator-triggered inverse of (1): :func:`reset_embeddings`, the
**Embedding reset** (ADR-009 §7), which empties exactly the vectors the backfill
refills so a model or **Embedding role** change can be re-embedded — the two
share their eligibility clause (:func:`_embeddable_row_clause`) so they cannot
disagree about which rows are supposed to carry a vector.

The vector-search index declares ``merged_into`` as a filter path so
``$vectorSearch`` queries can exclude tombstoned nodes natively. Existing
callers (e.g. ``tree.memory.graph.dedup.dedupe_entity``) still do a
post-``$vectorSearch`` ``$match`` for backward compatibility with seeded
fixtures; a future PR can promote that to a vector-index filter clause
now that the path is indexed.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from pymongo import AsyncMongoClient, UpdateOne
from pymongo.errors import OperationFailure

from tree.entities.memory import (
    MEMORY_COLLECTION,
    STORED_VECTOR_QUERY,
    to_stored_vector,
)
from tree.entities.ontology import LLM_EXTRACTABLE_NODE_TYPES
from tree.config.app_config import MemoryMode, app_config
from tree.memory.embedding_text import embed_texts, entity_embedding_text
from tree.memory.rag.embedding import child_embedding_text
from tree.models.base import BaseEmbeddingModel

logger = logging.getLogger(__name__)


# Index names (shared with query module).
VECTOR_INDEX_NAME = "vector_index"
TEXT_SEARCH_INDEX_NAME = "text_search_index"

# The classic ``$text`` index ``text_search_index`` replaced (ADR-015 §1).
# Beanie no longer declares it and never drops, so ``ensure_indexes`` retires
# it once per database.
_LEGACY_TEXT_INDEX_NAME = "text_index"
# MongoDB's ``IndexNotFound`` error code: a drop of an index that is not there.
_INDEX_NOT_FOUND = 27

# How long ``_wait_for_search_index_ready`` waits for a freshly created mongot
# index (either of the two) to answer queries, and how long it sleeps between
# two catalogue reads. Constants, not YAML knobs: a measured need would promote
# them (ADR-008 grooming). 300 s is the fail-OPEN cap — past it the index is
# not "broken", just slow, and retrieval already reports the index's leg as
# unavailable (``text_only`` / ``vector_only``) until it is queryable.
_SEARCH_INDEX_READY_TIMEOUT_S = 300
_SEARCH_INDEX_POLL_S = 5

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
    ``embedding`` is missing / ``None`` **and** it is either

    * a **Child chunk** (``type="chunk"``, ``subtype="child"``), embedded on its
      **Contextual header** text rebuilt from its OWN denormalised
      ``properties.title`` / ``properties.heading_path`` — no join back to the
      document, so the text is byte-identical to the one the worker's
      ``embed_children`` task produced; or
    * in ``graphrag`` only, an LLM-extractable entity node (``person``,
      ``organization``, ...), embedded on its generic node-text — EXCEPT
      ``preference`` and ``fact``, which embed their ``properties.statement`` / ``properties.object``, the
      same text the inline writer uses, so supersession keeps comparing
      statement to statement after an **Embedding reset**. The choice is
      :func:`tree.memory.embedding_text.entity_embedding_text`'s, not this
      function's.

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


def _embeddable_row_clause(mode: MemoryMode) -> list[dict[str, Any]]:
    """The ``$or`` naming the rows that are SUPPOSED to carry a vector in ``mode``.

    **Child chunk**s always; LLM-extractable entity nodes in ``graphrag`` only,
    so in ``rag`` the backfill and the **Embedding reset** can only ever touch
    children (ADR-006 §1 — a stray ``person:self`` row is never embedded).

    Shared verbatim by :func:`_backfill_filter` and :func:`_reset_filter`, so a
    row the **Embedding reset** empties is BY CONSTRUCTION a row the backfill
    refills — the reset can never strip a vector nothing rebuilds.
    """

    clause: list[dict[str, Any]] = [{"type": "chunk", "subtype": "child"}]
    if mode == "graphrag":
        clause.append(
            {"type": {"$in": sorted(t.value for t in LLM_EXTRACTABLE_NODE_TYPES)}}
        )
    return clause


def _backfill_filter(user_id: PydanticObjectId) -> dict[str, Any]:
    """The ONE query that decides what the backfill embeds (see ``embed_nodes``)."""

    return {
        "user_id": user_id,
        "kind": "node",
        # Absent OR null: a row without a vector never stores an empty one
        # (task 175 — vectors are float32 ``binData``).
        "embedding": None,
        "$or": _embeddable_row_clause(app_config.memory.mode),
    }


def node_embedding_text(node: dict[str, Any]) -> str:
    """The text ONE fetched row is embedded on.

    Child chunks embed their **Contextual header** (title + heading path +
    content); every other eligible row goes through
    :func:`tree.memory.embedding_text.entity_embedding_text` — generic node-text
    except for ``preference`` / ``fact``, which embed their
    ``properties.statement`` / ``properties.object``. Both delegations exist so
    the backfill cannot drift from the inline writers (``embed_children`` for
    chunks, ``add_entity`` for entities) into a second vector for the same row.
    That parity is what makes an **Embedding reset** safe: the backfill rebuilds
    the exact text the reset threw away.
    """

    properties = node.get("properties") or {}
    if node.get("type") == "chunk" and node.get("subtype") == "child":
        return child_embedding_text(
            title=properties.get("title"),
            heading_path=properties.get("heading_path") or [],
            content=properties.get("content") or "",
        )
    return entity_embedding_text(node)


async def _embed_batch(
    collection: Any,
    docs: list[dict[str, Any]],
    embedding_model: BaseEmbeddingModel,
) -> int:
    """Embed fetched rows and write the vectors back.

    Embedding is delegated to :func:`tree.memory.embedding_text.embed_texts`,
    which packs the texts into synchronous requests bounded by
    ``models.embedding_batch``. The returned vectors are
    positionally aligned with ``docs`` (across multiple requests), so the zip
    below is safe.

    Embeds as ``document`` (ADR-009 §5): the backfill refills exactly the
    vectors the inline path persists, so it must use the same **Embedding
    role** — otherwise re-embedded rows land in a different corner of the
    space than inline-written rows, at the same dimension.
    """

    vectors = await embed_texts(
        [node_embedding_text(doc) for doc in docs],
        embedding_model,
        input_type="document",
    )

    # Skip inputs the batcher could not embed (empty placeholder from a Voyage
    # content rejection) — leave the node unembedded so a later run retries it.
    ops = [
        UpdateOne(
            {"_id": doc["_id"]}, {"$set": {"embedding": to_stored_vector(vector)}}
        )
        for doc, vector in zip(docs, vectors)
        if vector
    ]
    if ops:
        await collection.bulk_write(ops)

    return len(ops)


# ---------------------------------------------------------------------------
# 2. Embedding reset
# ---------------------------------------------------------------------------


def _reset_filter(user_id: PydanticObjectId) -> dict[str, Any]:
    """The mirror image of :func:`_backfill_filter`: rows that HAVE a vector.

    Same tenant, same ``kind``, same eligibility ``$or`` — only the
    ``embedding`` clause is inverted: a stored float32 ``binData`` vector. An
    already-reset row has no ``embedding`` field, which is what makes a second
    reset match nothing.
    """

    return {
        "user_id": user_id,
        "kind": "node",
        "embedding": STORED_VECTOR_QUERY,
        "$or": _embeddable_row_clause(app_config.memory.mode),
    }


async def reset_embeddings(
    client: AsyncMongoClient,
    database: str,
    user_id: PydanticObjectId,
    *,
    dry_run: bool = False,
) -> int:
    """**Embedding reset** for ONE user: empty every persisted vector it can rebuild.

    The migration for any change that makes stored vectors incomparable with new
    ones — a different embedding model (voyage-3.5 → voyage-4) or a different
    **Embedding role** rule — because rows carry NO model stamp (ADR-009 §7).
    Measured on live voyage-4, a legacy role-less vector scores cos=0.983 against
    the same text embedded as ``query`` but only 0.774 against it as
    ``document``: the old vectors are not merely older, they sit in a different
    corner of the space. Emptying them is load-bearing, not cosmetic.

    Two writes, both keyed on ids captured BEFORE either runs (the first write
    makes the rows stop matching :func:`_reset_filter`, so a filter-keyed second
    write would silently match zero rows):

    1. every matched row → ``embedding`` unset + a fresh ``updated_at``;
    2. the **Child chunk** subset → also ``cluster_id: None``, ``viz: None``.

    (2) exists because ``load_embedding_map`` reads a child as CURRENT when
    ``viz.run_id`` equals the latest run id, and re-embedding never touches
    ``viz`` — without it the **Embedding map** would keep drawing old-space
    coordinates in silence. Cleared, the existing surfaces warn instead ("N/M
    chunks don’t have a 2D embedding. …"). ``memory_clusters``
    rows are deliberately left alone: the next **Clustering run** replaces them
    wholesale.

    ``dry_run=True`` counts and writes nothing. Idempotent either way — a second
    call matches 0 rows and issues no write at all.

    Returns the number of rows reset (matched, in a dry run).
    """

    collection = client[database][MEMORY_COLLECTION]

    reset_filter = _reset_filter(user_id)
    # Projected id reads rather than ``distinct``, whose result must fit in one
    # 16 MB BSON document (~300k ids) — a cap a large tenant could reach.
    ids = [
        row["_id"] for row in await collection.find(reset_filter, {"_id": 1}).to_list()
    ]
    child_ids = [
        row["_id"]
        for row in await collection.find(
            {**reset_filter, "type": "chunk", "subtype": "child"}, {"_id": 1}
        ).to_list()
    ]

    logger.info(
        "Embedding reset: user=%s database=%s rows=%d (children=%d) dry_run=%s",
        user_id,
        database,
        len(ids),
        len(child_ids),
        dry_run,
    )

    if dry_run or not ids:
        return len(ids)

    await collection.update_many(
        {"_id": {"$in": ids}},
        {"$unset": {"embedding": ""}, "$set": {"updated_at": datetime.now(UTC)}},
    )
    if child_ids:
        await collection.update_many(
            {"_id": {"$in": child_ids}},
            {"$set": {"cluster_id": None, "viz": None}},
        )

    return len(ids)


# ---------------------------------------------------------------------------
# 3. Ensure search indexes
# ---------------------------------------------------------------------------


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
    user_id: PydanticObjectId | None = None,
) -> None:
    """Ensure BOTH mongot search indexes: ``vector_index`` then ``text_search_index``.

    This function owns what Beanie cannot express — the two mongot indexes
    (ADR-012, ADR-015 §5–§7) and the ONE classic-index retirement Beanie cannot
    perform (it only creates): the legacy ``$text`` index ``text_index`` is
    dropped as step 3, AFTER ``text_search_index`` is ensured (ready, or the
    fail-open timeout — the ``$text`` leg is gone either way, so the classic
    index serves nothing). ADR-012's "nothing drops" stands for every other
    index. The classic set is Beanie's: it creates the mode's indexes
    (:func:`tree.entities.memory.memory_indexes`) on every ``init_mongodb``. ``user_id`` is optional and only logged: the pipeline
    passes the tenant that triggered the run, the MCP server boot passes none
    (ADR-014 §1).

    Reconcile rules, per index:

    * ``vector_index`` — ``numDimensions`` comes from
      ``embedding_model.dimensions``, read ONCE. A different live dimension
      logs a WARNING naming both numbers and drops + recreates; a missing
      filter path quietly recreates.
    * ``text_search_index`` (the **Text search index**) — a static definition
      (:func:`_build_text_search_index_definition`). Definition drift
      (``dynamic``, a text path's type / analyzer, a filter path's type)
      logs a WARNING naming the differences and drops + recreates;
      server-echoed defaults are not drift.

    Either index is created when absent, and every create is followed by
    :func:`_wait_for_search_index_ready`. Vector first, text second, the
    legacy drop third.

    Idempotent: every step inspects live state and skips when the desired
    configuration is already in place.
    """

    # The indexes are global to the collection (one index covers every tenant),
    # so ``user_id`` only tells the operator what triggered this reconcile.
    trigger = f"tenant user_id={user_id}" if user_id is not None else "server boot"
    logger.info(
        "Ensuring indexes on %s (triggered by %s; indexes themselves are global "
        "to the collection)",
        MEMORY_COLLECTION,
        trigger,
    )

    db = client[database]
    collection = db[MEMORY_COLLECTION]

    # Snapshot the live model's output dimension once so the reconcile
    # logic and the index definition agree even if the model is swapped
    # under us mid-call.
    target_dimensions = embedding_model.dimensions

    # --- 1. Vector search index (for $vectorSearch) ---
    await _ensure_vector_index(collection, target_dimensions)
    # --- 2. Text search index (for $search) ---
    await _ensure_text_search_index(collection)
    # --- 3. Retire the classic $text index it replaced ---
    await _drop_legacy_text_index(collection)


async def _drop_legacy_text_index(collection: Any) -> None:
    """Drop the classic ``text_index`` when the collection still has it.

    Idempotent: absent → nothing. ``IndexNotFound`` (code 27) on the drop is
    "already gone" too — a concurrent ``ensure_indexes`` (MCP boot vs the
    indexing pipeline) dropped it between our check and our drop. Every OTHER
    drop failure propagates — a silently kept ``$text`` index would cost every
    write an index update for no reader.
    """

    if _LEGACY_TEXT_INDEX_NAME not in await collection.index_information():
        logger.debug("No legacy $text index '%s' to drop", _LEGACY_TEXT_INDEX_NAME)
        return

    try:
        await collection.drop_index(_LEGACY_TEXT_INDEX_NAME)
    except OperationFailure as exc:
        if exc.code != _INDEX_NOT_FOUND:
            raise
        logger.debug(
            "Legacy $text index '%s' already dropped by a concurrent ensure_indexes",
            _LEGACY_TEXT_INDEX_NAME,
        )
        return
    logger.info(
        "Dropped legacy $text index '%s' (replaced by '%s', ADR-015)",
        _LEGACY_TEXT_INDEX_NAME,
        TEXT_SEARCH_INDEX_NAME,
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

    await _create_search_index(
        collection,
        {
            "name": VECTOR_INDEX_NAME,
            "type": "vectorSearch",
            "definition": required_definition,
        },
    )

    await _wait_for_search_index_ready(collection, VECTOR_INDEX_NAME)


# Atlas Search text paths of the **Text search index** — the four fields the
# retired classic ``$text`` index covered, so the lexical leg reads the same
# text. Declared DOTTED here (the form ``$search`` queries
# use); :func:`_build_text_search_index_definition` nests ``properties.*``
# through a ``document`` field, the Atlas static-mapping syntax.
TEXT_INDEX_TEXT_PATHS: tuple[str, ...] = (
    "name",
    "aliases",
    "properties.content",
    "properties.aliases",
)

# ``lucene.english``: stemming on the corpus side (``agent`` matches
# ``agents``) and Lucene's English stop words dropped at index AND query time —
# verified live in task 190's spike (``the`` / ``in`` / ``a`` clauses match
# nothing even on a row that contains them).
_TEXT_INDEX_ANALYZER = "lucene.english"

# Filter paths of the **Text search index** → their Atlas Search field type,
# for the ``equals`` clauses of ``compound.filter`` / ``mustNot``. ``user_id``
# is first (every query is tenant-scoped); ``token`` carries no
# ``normalizer`` because the values are already lower-case literals.
# ``merged_into`` is deliberately ABSENT (ADR-015 §6 — no reader needs it).
TEXT_INDEX_FILTER_PATHS: dict[str, str] = {
    "user_id": "objectId",
    "kind": "token",
    "type": "token",
    "subtype": "token",
}


def _build_text_search_index_definition() -> dict[str, Any]:
    """Atlas Search definition of ``text_search_index``: static mappings only.

    ``dynamic: False`` so no other field is indexed; every text path is a
    ``lucene.english`` string, every filter path its declared type. Dotted
    paths are declared through nested ``document`` fields — a dotted KEY in a
    static mapping would name a field literally called ``properties.content``.
    """

    fields: dict[str, Any] = {}
    for path in TEXT_INDEX_TEXT_PATHS:
        _set_static_mapping(
            fields, path, {"type": "string", "analyzer": _TEXT_INDEX_ANALYZER}
        )
    for path, field_type in TEXT_INDEX_FILTER_PATHS.items():
        _set_static_mapping(fields, path, {"type": field_type})
    return {"mappings": {"dynamic": False, "fields": fields}}


def _set_static_mapping(
    fields: dict[str, Any], dotted_path: str, mapping: dict[str, Any]
) -> None:
    """Declare ``mapping`` at ``dotted_path``, nesting through ``document`` fields."""

    head, _, rest = dotted_path.partition(".")
    if not rest:
        fields[head] = mapping
        return
    parent = fields.setdefault(head, {"type": "document", "fields": {}})
    _set_static_mapping(parent["fields"], rest, mapping)


def _existing_text_index_mappings(existing: dict[str, Any]) -> dict[str, Any]:
    """``mappings`` of a live ``list_search_indexes`` entry (``latestDefinition``
    first, ``definition`` as the fallback, as the vector helpers read it)."""

    return (
        existing.get("latestDefinition", {}).get("mappings")
        or existing.get("definition", {}).get("mappings")
        or {}
    )


def _extract_text_index_field_types(
    existing: dict[str, Any],
) -> dict[str, tuple[str, str | None]]:
    """Flatten a live entry's static mappings to ``dotted path → (type, analyzer)``.

    ``document`` fields are walked into dotted paths and never reported
    themselves. Everything else the server echoes on a field (local mongot
    adds ``indexOptions`` / ``store`` / ``norms`` to strings, and a nested
    ``dynamic: false`` to a ``document`` — task 190's spike) is dropped here,
    so it can never read as drift.
    """

    flat: dict[str, tuple[str, str | None]] = {}

    def _walk(fields: dict[str, Any], prefix: str) -> None:
        for name, mapping in fields.items():
            path = f"{prefix}{name}"
            if mapping.get("type") == "document":
                _walk(mapping.get("fields") or {}, f"{path}.")
            else:
                flat[path] = (mapping.get("type"), mapping.get("analyzer"))

    _walk(_existing_text_index_mappings(existing).get("fields") or {}, "")
    return flat


def _text_search_index_drift(
    existing: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(have, want)`` for every declared setting the live index does not meet.

    A SUBSET comparison on the declared paths — exactly as the vector helper
    compares dimensions + the filter-path set, never the whole dict
    (server-echoed defaults would otherwise drop + recreate on every run):
    ``mappings.dynamic`` must be ``False`` — ABSENT counts as ``False``, Atlas's
    default, so a catalogue that omits the key is not drift; every text path a
    ``string`` with the ``lucene.english`` analyzer; every filter path its
    declared type. Extra live paths are not drift. Both dicts are empty when
    up-to-date.
    """

    have: dict[str, Any] = {}
    want: dict[str, Any] = {}

    dynamic = _existing_text_index_mappings(existing).get("dynamic", False)
    if dynamic is not False:
        have["dynamic"], want["dynamic"] = dynamic, False

    live = _extract_text_index_field_types(existing)
    for path in TEXT_INDEX_TEXT_PATHS:
        declared = ("string", _TEXT_INDEX_ANALYZER)
        if live.get(path) != declared:
            have[path], want[path] = live.get(path), declared
    for path, field_type in TEXT_INDEX_FILTER_PATHS.items():
        live_type = live[path][0] if path in live else None
        if live_type != field_type:
            have[path], want[path] = live_type, field_type

    return have, want


async def _ensure_text_search_index(collection: Any) -> None:
    """Create or reconcile the **Text search index** (``text_search_index``).

    Absent → create + wait ready. Present → compare with
    :func:`_text_search_index_drift`: up-to-date → one INFO and return; drift
    → one WARNING naming the differences, drop, let mongot settle, create,
    wait ready.
    """

    cursor = await collection.list_search_indexes()
    existing_indexes = {idx["name"]: idx async for idx in cursor}

    if TEXT_SEARCH_INDEX_NAME in existing_indexes:
        have, want = _text_search_index_drift(existing_indexes[TEXT_SEARCH_INDEX_NAME])
        if not want:
            logger.info(
                "Search index '%s' already up-to-date (analyzer=%s, text paths=%s, "
                "filter paths=%s)",
                TEXT_SEARCH_INDEX_NAME,
                _TEXT_INDEX_ANALYZER,
                list(TEXT_INDEX_TEXT_PATHS),
                TEXT_INDEX_FILTER_PATHS,
            )
            return

        logger.warning(
            "Search index '%s' definition drift (have=%s, want=%s) — dropping and "
            "recreating",
            TEXT_SEARCH_INDEX_NAME,
            have,
            want,
        )
        await collection.drop_search_index(TEXT_SEARCH_INDEX_NAME)
        # Allow mongot to process the drop before recreating the same name.
        await asyncio.sleep(2)

    await _create_search_index(
        collection,
        {
            "name": TEXT_SEARCH_INDEX_NAME,
            "type": "search",
            "definition": _build_text_search_index_definition(),
        },
    )

    await _wait_for_search_index_ready(collection, TEXT_SEARCH_INDEX_NAME)


async def _create_search_index(collection: Any, model: dict[str, Any]) -> None:
    """``create_search_index`` for EITHER mongot index, with the M0-cap message.

    A driver error that IS the Atlas M0 3-search-index cap (its message names
    ``MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED`` or "maximum number of FTS indexes")
    re-raises as a ``RuntimeError`` naming the index and the stray-index fix,
    the driver error kept through ``from exc`` chaining. Every other error
    (auth, network, a malformed definition) propagates unchanged, so an
    unreachable mongot never reads as a cap problem.
    """

    name = model["name"]
    logger.info("Creating search index '%s' (type=%s)...", name, model["type"])
    try:
        await collection.create_search_index(model=model)
    except Exception as exc:
        message = str(exc)
        if (
            "MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED" not in message
            and "maximum number of fts indexes" not in message.lower()
        ):
            raise
        raise RuntimeError(
            f"Could not create search index '{name}': Atlas M0 allows 3 search "
            f"indexes per cluster (search + vectorSearch together) and Tree "
            f"needs two, '{VECTOR_INDEX_NAME}' and '{TEXT_SEARCH_INDEX_NAME}'. "
            f"A MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED cause means a stray index: "
            f"drop the stray index in Atlas → Search & Vector Search and re-run "
            f"`make memory-run-indexing-pipeline`. Driver error: {exc}"
        ) from exc


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
      index's leg as unavailable).
    * ``None`` — this deployment does not report readiness (local mongot);
      read it as ready. Truthiness alone would read it as ``False`` and turn
      every healthy local run into a 5-minute wait (here) or a permanently
      degraded leg (``rag/search.py``).

    The ONE reader of this catalogue shape: ``_wait_for_search_index_ready``
    below and ``tree.memory.rag.search._search_index_is_queryable`` both decide
    here — for ``vector_index`` and ``text_search_index`` alike — so the two
    cannot drift apart.
    """

    queryable = entry.get("queryable")
    status = entry.get("status")

    if queryable is None and status is None:
        return None
    if queryable is not None:
        return queryable is True
    return status == "READY"


async def _wait_for_search_index_ready(collection: Any, index_name: str) -> None:
    """Poll the catalogue until ``index_name`` can actually serve queries.

    Shared by both mongot indexes (``vector_index``, ``text_search_index``).
    mongot builds search indexes out-of-band, so ``create_search_index``
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
      PREVIOUS definition, e.g. the stale-dimensions state
      :func:`assert_settings_match_live_vector_index` exists to catch.
    * **fail-open** — past the 300 s cap, WARN and return. A timeout is "not
      yet", not "never": retrieval reports the index's leg as unavailable
      until mongot catches up, and the next indexing run finds the index
      up-to-date.
    """

    logger.info(
        "Waiting for search index '%s' to be ready (up to %d s)...",
        index_name,
        _SEARCH_INDEX_READY_TIMEOUT_S,
    )

    last_status: str | None = None
    for _ in range(_SEARCH_INDEX_READY_TIMEOUT_S // _SEARCH_INDEX_POLL_S):
        cursor = await collection.list_search_indexes(index_name)
        entries = await cursor.to_list()

        if entries:
            entry = entries[0]
            last_status = entry.get("status")

            if last_status == "FAILED":
                raise RuntimeError(
                    f"Search index '{index_name}' build failed (status=FAILED)"
                )

            queryable = index_entry_is_queryable(entry)
            if queryable is None:
                logger.info(
                    "Search index '%s' reports neither 'status' nor 'queryable' "
                    "(local mongot); treating it as ready",
                    index_name,
                )
                return
            if queryable:
                logger.info(
                    "Search index '%s' ready (status=%s)", index_name, last_status
                )
                return

        logger.debug(
            "Search index '%s' not queryable yet (status=%s); polling again in %d s",
            index_name,
            last_status,
            _SEARCH_INDEX_POLL_S,
        )
        await asyncio.sleep(_SEARCH_INDEX_POLL_S)

    logger.warning(
        "Search index '%s' not queryable after %d s (last status=%s); its "
        "retrieval leg stays unavailable until it is queryable",
        index_name,
        _SEARCH_INDEX_READY_TIMEOUT_S,
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
            f"value, or set apps/memory/src/tree/config/default.yaml's "
            f"models.search_embedding.dimensions to {live_dimensions}."
        )
