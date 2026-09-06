"""The ONE memory ingestion module: Prefect tasks + the three memory flows.

ADR-006 decision 8. Every memory flow lives here so the rag/graph split is ONE
``if`` in one file instead of a package boundary the reader has to chase:

* ``memory_extract_etl_worker`` (deployment ``memory-extract-etl-worker``) — the
  ingestion body for BOTH memory modes. NO ``num_shards``, NO coordinator
  branch, NO ``run_deployment``, NO indexing trigger. Returns a
  :class:`WriteSummary`.
* ``memory_extract_etl_coordinator`` (an inline subflow of ``offline-pipeline``,
  not a deployment) — resolve pending docs -> partition into
  ``min(num_shards, N)`` balanced shards -> dispatch ONE
  ``memory-extract-etl-worker`` run per shard under
  ``asyncio.gather(return_exceptions=True)``. It does NOT index: since ADR-007
  Decision 5 indexing is its own **Offline phase**. Dispatches to the WORKER (no
  recursion). Returns a :class:`FanOutStats`.
* ``memory_indexing`` (flow ``memory-indexing-etl``) — the embedding backfill +
  search-index reconcile, run as phase 3 of ``offline-pipeline`` (once per
  target user) and inline by ``online-pipeline`` for a single document. Returns
  the number of rows it embedded.

Worker task topology. The RAG stages run in BOTH modes; the graph stages run
only when ``memory.mode == "graphrag"`` (read ONCE at flow entry):

* (rag) ① ``clean_and_chunk_task`` — per-doc, ``INPUTS`` cache (the
  ``ChunkingConfig`` is a task PARAMETER, so a strategy change is a cache
  MISS), 1 retry.
* (rag) ② ``embed_children_task`` — one batched embed of the run's unique
  **Contextual header** texts, ``INPUTS`` cache, 2 retries.
* (rag) ③ ``load_rag_rows_task`` — document + parent + child rows in ONE
  ``bulk_write``, ``NO_CACHE``, 3 retries. **``rag`` mode returns here.**
* (graph) ④ ``llm_extract_entities_task`` — one LLM call per PARENT chunk,
  ``INPUTS`` cache, 2 retries.
* (graph) ④.5 ``validate_raws_task`` — envelope + field validation.
* (graph) ⑤ ``resolve_entities_task`` — batched, ``NO_CACHE``, 1 retry.
* (graph) ⑥ ``embed_entities_task`` — batched, ``INPUTS`` cache.
* (graph) ⑦ ``dedupe_entities_task`` — batched, ``NO_CACHE``, 1 retry.
* (graph) ⑧ ``apply_writes_task`` — entity rows, ``mentions`` and the structural
  edges over the chunk hierarchy, batched, ``NO_CACHE``, 3 retries.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from beanie import PydanticObjectId
from prefect import flow, get_run_logger, task
from prefect.cache_policies import INPUTS, NO_CACHE
from prefect.deployments import run_deployment
from pymongo import UpdateOne

from tree.config.app_config import ChunkingConfig, load_app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.documents import Document
from tree.entities.extraction_audit import (
    ExtractionDroppedField,
    ExtractionRejection,
    truncate_raw_row,
    truncate_raw_value,
)
from tree.entities.memory import (
    EdgeType,
    ExtractorInfo,
    MEMORY_COLLECTION,
    NodeType,
    build_edge_id,
    build_node_id,
)
from tree.entities.ontology import LLM_EXTRACTABLE_NODE_TYPES
from tree.entities.users import User
from tree.memory.embedding_text import embed_in_batches, node_to_embedding_text
from tree.memory.graph.add_entity import add_entity
from tree.memory.graph.extraction import build_structural_entries, extract_entities
from tree.memory.graph.dedup import (
    DeduplicationConfig,
    DeduplicationResult,
    MergeStrategy,
    dedupe_entity,
)
from tree.memory.graph.first_person_resolver import redirect_first_person
from tree.memory.graph.preference_supersession import (
    canonicalize_preference_names,
    resolve_supersessions,
    write_self_has_preference_edges,
)
from tree.memory.graph.sharding import (
    FanOutStats,
    _fan_out_extraction,
    _partition_into_shards,
    _resolve_num_shards,
    _resolve_pending_document_ids,
)
from tree.memory.graph.validation import (
    get_edge_property_schema,
    get_node_property_schemas,
    validate_envelope,
    validate_properties,
)
from tree.memory.rag.chunking import split_document
from tree.memory.rag.cleaning import clean_text
from tree.memory.rag.embedding import child_embedding_text
from tree.memory.rag.indexing import (
    assert_settings_match_live_vector_index,
    embed_nodes,
    ensure_indexes,
)
from tree.memory.rag.types import ParentChunk
from tree.memory.rag.load import (
    build_rag_row_ops,
    child_chunk_name,
    child_row_id,
    document_row_id,
    load_rag_rows,
    parent_chunk_name,
    parent_row_id,
)
from tree.memory.graph.resolution.composite import CompositeResolver
from tree.memory.graph.resolution.types import ResolvedEntity, _normalize
from tree.memory.types import (
    ChunkedDocument,
    DedupDecision,
    DedupMap,
    EmbeddingMap,
    ExtractedEdge,
    ExtractedNode,
    ExtractionResult,
    RawExtraction,
    ResolutionOutput,
    WriteSummary,
    make_entity_key,
    make_type_name_key,
)
from tree.models.base import BaseEmbeddingModel, BaseLLM
from tree.models.get_model import (
    get_embedding_model,
    get_llm,
    get_resolution_embedding_model,
    get_search_embedding_model,
)
from tree.config.constants import TAGS_EXTRACTION, TAGS_INDEXING
from tree.observability import (
    configure_opik,
    flush_opik,
    get_distributed_trace_headers,
    pipeline_metadata,
    span,
    tracked_span,
)

logger = logging.getLogger(__name__)


# Tag every extraction task span as ingestion telemetry. Tasks open their span
# explicitly via :func:`tree.observability.span`, attaching to the flow's trace
# through the ``opik_trace_headers`` parameter (a distributed-trace header dict
# the flow grabs and passes down). This is robust to Prefect running each task in
# its own thread/subprocess — contextvars do NOT propagate there, so a bare
# ``@track`` would mint a fresh root trace per task (~650 fragments for one run).
# The ``INPUTS`` cache policies EXCLUDE ``opik_trace_headers`` so a new run's
# headers never bust the cache (see each ``task(...)`` ``cache_policy=``).
#
# Pipeline-identity tags (see ``tree.observability``): the memory-extraction
# pipeline's tags, shared 1:1 with its Prefect deployment / flow-run tags. The
# pipeline name also rides as span metadata (``pipeline="extraction"``).
_EXTRACTION_TAGS = TAGS_EXTRACTION
_EXTRACTION_METADATA = pipeline_metadata("extraction")

# Cache policy for the cached tasks: INPUTS minus the per-run trace-header param,
# so changing trace headers between runs is NOT a cache miss (Prefect 3).
_INPUTS_NO_HEADERS = INPUTS - "opik_trace_headers"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_run_logger() -> logging.Logger:
    """Return the Prefect run logger when inside a task/flow, else the module logger.

    Tests that call task ``.fn(...)`` outside a flow run hit the
    ``MissingContextError`` branch — those still emit through the regular
    logger so unit tests can capture log lines via ``caplog``.
    """

    try:
        return get_run_logger()
    except Exception:  # noqa: BLE001 — Prefect raises a typed error
        return logger


def _unwrap_validation_error(exc: BaseException) -> BaseException:
    """Project a Pydantic ``ValidationError`` to its inner ``ValueError``.

    The cross-key validator raises ``ValueError`` but Pydantic wraps it as a
    ``ValidationError``. Callers (and tests) want to match on
    ``ValueError`` directly.
    """

    from pydantic import ValidationError

    if isinstance(exc, ValueError) and not isinstance(exc, ValidationError):
        return exc
    if isinstance(exc, ValidationError):
        for err in exc.errors():
            ctx_err = err.get("ctx", {}).get("error")
            if isinstance(ctx_err, ValueError):
                return ctx_err
            if isinstance(ctx_err, str):
                return ValueError(ctx_err)
        # No structured underlying error — re-raise as ValueError with message.
        return ValueError(str(exc))
    return exc


def _live_app_config() -> Any:
    """Return a freshly-loaded :class:`AppConfig`.

    The module-level ``app_config`` is set at import time. Tests (and operators
    flipping an env var) want subsequent flow entries to see the new state, so
    every helper that reads ``extraction.*`` re-loads the config here.
    """

    return load_app_config()


def _build_dedup_config() -> DeduplicationConfig:
    """Translate the YAML :class:`DedupConfig` to the runtime
    :class:`DeduplicationConfig` (re-validates ranges via ``__post_init__``).

    The YAML config (``app_config.extraction.dedup``) is the sole
    authoritative source for the runtime thresholds. Operators who
    need a one-off override use the ``TREE_EXTRACTION__DEDUP__<KEY>``
    env-var escape hatch (see :func:`_apply_env_overrides` in
    ``app_config``); the previous ``DEDUP_*``-prefixed BaseSettings
    surface was decommissioned in #034. The cross-validator on
    :class:`ExtractionConfig` that pins resolution / dedup
    type-strictness keys off the YAML config.
    """

    cfg = _live_app_config().extraction.dedup
    return DeduplicationConfig(
        enabled=cfg.enabled,
        auto_merge_threshold=cfg.auto_merge_threshold,
        flag_threshold=cfg.flag_threshold,
        use_fuzzy_matching=cfg.use_fuzzy_matching,
        fuzzy_threshold=cfg.fuzzy_threshold,
        max_candidates=cfg.max_candidates,
        match_same_type_only=cfg.match_same_type_only,
        merge_strategy=MergeStrategy(cfg.merge_strategy),
    )


def _entity_embeddable_text(
    *, entity_type: NodeType, name: str, canonical_name: str, properties: dict[str, Any]
) -> str:
    """Embeddable text for one extracted entity.

    Mirrors :func:`tree.memory.graph.add_entity._embeddable_text` so
    the vector task ④ pre-computes (that ⑤ deduplicates against and ⑥
    persists) is byte-for-byte the text ``add_entity`` would build for the
    same node — GENERIC types embed their node-text, PREFERENCE / FACT embed
    ``properties.statement`` / ``properties.object``. Keeping the two
    builders in lock-step is what lets ``_CachedSingleEmbedding`` reuse the
    vector and makes the indexing backfill a no-op for dedup-created nodes.
    """

    if entity_type == NodeType.PREFERENCE:
        statement = (properties or {}).get("statement")
        if isinstance(statement, str) and statement.strip():
            return statement.strip()
    elif entity_type == NodeType.FACT:
        obj = (properties or {}).get("object") or (properties or {}).get("object_")
        if isinstance(obj, str) and obj.strip():
            return obj.strip()

    # ``aliases`` / ``confidence`` are top-level columns on the persisted
    # row, never under ``properties`` — strip them so this text matches
    # both ``add_entity._embeddable_text`` and the indexing backfill text.
    node = {
        "type": entity_type.value,
        "name": name,
        "canonical_name": canonical_name,
        "properties": {
            k: v
            for k, v in (properties or {}).items()
            if k not in {"aliases", "confidence"}
        },
    }
    return node_to_embedding_text(node)


def _build_resolver(embedding_model: BaseEmbeddingModel) -> CompositeResolver:
    cfg = _live_app_config().extraction.resolution
    return CompositeResolver(
        embedding_model,
        fuzzy_threshold=cfg.fuzzy_threshold,
        semantic_threshold=cfg.semantic_threshold,
        type_strict=cfg.type_strict,
        embedding_cache_max_size=cfg.embedding_cache_max_size,
    )


# ---------------------------------------------------------------------------
# Task ① — clean + two-level chunk (rag stage: runs in BOTH modes)
# ---------------------------------------------------------------------------


async def _clean_and_chunk(
    document: Document,
    chunking: ChunkingConfig,
    opik_trace_headers: dict[str, str] | None = None,
) -> ChunkedDocument:
    """Clean one document and split it into its **Parent chunk** hierarchy.

    ADR-006 decisions 6 + 7: the **Clean step** is the FIRST memory stage (the
    splitter only ever sees cleaned text), then ``split_document`` yields the
    parents, each carrying its own children. Deterministic end-to-end — the same
    (document, chunking config) always produces the same payload, and that is
    what lets every downstream stage derive row ids from ``source_uri`` +
    position instead of a per-run ``uuid4()``.

    ``chunking`` is an explicit PARAMETER, not a config read inside the body, so
    it is part of the ``INPUTS`` cache key: an operator who flips
    ``memory.chunking.strategy`` to ``fixed_tokens``, drops ``memory`` and
    re-runs gets re-chunked parents instead of a 30-day-old cached payload
    built by the other splitter. Pydantic models hash cleanly under the
    ``INPUTS`` policy (JSON-serialised), so an unchanged config still hits.

    ``opik_trace_headers`` (passed by the flow) attaches this task's span to the
    flow's trace across the Prefect task boundary; excluded from the cache key.
    """

    log = _get_run_logger()
    with span(
        "_clean_and_chunk",
        tags=_EXTRACTION_TAGS,
        trace_headers=opik_trace_headers,
    ):
        content = clean_text(document.content or "")
        parents = split_document(content, chunking) if content else []
        chunked = ChunkedDocument(
            document_id=str(document.id),
            source_uri=document.source_uri,
            source_type=document.source_type.value,
            title=document.title,
            date=document.date.isoformat() if document.date else None,
            reference_uris=_reference_uris(document),
            parents=parents,
        )
        log.info(
            "clean_and_chunk: doc_id=%s n_parents=%d n_children=%d",
            chunked.document_id,
            len(parents),
            sum(len(parent.children) for parent in parents),
        )
        return chunked


def _reference_uris(document: Document) -> list[str]:
    """Resolve already-populated ``Document.references`` to their source URIs."""

    return [ref.source_uri for ref in document.references if isinstance(ref, Document)]


clean_and_chunk_task = task(
    _clean_and_chunk,
    name="clean-and-chunk",
    cache_policy=_INPUTS_NO_HEADERS,
    cache_expiration=timedelta(days=30),
    retries=1,
)


async def _split_documents(
    docs: list[Any],
    chunking: ChunkingConfig,
    opik_trace_headers: dict[str, str] | None = None,
) -> list[ChunkedDocument]:
    """Run task ① over every document, fanned out under a bounded semaphore
    sized by ``doc_concurrency`` (#059 R7).

    ``chunking`` is resolved ONCE by the flow at entry (like ``memory.mode``)
    and threaded through, so every document in a run is split by the same
    config AND that config is in each task's cache key.

    Task ① is purely CPU-bound — no shared LLM quota, no read-after-write — so
    the per-doc calls parallelize safely. ``doc_concurrency`` defaults to 1
    (serial-equivalent = today's exact behavior); operators opt into overlap via
    ``TREE_EXTRACTION__DOC_CONCURRENCY``.

    ``asyncio.gather`` preserves INPUT order, so ``chunked_docs`` comes back in
    the same order (and with the same contents) as a sequential loop would —
    downstream per-doc iteration stays deterministic. We keep the underlying
    Prefect task call (not ``.map()``) so its ``INPUTS`` cache still applies.
    """

    if not docs:
        return []

    semaphore = asyncio.Semaphore(_live_app_config().extraction.doc_concurrency)

    async def _one(doc: Any) -> ChunkedDocument:
        async with semaphore:
            return await clean_and_chunk_task(
                doc, chunking=chunking, opik_trace_headers=opik_trace_headers
            )

    return list(await asyncio.gather(*[_one(doc) for doc in docs]))


# ---------------------------------------------------------------------------
# Task ② — embed children (rag stage: runs in BOTH modes)
# ---------------------------------------------------------------------------


def child_embedding_texts(chunked_docs: list[ChunkedDocument]) -> list[str]:
    """Every child's **Contextual header** text for the run, sorted + unique.

    Sorted so the ``INPUTS`` cache key of task ② does not depend on document
    order; unique so two identical children (a boilerplate paragraph repeated
    across documents) are embedded once and share the vector.
    """

    return sorted(
        {
            child_embedding_text(
                title=chunked.title,
                heading_path=parent.heading_path,
                content=child.content,
            )
            for chunked in chunked_docs
            for parent in chunked.parents
            for child in parent.children
        }
    )


async def _embed_children(
    texts: list[str], opik_trace_headers: dict[str, str] | None = None
) -> dict[str, list[float]]:
    """Embed every **Child chunk** text of the run in as few requests as possible.

    Children are the ONLY rows the ingestion path embeds (ADR-006 decision 4):
    parents and documents are deliberately vector-less so parent-document
    retrieval can search children and return parents. Uses the **search** model
    — the persisted, index-coupled vector — through
    :func:`tree.memory.embedding_text.embed_in_batches`, which packs the texts
    into request-sized batches; the 429 backoff lives inside ``.embed()``.

    Returns a ``text -> vector`` map the loader indexes by the same text it
    rebuilds per child, so a missing vector degrades to ``embedding=[]`` (the
    indexing backfill picks it up) instead of misaligning rows.
    """

    log = _get_run_logger()
    with span(
        "_embed_children",
        tags=_EXTRACTION_TAGS,
        trace_headers=opik_trace_headers,
    ):
        if not texts:
            return {}

        vectors = await embed_in_batches(texts, get_search_embedding_model())
        log.info(
            "embed_children: n_texts=%d dim=%d",
            len(texts),
            len(vectors[0]) if vectors else 0,
        )
        return dict(zip(texts, vectors))


embed_children_task = task(
    _embed_children,
    name="embed-children",
    cache_policy=_INPUTS_NO_HEADERS,
    cache_expiration=timedelta(days=90),
    retries=2,
)


# ---------------------------------------------------------------------------
# Task ③ — load the RAG rows (rag stage: runs in BOTH modes)
# ---------------------------------------------------------------------------


async def _load_rag_rows(
    chunked_docs: list[ChunkedDocument],
    embeddings: dict[str, list[float]],
    database: Any,
    user_id: PydanticObjectId,
    opik_trace_headers: dict[str, str] | None = None,
) -> int:
    """Upsert the document / parent / child rows of the run in ONE ``bulk_write``.

    ``NO_CACHE`` because it writes: a cache hit would skip the write on a
    re-run that a previous crash left half-applied. Idempotent instead — every
    ``_id`` is deterministic, so re-running a document rewrites its own rows and
    the collection count does not move.

    Returns the number of rows written.
    """

    log = _get_run_logger()
    with span(
        "_load_rag_rows",
        tags=_EXTRACTION_TAGS,
        trace_headers=opik_trace_headers,
    ):
        ops = []
        for chunked in chunked_docs:
            ops.extend(
                build_rag_row_ops(
                    user_id=user_id,
                    document_id=chunked.document_id,
                    source_uri=chunked.source_uri,
                    source_type=chunked.source_type,
                    title=chunked.title,
                    date=chunked.date,
                    parents=chunked.parents,
                    child_vectors=embeddings,
                )
            )

        rows = await load_rag_rows(database=database, ops=ops)
        log.info(
            "load_rag_rows: documents=%d rows_written=%d",
            len(chunked_docs),
            rows,
        )
        return rows


load_rag_rows_task = task(
    _load_rag_rows,
    name="load-rag-rows",
    cache_policy=NO_CACHE,
    retries=3,
    retry_delay_seconds=10,
)


# ---------------------------------------------------------------------------
# Task ④ — LLM extraction over PARENT chunks (graph stage: graphrag only)
# ---------------------------------------------------------------------------


async def _llm_extract_entities(
    chunked: ChunkedDocument,
    user_id: PydanticObjectId,
    llm: BaseLLM | None = None,
    opik_trace_headers: dict[str, str] | None = None,
) -> RawExtraction:
    """Invoke the LLM once per **Parent chunk** and merge the extractions.

    Parents (4096 tokens by default) are the extraction unit, not children:
    ~8x fewer, larger calls than the pre-ADR-006 512-token window, and each call
    sees a whole section instead of a sentence fragment. Each emission is
    stamped with the parent ROW ``_id`` as its ``chunk_id`` provenance — needs
    ``user_id`` because that id is tenant-scoped.

    ``llm`` is optional so a caller that already holds a handle can inject it.
    When omitted the flow uses the default ``get_llm()`` factory.

    ``opik_trace_headers`` attaches this task's span to the flow's trace. The
    nested Gemini LLM spans (with native usage + cost) attach to THIS span via
    contextvars — they're in-process under the wrapped genai client, so the
    distributed-header hop is only needed at the task boundary, not per-parent.
    Excluded from the cache key.
    """

    log = _get_run_logger()
    with span(
        "_llm_extract_entities",
        tags=_EXTRACTION_TAGS,
        trace_headers=opik_trace_headers,
    ):
        if not chunked.parents:
            return RawExtraction(
                document_id=chunked.document_id,
                source_uri=chunked.source_uri,
                chunked=chunked,
                extracted=ExtractionResult(),
            )

        if llm is None:
            llm = get_llm()
        semaphore = asyncio.Semaphore(_live_app_config().extraction.llm_concurrency)

        async def _one(parent: ParentChunk) -> ExtractionResult:
            async with semaphore:
                return await extract_entities(
                    llm,
                    parent.content,
                    chunk_id=parent_row_id(user_id, chunked.source_uri, parent.index),
                )

        per_parent = await asyncio.gather(*[_one(parent) for parent in chunked.parents])

        merged = ExtractionResult()
        for piece in per_parent:
            merged = merged.merge(piece)

        log.info(
            "llm_extract_entities: doc_id=%s n_parents=%d n_entities_raw=%d "
            "n_edges_raw=%d",
            chunked.document_id,
            len(chunked.parents),
            len(merged.nodes),
            len(merged.edges),
        )
        return RawExtraction(
            document_id=chunked.document_id,
            source_uri=chunked.source_uri,
            chunked=chunked,
            extracted=merged,
        )


llm_extract_entities_task = task(
    _llm_extract_entities,
    name="llm-extract-entities",
    cache_policy=_INPUTS_NO_HEADERS,
    cache_expiration=timedelta(days=30),
    retries=2,
    retry_delay_seconds=15,
)


# ---------------------------------------------------------------------------
# Task ④.5 — envelope + field validation (#030)
# ---------------------------------------------------------------------------
#
# Lives between LLM extract and resolve so the resolver only sees rows
# that survived the envelope check. The validator is **strict at the
# envelope** (drops the whole row when ``type`` / ``semantic_type`` /
# pair / subtype is wrong) and **lenient at the field** (drops just the
# offending property). Both branches write audit rows to
# ``extraction_rejections`` / ``extraction_dropped_fields`` so prompt
# drift is structured signal, not a log line.


def _make_extractor_info() -> ExtractorInfo:
    """Build the per-run :class:`ExtractorInfo` provenance block.

    Reads the active LLM model name from ``app_config.models.llm.model``
    and pins the pipeline release tag from this Python package's
    ``__version__``. Both are stable across a single flow invocation.
    """

    cfg = _live_app_config()
    llm_name = cfg.models.llm.model
    try:
        # ``importlib.metadata`` is the canonical lookup; falls back to
        # a static string if the package isn't installed (test runners
        # that exercise the module without ``pip install -e .``).
        from importlib.metadata import PackageNotFoundError, version as _pkg_version

        try:
            pkg_version = _pkg_version("tree-memory")
        except PackageNotFoundError:
            pkg_version = "0.0.0+local"
    except Exception:  # noqa: BLE001
        pkg_version = "0.0.0+local"

    return ExtractorInfo(name=llm_name, version=f"tree-memory-{pkg_version}")


def _build_envelope_rejection_row(
    *,
    user_id: PydanticObjectId,
    document_id: PydanticObjectId | None,
    chunk_id: str | None,
    raw_row: dict[str, Any],
    reason: str,
    extractor: ExtractorInfo,
) -> dict[str, Any]:
    """Build one ``extraction_rejections`` audit row (no DB write).

    Rows are accumulated by :func:`_validate_raws` and flushed with a single
    ``insert_many`` (#059 R4) — the row contents are identical to the prior
    per-row ``insert_one`` path.
    """

    rejection = ExtractionRejection(
        user_id=user_id,
        document_id=document_id,
        chunk_id=chunk_id,
        timestamp=datetime.now(tz=UTC),
        rejected_at_stage="envelope",
        rejection_reason=reason,
        raw_row=truncate_raw_row(raw_row),
        extractor=extractor,
    )
    return rejection.model_dump(by_alias=True, exclude={"id"})


def _build_dropped_field_row(
    *,
    user_id: PydanticObjectId,
    document_id: PydanticObjectId | None,
    chunk_id: str | None,
    row_type: str,
    row_subtype: str | None,
    semantic_type: str | None,
    dropped_field: str,
    raw_value: Any,
    reason: str,
    extractor: ExtractorInfo,
) -> dict[str, Any]:
    """Build one ``extraction_dropped_fields`` audit row (no DB write)."""

    dropped = ExtractionDroppedField(
        user_id=user_id,
        document_id=document_id,
        chunk_id=chunk_id,
        timestamp=datetime.now(tz=UTC),
        row_type=row_type,
        row_subtype=row_subtype,
        semantic_type=semantic_type,
        dropped_field=dropped_field,
        raw_value=truncate_raw_value(raw_value),
        reason=reason,
        extractor=extractor,
    )
    return dropped.model_dump(by_alias=True, exclude={"id"})


@tracked_span("_validate_raws", tags=_EXTRACTION_TAGS)
async def _validate_raws(
    *,
    raws: list[RawExtraction],
    database: Any,
    user_id: PydanticObjectId,
    extractor: ExtractorInfo,
    opik_trace_headers: dict[str, str] | None = None,
) -> list[RawExtraction]:
    """Apply envelope + field validation to every LLM-extracted row.

    Mutates each :class:`RawExtraction` in place:

    * Nodes / edges that fail envelope validation are removed from
      ``raw.extracted`` and a row is inserted into
      ``extraction_rejections``.
    * Nodes / edges that pass envelope validation but have invalid /
      unknown fields keep the row (lenient policy) and write one row
      per dropped field to ``extraction_dropped_fields``. The
      surviving properties replace ``raw.extracted.*.properties``.

    Structural rows (``document`` / ``chunk`` / pipeline-emitted edges
    in ``raw.chunked.structural``) are intentionally **not** validated
    here — they're built deterministically by the pipeline, not by the
    LLM, so envelope drift is impossible.
    """

    log = _get_run_logger()
    # #059 R4: accumulate audit rows and flush each collection with a single
    # ``insert_many`` instead of per-row ``insert_one`` writes. The row contents
    # and the SET of rows written are unchanged.
    rejection_rows: list[dict[str, Any]] = []
    dropped_field_rows: list[dict[str, Any]] = []
    for raw in raws:
        document_id: PydanticObjectId | None
        try:
            document_id = PydanticObjectId(raw.document_id)
        except Exception:  # noqa: BLE001 — defensive against odd shapes
            document_id = None

        # #030: emissions the parser already dropped (unknown type,
        # invalid endpoints, ...) flow through ``raw_rejections``. Surface
        # each one as an ``extraction_rejections`` row.
        for rejection in raw.extracted.raw_rejections:
            rejection_rows.append(
                _build_envelope_rejection_row(
                    user_id=user_id,
                    document_id=document_id,
                    chunk_id=rejection.chunk_id or None,
                    raw_row=rejection.raw,
                    reason=rejection.reason,
                    extractor=extractor,
                )
            )
        raw.extracted.raw_rejections = []

        validated_nodes: list[ExtractedNode] = []
        for node in raw.extracted.nodes:
            type_value = (
                node.type.value if hasattr(node.type, "value") else str(node.type)
            )
            envelope = validate_envelope(
                kind="node",
                type=type_value,
                subtype=node.subtype,
                name=node.name,
            )
            if not envelope.ok:
                rejection_rows.append(
                    _build_envelope_rejection_row(
                        user_id=user_id,
                        document_id=document_id,
                        chunk_id=node.chunk_id or None,
                        raw_row={
                            "type": type_value,
                            "subtype": node.subtype,
                            "name": node.name,
                            "properties": node.properties,
                        },
                        reason=envelope.reason or "envelope_invalid",
                        extractor=extractor,
                    )
                )
                log.info(
                    "envelope_rejection node type=%s name=%r reason=%s",
                    type_value,
                    node.name,
                    envelope.reason,
                )
                continue

            parent_schema, extras_schema = get_node_property_schemas(
                type=type_value, subtype=node.subtype
            )
            validated_props, drops = validate_properties(
                node.properties or {}, parent_schema, extras_schema
            )
            for drop in drops:
                dropped_field_rows.append(
                    _build_dropped_field_row(
                        user_id=user_id,
                        document_id=document_id,
                        chunk_id=node.chunk_id or None,
                        row_type=type_value,
                        row_subtype=node.subtype,
                        semantic_type=None,
                        dropped_field=drop.field,
                        raw_value=drop.value,
                        reason=drop.reason,
                        extractor=extractor,
                    )
                )
            node.properties = validated_props
            validated_nodes.append(node)

        validated_edges: list[ExtractedEdge] = []
        for edge in raw.extracted.edges:
            type_value = (
                edge.type.value if hasattr(edge.type, "value") else str(edge.type)
            )
            src_type = (
                edge.source_type.value
                if hasattr(edge.source_type, "value")
                else str(edge.source_type)
            )
            tgt_type = (
                edge.target_type.value
                if hasattr(edge.target_type, "value")
                else str(edge.target_type)
            )
            envelope = validate_envelope(
                kind="edge",
                type=type_value,
                source_type=src_type,
                target_type=tgt_type,
                semantic_type=edge.semantic_type,
            )
            if not envelope.ok:
                rejection_rows.append(
                    _build_envelope_rejection_row(
                        user_id=user_id,
                        document_id=document_id,
                        chunk_id=edge.chunk_id or None,
                        raw_row={
                            "type": type_value,
                            "semantic_type": edge.semantic_type,
                            "source_type": src_type,
                            "source_node_id": edge.source_node_id,
                            "target_type": tgt_type,
                            "target_node_id": edge.target_node_id,
                            "properties": edge.properties,
                        },
                        reason=envelope.reason or "envelope_invalid",
                        extractor=extractor,
                    )
                )
                log.info(
                    "envelope_rejection edge type=%s semantic=%s reason=%s",
                    type_value,
                    edge.semantic_type,
                    envelope.reason,
                )
                continue

            edge_schema = get_edge_property_schema(
                type=type_value, semantic_type=edge.semantic_type
            )
            validated_props, drops = validate_properties(
                edge.properties or {}, edge_schema, None
            )
            for drop in drops:
                dropped_field_rows.append(
                    _build_dropped_field_row(
                        user_id=user_id,
                        document_id=document_id,
                        chunk_id=edge.chunk_id or None,
                        row_type=type_value,
                        row_subtype=None,
                        semantic_type=edge.semantic_type,
                        dropped_field=drop.field,
                        raw_value=drop.value,
                        reason=drop.reason,
                        extractor=extractor,
                    )
                )
            edge.properties = validated_props
            validated_edges.append(edge)

        raw.extracted.nodes = validated_nodes
        raw.extracted.edges = validated_edges

    # #059 R4: one ``insert_many`` per collection. Skip the call entirely when
    # nothing accumulated (empty/clean input) — ``insert_many([])`` would raise.
    if rejection_rows:
        await database["extraction_rejections"].insert_many(rejection_rows)
    if dropped_field_rows:
        await database["extraction_dropped_fields"].insert_many(dropped_field_rows)

    return raws


validate_raws_task = task(
    _validate_raws,
    name="validate-raws",
    cache_policy=NO_CACHE,
    retries=1,
)


# ---------------------------------------------------------------------------
# Task ⑤ — resolve entities (batched)
# ---------------------------------------------------------------------------


@tracked_span("_resolve_entities", tags=_EXTRACTION_TAGS)
async def _resolve_entities(
    raws: list[RawExtraction],
    database: Any,
    resolver: CompositeResolver,
    user_id: PydanticObjectId,
    opik_trace_headers: dict[str, str] | None = None,
) -> ResolutionOutput:
    """Fetch per-type candidates and run the resolver chain.

    The resolver is invoked PER TYPE so the alias map and candidate name list
    are both same-type — alias hits cannot cross types regardless of how
    ``CompositeResolver`` ranks them internally.

    The candidate-fetch query projects ``name``, ``canonical_name``, and
    ``aliases``. We feed the resolver the **set-union** of ``name`` and
    non-null ``canonical_name`` so a new mention of ``"John Smith"`` can
    match an existing node whose ``name="Jean Smith"`` and
    ``canonical_name="John Smith"``. The reverse map
    ``name_to_owner_id`` records the owning ``_id`` for each candidate
    string so task ⑥ can promote a canonical-name match to a soft join.
    """

    log = _get_run_logger()
    resolution_cfg = _live_app_config().extraction.resolution
    cap = resolution_cfg.max_candidates_per_type

    entities: list[tuple[str, NodeType]] = []
    entities_by_doc: dict[str, list[tuple[NodeType, str]]] = {}
    # Keep the originating node (with its properties) per entity key so we
    # can build the per-entity embeddable text once the resolver has chosen
    # a canonical_name.
    node_by_key: dict[str, ExtractedNode] = {}
    for raw in raws:
        per_doc: list[tuple[NodeType, str]] = []
        for node in raw.extracted.nodes:
            if node.type not in LLM_EXTRACTABLE_NODE_TYPES:
                continue
            entities.append((node.name, node.type))
            per_doc.append((node.type, node.name))
            node_by_key[make_entity_key(raw.document_id, node.type, node.name)] = node
        entities_by_doc[raw.document_id] = per_doc

    if not entities:
        log.info(
            "resolve_entities: n_entities=0 n_per_type={} candidates_seen_by_type={}"
        )
        return ResolutionOutput()

    by_type: dict[NodeType, list[tuple[str, NodeType]]] = {}
    for name, etype in entities:
        by_type.setdefault(etype, []).append((name, etype))

    candidates_seen_by_type: dict[str, int] = {}
    name_to_owner_id: dict[str, str] = {}
    resolved_by_key: dict[str, ResolvedEntity] = {}
    embeddable_text_by_key: dict[str, str] = {}

    # Fetch candidates and resolve per type. Candidate scope is restricted
    # to the run's ``user_id`` — cross-tenant rows are invisible to
    # resolution.
    for etype, entity_pairs in by_type.items():
        collection = database[MEMORY_COLLECTION]
        cursor = collection.find(
            {
                "user_id": user_id,
                "kind": "node",
                "type": etype.value,
                "merged_into": {"$in": [None, "", False]},
            },
            projection={
                "_id": 1,
                "name": 1,
                "canonical_name": 1,
                "aliases": 1,
            },
        ).limit(cap)

        candidate_docs: list[dict[str, Any]] = []
        async for doc in cursor:
            candidate_docs.append(doc)

        seen = len(candidate_docs)
        candidates_seen_by_type[etype.value] = seen
        if seen >= cap:
            log.warning(
                "%s candidate fetch hit cap (%d); resolution accuracy may degrade",
                etype.value.upper(),
                cap,
            )

        # Build the candidate name set (set-union of name and non-null canonical_name)
        # and the per-type alias map. ``name_to_owner_id`` is type-prefixed so
        # the same surface form under two types stays disambiguated.
        candidate_names: set[str] = set()
        alias_map: dict[str, list[str]] = {}
        for doc in candidate_docs:
            owner_id = str(doc.get("_id"))
            doc_name = doc.get("name")
            doc_canonical = doc.get("canonical_name")
            aliases = doc.get("aliases") or []

            if doc_name:
                candidate_names.add(doc_name)
                name_to_owner_id.setdefault(
                    make_type_name_key(etype, doc_name), owner_id
                )
            if doc_canonical:
                candidate_names.add(doc_canonical)
                name_to_owner_id.setdefault(
                    make_type_name_key(etype, doc_canonical), owner_id
                )
                # Alias map is keyed by canonical_name (resolver semantics).
                alias_map.setdefault(doc_canonical, []).extend(aliases)
            elif doc_name:
                alias_map.setdefault(doc_name, []).extend(aliases)

        existing_entities = {etype: sorted(candidate_names)}
        results = await resolver.resolve_with_types(
            entity_pairs,
            existing_entities=existing_entities,
            existing_aliases=alias_map,
        )
        for (name, _t), resolved in zip(entity_pairs, results):
            for doc_id, doc_entities in entities_by_doc.items():
                if (etype, name) in doc_entities:
                    key = make_entity_key(doc_id, etype, name)
                    resolved_by_key[key] = resolved
                    # Precompute the embeddable text now that the resolver
                    # has picked a canonical_name. Generic types → node-text;
                    # PREFERENCE/FACT → statement/object.
                    node = node_by_key.get(key)
                    embeddable_text_by_key[key] = _entity_embeddable_text(
                        entity_type=etype,
                        name=name,
                        canonical_name=resolved.canonical_name,
                        properties=(node.properties or {}) if node is not None else {},
                    )
                    break

    n_per_type = {t.value: len(v) for t, v in by_type.items()}
    log.info(
        "resolve_entities: n_entities=%d n_per_type=%s candidates_seen_by_type=%s",
        len(entities),
        n_per_type,
        candidates_seen_by_type,
    )

    return ResolutionOutput(
        entities=entities,
        resolved_by_key=resolved_by_key,
        name_to_owner_id=name_to_owner_id,
        candidates_seen_by_type=candidates_seen_by_type,
        embeddable_text_by_key=embeddable_text_by_key,
    )


resolve_entities_task = task(
    _resolve_entities,
    name="resolve-entities",
    cache_policy=NO_CACHE,
    retries=1,
)


# ---------------------------------------------------------------------------
# Task ⑥ — embed entities (ALL run node-texts in one batched call)
# ---------------------------------------------------------------------------


async def _embed_entities(
    texts: list[str], opik_trace_headers: dict[str, str] | None = None
) -> dict[str, list[float]]:
    """Embed every embeddable text for the run in one batched call.

    All the run's node-texts are packed into as few synchronous
    ``/v1/multimodalembeddings`` requests as the per-request caps (1000
    inputs / 320K tokens) allow via
    :func:`tree.memory.embedding_text.embed_in_batches`. Vectors come back
    positionally aligned, so we zip them to their texts and return a
    ``text -> vector`` map that task ⑤/⑥ index by embeddable text.

    Caches on ``INPUTS`` (the whole text list), so an identical re-run of the
    same document set is a cache hit; partial-overlap re-runs re-embed.
    Per-run dedup of identical texts happens upstream (the flow embeds
    ``sorted(set(...))``).

    Uses the **search** model — the persisted, index-coupled vector. The 429
    backoff is untouched: it lives inside ``.embed()``, called once per chunk.

    ``opik_trace_headers`` attaches this task's span to the flow's trace; the
    nested Voyage/Modal embed spans (usage + cost) nest under it via contextvars.
    Excluded from the cache key so a new run's headers don't bust the cache.
    """

    log = _get_run_logger()
    with span(
        "_embed_entities",
        tags=_EXTRACTION_TAGS,
        trace_headers=opik_trace_headers,
    ):
        if not texts:
            return {}

        embedding_model = get_search_embedding_model()
        vectors = await embed_in_batches(texts, embedding_model)
        log.info(
            "embed_entities: n_texts=%d dim=%d",
            len(texts),
            len(vectors[0]) if vectors else 0,
        )
        return dict(zip(texts, vectors))


embed_entities_task = task(
    _embed_entities,
    name="embed-entities",
    cache_policy=_INPUTS_NO_HEADERS,
    cache_expiration=timedelta(days=90),
    retries=2,
)


# ---------------------------------------------------------------------------
# Task ⑦ — dedup (batched)
# ---------------------------------------------------------------------------


@tracked_span("_dedupe_entities", tags=_EXTRACTION_TAGS)
async def _dedupe_entities(
    resolved: ResolutionOutput,
    embeddings: EmbeddingMap,
    database: Any,
    dedup_config: DeduplicationConfig,
    user_id: PydanticObjectId,
    opik_trace_headers: dict[str, str] | None = None,
) -> DedupMap:
    """For each resolved entity, run :func:`dedupe_entity` and bucket the
    decision under a stable per-entity key."""

    log = _get_run_logger()

    # ``dedupe_entity`` is a read-only ``$vectorSearch`` on PRECOMPUTED vectors
    # (no Voyage call, independent per entity), so the per-key decisions
    # parallelize safely. We gather them under a bounded semaphore sized by the
    # ``dedup_concurrency`` knob (#058). The per-key decision is a pure function
    # of its key, so concurrent evaluation cannot change any value; we rebuild
    # the ``decisions`` mapping and tallies in the original key order below so
    # the output is byte-identical to the prior sequential implementation.
    semaphore = asyncio.Semaphore(_live_app_config().extraction.dedup_concurrency)

    async def _one(key: str, resolved_entity: ResolvedEntity) -> DedupDecision:
        doc_id, type_value, name = key.split("|", maxsplit=2)
        entity_type = NodeType(type_value)

        # Dedup against the node-text vector (the same vector that will be
        # persisted on a non-merged node), keyed by the embeddable text task
        # ④ embedded. Falls back to the canonical-name key only when
        # ``embeddable_text_by_key`` was not populated.
        embeddable_text = resolved.embeddable_text_by_key.get(
            key, resolved_entity.canonical_name
        )
        embedding = embeddings.vectors.get(embeddable_text) or []

        if not embedding or not dedup_config.enabled:
            return DedupDecision(action="none")

        prospective_id = build_node_id(user_id, entity_type, _normalize(name))
        async with semaphore:
            raw = await dedupe_entity(
                database=database,
                user_id=user_id,
                name=name,
                entity_type=entity_type,
                embedding=embedding,
                config=dedup_config,
                incoming_node_id=prospective_id,
            )
        return _to_decision(raw, prospective_id)

    items = list(resolved.resolved_by_key.items())
    results = await asyncio.gather(
        *[_one(key, resolved_entity) for key, resolved_entity in items]
    )

    decisions: dict[str, DedupDecision] = {}
    n_merged = n_flagged = n_none = 0
    for (key, _resolved_entity), decision in zip(items, results):
        decisions[key] = decision
        if decision.action == "merged":
            n_merged += 1
        elif decision.action == "flagged":
            n_flagged += 1
        else:
            n_none += 1

    log.info(
        "dedupe_entities: n_merged=%d n_flagged=%d n_none=%d",
        n_merged,
        n_flagged,
        n_none,
    )
    return DedupMap(decisions=decisions)


def _to_decision(result: DeduplicationResult, prospective_id: str) -> DedupDecision:
    """Drop a self-match (top candidate == prospective_id) and project to a transit type."""

    if result.action != "none" and result.matched_node_id == prospective_id:
        return DedupDecision(action="none")
    return DedupDecision(
        action=result.action,
        matched_node_id=result.matched_node_id,
        matched_node_name=result.matched_node_name,
        similarity_score=result.similarity_score,
        match_type=result.match_type,
    )


dedupe_entities_task = task(
    _dedupe_entities,
    name="dedupe-entities",
    cache_policy=NO_CACHE,
    retries=1,
)


# ---------------------------------------------------------------------------
# Task ⑧ — apply writes (batched)
# ---------------------------------------------------------------------------


@tracked_span("_apply_writes", tags=_EXTRACTION_TAGS)
async def _apply_writes(
    raws: list[RawExtraction],
    resolved: ResolutionOutput,
    embeddings: EmbeddingMap,
    dedup_results: DedupMap,
    database: Any,
    resolver: CompositeResolver,
    dedup_config: DeduplicationConfig,
    embedding_model: BaseEmbeddingModel,
    user_id: PydanticObjectId,
    extractor: ExtractorInfo | None = None,
    opik_trace_headers: dict[str, str] | None = None,
) -> WriteSummary:
    """One pass over the resolved entities → ``add_entity()`` → edge upserts.

    Edges are remapped from raw extracted names to final ``target_id``s by
    consulting the in-batch ``name_to_target_id`` map (built while walking the
    nodes) plus the cross-batch ``name_to_owner_id`` map from task ③.
    Identical edges (after remapping) collapse into one upsert.

    Idempotent: ``add_entity`` and the edge upserts both use ``upsert=True``.
    """

    log = _get_run_logger()

    summary = WriteSummary(documents_processed=len(raws))
    name_to_target_id: dict[str, str] = {}

    # ----- Structural edges (PART_OF at both levels, NEXT, REFERENCED) -------
    # The document / parent / child ROWS were already upserted by the rag load
    # stage (task ③) in BOTH modes; ``graphrag`` only adds the edges that
    # express the same hierarchy. Seeding ``name_to_target_id`` with the
    # loader's deterministic row ids is what pins every structural endpoint —
    # and the MENTIONS edges built below — to the row the loader actually
    # wrote, instead of the ``_normalize``d fallback in ``_remap_endpoint``.
    structural_edges: list[ExtractedEdge] = []
    for raw in raws:
        structural_edges.extend(
            build_structural_entries(
                source_uri=raw.chunked.source_uri,
                parents=raw.chunked.parents,
                reference_uris=raw.chunked.reference_uris,
            ).edges
        )
        name_to_target_id.update(_rag_row_id_map(user_id, raw.chunked))

    # ----- LLM-extracted entities → add_entity ----------------------------------
    for raw in raws:
        for node in raw.extracted.nodes:
            if node.type not in LLM_EXTRACTABLE_NODE_TYPES:
                continue

            key = make_entity_key(raw.document_id, node.type, node.name)
            resolved_entity = resolved.resolved_by_key.get(key)
            decision = dedup_results.decisions.get(key, DedupDecision(action="none"))

            target_id = await _dispatch_entity_write(
                database=database,
                embedding_model=embedding_model,
                resolver=resolver,
                user_id=user_id,
                node=node,
                source_document_id=raw.document_id,
                resolved_entity=resolved_entity,
                decision=decision,
                embeddings=embeddings,
                resolved=resolved,
                dedup_config=dedup_config,
                summary=summary,
                extractor=extractor,
            )
            name_to_target_id[make_type_name_key(node.type, node.name)] = target_id

    # ----- MENTIONS edges (now that PERSON targets exist) ------------------------
    mentions: list[ExtractedEdge] = []
    for raw in raws:
        person_names = {
            n.name for n in raw.extracted.nodes if n.type == NodeType.PERSON
        }
        for person in person_names:
            mentions.append(
                ExtractedEdge(
                    source_node_id=raw.chunked.source_uri,
                    source_type=NodeType.DOCUMENT,
                    target_node_id=person,
                    target_type=NodeType.PERSON,
                    type=EdgeType.MENTIONS,
                )
            )

    # ----- All edges: remap + collapse + upsert ----------------------------------
    all_edges: list[ExtractedEdge] = []
    all_edges.extend(structural_edges)
    all_edges.extend(mentions)
    for raw in raws:
        all_edges.extend(raw.extracted.edges)

    seen_edge_ids: dict[str, ExtractedEdge] = {}
    for edge in all_edges:
        src_id = _remap_endpoint(
            edge.source_type, edge.source_node_id, name_to_target_id, user_id
        )
        tgt_id = _remap_endpoint(
            edge.target_type, edge.target_node_id, name_to_target_id, user_id
        )

        if not _edge_endpoints_valid(edge, src_id, tgt_id):
            continue

        edge_id = build_edge_id(src_id, edge.type, tgt_id)
        seen_edge_ids[edge_id] = ExtractedEdge(
            source_node_id=src_id,
            source_type=edge.source_type,
            target_node_id=tgt_id,
            target_type=edge.target_type,
            type=edge.type,
            semantic_type=edge.semantic_type,
            properties=edge.properties,
            chunk_id=edge.chunk_id,
        )

    # Upsert each collapsed edge. #057: accumulate every edge upsert and
    # flush them in a single ``bulk_write(ordered=False)`` instead of one
    # awaited round-trip per edge. ``seen_edge_ids`` already collapsed edges
    # to distinct ``_id``s with no read-after-write, so ordering is safe.
    edge_ops: list[UpdateOne] = []
    source_document_ids = [PydanticObjectId(raw.document_id) for raw in raws]
    for edge_id, edge in seen_edge_ids.items():
        # Only stamp ``extractor`` on LLM-extractable edges (today only
        # ``related_to``). Structural edges (``part_of``, ``next``,
        # ``mentions``, ``referenced``, ``has``, ``same_as``) skip the
        # column per ``plan.md:210``.
        edge_type_value = (
            edge.type.value if hasattr(edge.type, "value") else str(edge.type)
        )
        edge_extractor = extractor if edge_type_value == "related_to" else None
        edge_ops.append(
            _build_edge_op(
                user_id=user_id,
                edge=edge,
                edge_id=edge_id,
                source_document_ids=source_document_ids,
                extractor=edge_extractor,
            )
        )
        summary.edges_written += 1

    if edge_ops:
        await database[MEMORY_COLLECTION].bulk_write(edge_ops, ordered=False)

    log.info(
        "apply_writes: nodes_written=%d edges_written=%d same_as_emitted=%d "
        "nodes_merged=%d nodes_flagged=%d",
        summary.nodes_written,
        summary.edges_written,
        summary.same_as_edges_emitted,
        summary.nodes_merged,
        summary.nodes_flagged,
    )
    return summary


def _rag_row_id_map(
    user_id: PydanticObjectId, chunked: ChunkedDocument
) -> dict[str, str]:
    """Map every RAG row NAME of one document to the ``_id`` the loader wrote.

    Keyed like ``name_to_target_id`` (``"{type}|{name}"``) so ``_apply_writes``
    can merge it straight in. Pure id arithmetic — no DB read — because
    :mod:`tree.memory.rag.load` derives every ``_id`` from ``source_uri`` +
    position.
    """

    ids = {
        make_type_name_key(NodeType.DOCUMENT, chunked.source_uri): document_row_id(
            user_id, chunked.source_uri
        )
    }
    for parent in chunked.parents:
        ids[
            make_type_name_key(
                NodeType.CHUNK, parent_chunk_name(chunked.source_uri, parent.index)
            )
        ] = parent_row_id(user_id, chunked.source_uri, parent.index)
        for child in parent.children:
            ids[
                make_type_name_key(
                    NodeType.CHUNK,
                    child_chunk_name(chunked.source_uri, parent.index, child.index),
                )
            ] = child_row_id(user_id, chunked.source_uri, parent.index, child.index)
    return ids


def _remap_endpoint(
    etype: NodeType,
    name: str,
    name_to_target_id: dict[str, str],
    user_id: PydanticObjectId,
) -> str:
    """Map an extracted endpoint (raw name) to a final ``_id``.

    Falls back to ``build_node_id(user_id, etype, _normalize(name))`` for
    endpoints we did not see during the node-write pass (e.g. structural
    edges between chunks where the target id is the raw chunk name).
    """

    mapped = name_to_target_id.get(make_type_name_key(etype, name))
    if mapped is not None:
        return mapped
    return build_node_id(user_id, etype, _normalize(name))


def _edge_endpoints_valid(edge: ExtractedEdge, src_id: str, tgt_id: str) -> bool:
    """Drop self-loops that arose from auto-merging both endpoints to the same id.

    Allowed for SAME_AS (emitted explicitly by ``add_entity`` between distinct
    pre-merge ids; never via this code path) — but this remap path should
    never produce a SAME_AS edge between an id and itself.
    """

    return src_id != tgt_id


async def _dispatch_entity_write(
    *,
    database: Any,
    embedding_model: BaseEmbeddingModel,
    resolver: CompositeResolver,
    user_id: PydanticObjectId,
    node: ExtractedNode,
    source_document_id: str,
    resolved_entity: ResolvedEntity | None,
    decision: DedupDecision,
    embeddings: EmbeddingMap,
    resolved: ResolutionOutput,
    dedup_config: DeduplicationConfig,
    summary: WriteSummary,
    extractor: ExtractorInfo | None = None,
) -> str:
    """Write one LLM-extracted entity through ``add_entity``.

    The function shapes the inputs ``add_entity`` expects and tallies
    per-entity counters (merged/flagged/written). It also honors the
    "canonical-name-as-match-target" soft-join: when resolution chose a
    canonical_name that we already know maps to an owning ``_id`` in the
    existing graph, we let dedup decide whether to auto-merge into it
    (which preserves edges on the existing canonical without a fresh
    upsert).
    """

    # Inject the pre-computed embedding into ``add_entity`` via embedding_model.
    #
    # Task ④ already embedded this entity's embeddable text (GENERIC
    # node-text, or statement/object for PREFERENCE / FACT). We look that
    # vector up by the same embeddable-text key and wrap it in
    # ``_CachedSingleEmbedding`` so ``add_entity``'s internal
    # ``embedding_model.embed([...])`` returns it WITHOUT a second embed call.
    # ``add_entity`` rebuilds the identical text via its own
    # ``_embeddable_text`` and persists this same vector on the non-merged
    # path — dedup vector == persisted vector, computed once.
    key = make_entity_key(source_document_id, node.type, node.name)
    canonical = (
        resolved_entity.canonical_name if resolved_entity is not None else node.name
    )
    embeddable_text = resolved.embeddable_text_by_key.get(key)
    cached_vec = (
        embeddings.vectors.get(embeddable_text)
        if embeddable_text is not None
        else embeddings.vectors.get(canonical)
    )
    model_for_call: BaseEmbeddingModel = (
        _CachedSingleEmbedding(cached_vec) if cached_vec else embedding_model
    )

    candidate_names = sorted(
        {
            name
            for ttype, name in (key.split("|")[1:] for key in resolved.resolved_by_key)
            if NodeType(ttype) == node.type
        }
    )

    target_id, _resolved, dedup_result = await add_entity(
        database=database,
        embedding_model=model_for_call,
        resolver=resolver,
        user_id=user_id,
        name=node.name,
        entity_type=node.type,
        subtype=node.subtype,
        properties=node.properties,
        source_id=source_document_id,
        dedup_config=dedup_config,
        candidate_names=candidate_names,
        extractor=extractor,
    )

    summary.nodes_written += 1
    if dedup_result.action == "merged":
        summary.nodes_merged += 1
    elif dedup_result.action == "flagged":
        summary.nodes_flagged += 1
        summary.same_as_edges_emitted += 1

    # Drive caching/decision plumbing from the up-stream task ⑤ result too —
    # the in-flight ``add_entity`` call returned its own decision (driven by
    # ``dedupe_entity`` over the same vector). When ⑤ said "merged" and the
    # call agrees, we don't double-count.
    if decision.action == "merged" and dedup_result.action != "merged":
        # Disagreement — usually means the dedup index changed between ⑤ and ⑥.
        # Trust the in-call decision; task ⑤'s count is best-effort.
        pass

    return target_id


class _CachedSingleEmbedding(BaseEmbeddingModel):
    """Return a pre-computed vector regardless of input text.

    Used inside ``apply_writes`` so ``add_entity``'s internal
    ``embedding_model.embed([name])`` reuses the vector task ④ already
    computed instead of paying for it twice.
    """

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    @property
    def dimensions(self) -> int:
        return len(self._vector)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector for _ in texts]


def _build_edge_op(
    *,
    user_id: PydanticObjectId,
    edge: ExtractedEdge,
    edge_id: str,
    source_document_ids: list[PydanticObjectId],
    extractor: ExtractorInfo | None = None,
) -> UpdateOne:
    """Build the upsert op for one collapsed edge document.

    #030: ``extractor`` is stamped on LLM-extracted edges (today only
    ``related_to`` rows). Structural edges pass ``extractor=None`` and
    leave the column unset on the row.

    Returns the :class:`UpdateOne` op so the caller can accumulate every
    edge write into a single ``bulk_write(ops, ordered=False)`` round-trip
    (#057).
    """

    now = datetime.now(tz=UTC)
    set_stage: dict[str, Any] = {
        "user_id": user_id,
        "kind": "edge",
        "type": edge.type.value,
        # #029: persist ``semantic_type`` (None on non-related_to).
        "semantic_type": edge.semantic_type,
        "source_node_id": edge.source_node_id,
        "source_type": edge.source_type.value,
        "target_node_id": edge.target_node_id,
        "target_type": edge.target_type.value,
        "properties": {
            "$mergeObjects": [
                {"$ifNull": ["$properties", {}]},
                edge.properties,
            ]
        },
        "sources": {
            "$setUnion": [
                {"$ifNull": ["$sources", []]},
                source_document_ids,
            ]
        },
        "created_at": {"$ifNull": ["$created_at", now]},
        "updated_at": now,
    }
    if extractor is not None:
        set_stage["extractor"] = extractor.model_dump()
    return UpdateOne(
        {"_id": edge_id},
        [{"$set": set_stage}],
        upsert=True,
    )


apply_writes_task = task(
    _apply_writes,
    name="apply-writes",
    cache_policy=NO_CACHE,
    retries=3,
    retry_delay_seconds=10,
)


# ---------------------------------------------------------------------------
# Coordinator path — document-shard fan-out dispatching the worker (#067)
# ---------------------------------------------------------------------------


async def _coordinate_sharded_extraction(
    *,
    user_id: PydanticObjectId,
    document_ids: list[str] | None,
    num_shards: int,
    opik_trace_headers: dict[str, str] | None = None,
) -> FanOutStats:
    """Run the coordinator path: resolve pending docs, partition, fan out.

    The body of the ``memory-extract-etl-coordinator`` flow. Resolves the user's
    pending documents when ``document_ids is None`` (an explicit list is used
    verbatim), partitions them into ``min(num_shards, N)`` balanced shards, then
    dispatches one ``memory-extract-etl-worker`` run per shard (each carrying only
    ``{user_id, document_ids}`` — NO ``num_shards`` key, the worker has no such
    param) under ``asyncio.gather(return_exceptions=True)``. Dispatches to the
    WORKER deployment — there is NO recursion. Indexing is NOT part of this path:
    it is phase 3 of ``offline_pipeline`` (ADR-007 Decision 5).

    An empty resolved/explicit doc set is a clean no-op: zero worker dispatch,
    ``FanOutStats(shards_total=0)``.
    """

    log = _get_run_logger()
    effective_num_shards = _resolve_num_shards(num_shards)

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = client[settings.mongo.mongo_initdb_database]

    if document_ids is None:
        ids = await _resolve_pending_document_ids(database=database, user_id=user_id)
        log.info(
            "extraction fan-out: resolved %d pending document(s) for user_id=%s",
            len(ids),
            user_id,
        )
    else:
        ids = list(document_ids)
        log.info(
            "extraction fan-out: using %d explicit document_id(s) for user_id=%s",
            len(ids),
            user_id,
        )

    if not ids:
        log.info(
            "extraction fan-out: no pending documents for user_id=%s — nothing "
            "to do (no child runs)",
            user_id,
        )
        return FanOutStats(shards_total=0)

    shards = _partition_into_shards(ids, effective_num_shards)
    log.info(
        "extraction fan-out: partitioned %d document(s) into %d shard(s) "
        "(num_shards=%d)",
        len(ids),
        len(shards),
        effective_num_shards,
    )

    return await _fan_out_extraction(
        user_id=user_id,
        shards=shards,
        run_deployment=run_deployment,
        opik_trace_headers=opik_trace_headers,
    )


# ---------------------------------------------------------------------------
# Coordinator flow — memory-extract-etl-coordinator (#067)
# ---------------------------------------------------------------------------


@flow(name="memory-extract-etl-coordinator", log_prints=True)
async def memory_extract_etl_coordinator(
    user_id: PydanticObjectId,
    document_ids: list[str] | None = None,
    num_shards: int = 1,
) -> FanOutStats:
    """Resolve → partition → dispatch ``memory-extract-etl-worker`` runs.

    The operator entrypoint for memory extraction (ADR-002 §3, amended #066). Resolves
    the user's pending documents when ``document_ids is None`` (an explicit list is used
    verbatim), partitions them into ``min(num_shards, N)`` balanced shards, and dispatches
    ONE ``memory-extract-etl-worker`` run per shard via ``run_deployment`` under
    ``asyncio.gather(return_exceptions=True)``. Each worker dispatch carries only
    ``{user_id, document_ids}`` — there is NO ``num_shards`` child key (the worker has no
    such param) and NO recursion (it dispatches a DISTINCT worker deployment). One
    shard's failure is isolated and recorded in :class:`FanOutStats.failures`.

    Extraction ONLY: the Coordinator does not index. ``memory_indexing`` runs as
    phase 3 of ``offline_pipeline``, once per target user, after every user's
    extraction (ADR-007 Decision 5 — this superseded ADR-002 §3's trailing-index
    rule). A partial extraction is still indexed, because the phase runs
    regardless of how many shards failed.

    ``num_shards=1`` (the default) dispatches 1 worker run — it is NOT a
    byte-identical in-process extraction (that is the worker, triggered directly). An
    empty resolved/explicit doc set is a clean no-op: zero worker dispatch,
    ``FanOutStats(shards_total=0)``.
    """

    # Configure Opik in this flow-run process (idempotent; no-op without a key)
    # and own ONE trace for the whole coordinated run. The trace's distributed
    # headers are forwarded to each worker so the coordinator → workers chain
    # renders as a SINGLE trace.
    configure_opik()
    try:
        with span(
            "memory-extract-etl-coordinator",
            tags=_EXTRACTION_TAGS,
            metadata=_EXTRACTION_METADATA,
        ):
            headers = get_distributed_trace_headers()
            return await _coordinate_sharded_extraction(
                user_id=user_id,
                document_ids=document_ids,
                num_shards=num_shards,
                opik_trace_headers=headers,
            )
    finally:
        # Flush batched Opik telemetry (fail-open; no-op without OPIK_API_KEY).
        flush_opik()


# ---------------------------------------------------------------------------
# Worker flow — memory-extract-etl-worker (#067)
# ---------------------------------------------------------------------------


@flow(name="memory-extract-etl-worker", log_prints=True)
async def memory_extract_etl_worker(
    user_id: PydanticObjectId,
    document_ids: list[str] | None = None,
    opik_trace_headers: dict[str, str] | None = None,
) -> WriteSummary:
    """Extract knowledge-graph entries from documents for ``user_id`` (pure worker).

    The six-task extraction body. ``user_id`` is a required, non-Optional Prefect
    parameter. Every task receives the value; candidate fetches and writes are scoped
    to ``user_id`` end-to-end. Cross-tenant rows are invisible to resolution / dedup.

    Fetch semantics: explicit ``document_ids`` → fetch those user-scoped docs;
    ``document_ids is None`` → fetch all ``content != None`` docs for the user; zero
    docs → ``WriteSummary(documents_processed=0)``.

    This is PURE extraction: NO ``num_shards``, NO coordinator branch, NO
    ``run_deployment`` (no self-dispatch), NO ``memory_indexing`` call. It is the
    coordinator's internal dispatch target, but may also be triggered directly for a
    bare extraction with no indexing (e.g. debugging).

    Observability (#monitoring-fix): configures Opik at entry (Prefect runs flow
    runs in subprocesses where serve-time config never happened — without this
    the Gemini client is wrapped unwrapped, so no LLM spans / usage / cost), then
    owns ONE trace for the whole worker run. ``opik_trace_headers`` is forwarded
    by the coordinator so the coordinator → worker → indexing chain is ONE
    trace; when triggered standalone it is ``None`` and the worker starts its own
    trace. Every task receives the run's distributed-trace headers so its span
    nests under this trace instead of minting a fresh root.

    Cross-key config validation runs at import time (``settings = Settings()``
    in ``app_config.py``). If the validator raises, the flow never starts;
    callers see the ``ValueError`` straight from the first attribute access on
    ``app_config.extraction``.
    """

    log = _get_run_logger()
    # Configure Opik in THIS flow-run process before any model factory runs, so
    # ``get_llm()`` returns an Opik-wrapped Gemini client (nested LLM spans +
    # native usage/cost). Idempotent + no-op without OPIK_API_KEY.
    configure_opik()

    try:
        with span(
            "memory-extract-etl-worker",
            tags=_EXTRACTION_TAGS,
            trace_headers=opik_trace_headers,
            metadata=_EXTRACTION_METADATA,
        ):
            return await _run_extraction_worker_body(
                user_id=user_id,
                document_ids=document_ids,
                log=log,
            )
    finally:
        # Flush batched Opik telemetry so spans aren't lost in the long-lived
        # serve worker (fail-open; no-op without OPIK_API_KEY). Runs AFTER the
        # root span closes so the whole trace is flushed as a unit.
        flush_opik()


async def _run_extraction_worker_body(
    *,
    user_id: PydanticObjectId,
    document_ids: list[str] | None,
    log: logging.Logger,
) -> WriteSummary:
    """The ingestion body, run inside the worker's root Opik span.

    Reads ``memory.mode`` ONCE, up front (ADR-006 decision 5): the rag stages
    always run, the graph stages only in ``graphrag``. Grabs the run's
    distributed-trace headers ONCE (so every task span attaches to the worker
    trace) and threads them through every task invocation.
    """

    # Headers for the CURRENT trace (the worker root span opened above). Passed
    # to every task so its span attaches here instead of minting a fresh root.
    headers = get_distributed_trace_headers()

    # Validate config invariants up-front (also re-checked on every dedup call).
    # We re-load so env-var changes since import-time are honored — gives tests
    # a deterministic seam for the "misconfig fails at flow entry" AC, and it is
    # the ONE read of ``memory.mode`` for the whole run.
    try:
        config = load_app_config()
    except Exception as exc:
        # Surface the underlying ValueError directly (Pydantic wraps it in
        # ValidationError; unwrap so callers can match on ``ValueError``).
        unwrapped = _unwrap_validation_error(exc)
        raise unwrapped from exc
    mode = config.memory.mode
    # Built at ENTRY, not at first use: :class:`DeduplicationConfig` re-validates
    # its threshold ordering in ``__post_init__``, so a misconfigured pair fails
    # the run before a single row is written (only ``graphrag`` consumes it).
    dedup_config = _build_dedup_config()

    # Initialize the DB connection (Beanie + Motor handles) ---------------------
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = client[settings.mongo.mongo_initdb_database]

    # ----- Fetch documents (scoped to user_id) -------------------------------
    if document_ids:
        docs = await Document.find(
            {
                "_id": {"$in": [PydanticObjectId(did) for did in document_ids]},
                "user_id": user_id,
            }
        ).to_list()
    else:
        docs = await Document.find(
            {"user_id": user_id, "content": {"$ne": None}}
        ).to_list()

    log.info(
        "Processing %d documents in %s mode (user_id=%s)", len(docs), mode, user_id
    )

    if not docs:
        return WriteSummary(documents_processed=0)

    # ----- Task ① — clean + chunk (per-doc fan-out) -------------------------
    # Fans out under a bounded semaphore sized by ``doc_concurrency`` (#059 R7,
    # default 1 = serial-equivalent). gather preserves order so ``chunked_docs``
    # stays deterministic for the loops below. The chunking config resolved at
    # entry rides along as a task PARAMETER so it is in task ①'s cache key
    # (#112): a re-run after a strategy flip must re-chunk, not replay.
    chunked_docs: list[ChunkedDocument] = await _split_documents(
        docs, config.memory.chunking, opik_trace_headers=headers
    )

    # ----- Task ② — embed every child's contextual-header text --------------
    child_vectors = await embed_children_task(
        child_embedding_texts(chunked_docs), opik_trace_headers=headers
    )

    # ----- Task ③ — load the document / parent / child rows -----------------
    rag_rows = await load_rag_rows_task(
        chunked_docs,
        child_vectors,
        database,
        user_id,
        opik_trace_headers=headers,
    )

    if mode == "rag":
        # Chapter-4 system: node rows only. No LLM call, no edges — the
        # hierarchy lives in ``parent_id`` on the rows we just wrote.
        summary = WriteSummary(
            documents_processed=len(docs),
            nodes_written=rag_rows,
            edges_written=0,
        )
        log.info(
            "memory_extract_etl_worker complete (rag): documents=%d nodes_written=%d",
            summary.documents_processed,
            summary.nodes_written,
        )
        return summary

    # ====================== graphrag-only stages from here ==================

    # Two distinct embedding handles coexist in the flow:
    #   * ``resolution_embedding_model`` — feeds the resolver's semantic stage,
    #     which embeds the entity NAME only. Transient: discarded by the
    #     resolver's bounded LRU, never persisted, not index-coupled.
    #   * ``search_embedding_model`` — the persisted, index-coupled vector. Feeds
    #     dedup, supersession (statement-vs-persisted-statement), and the node
    #     ``embedding`` written by apply-writes.
    resolution_embedding_model = get_resolution_embedding_model()
    search_embedding_model = get_search_embedding_model()
    resolver = _build_resolver(resolution_embedding_model)

    # ----- Load the active user (for the first-person resolver) -------------
    user = await User.get(user_id)
    if user is None:
        # Defensive — the upstream Prefect parameter validation guarantees
        # a non-None user_id, but a stale id is possible.
        raise ValueError(f"User {user_id} not found; cannot run extraction.")

    # ----- Task ④ — LLM extraction, once per PARENT chunk -------------------
    raws: list[RawExtraction] = []
    for chunked in chunked_docs:
        raws.append(
            await llm_extract_entities_task(
                chunked, user_id, opik_trace_headers=headers
            )
        )

    # ----- Task ④.5 — envelope + field validation (#030) -------------------
    # Runs BEFORE the first-person resolver so the resolver never sees
    # rows the envelope would reject. The lenient field-level pass
    # also strips bad property values (e.g. ``aliases: 5``) that
    # would otherwise blow up downstream iterators.
    extractor = _make_extractor_info()
    await validate_raws_task(
        raws=raws,
        database=database,
        user_id=user_id,
        extractor=extractor,
        opik_trace_headers=headers,
    )

    # ----- First-person resolver (post-validate, pre-resolve) --------------
    for raw in raws:
        raw.extracted.nodes = redirect_first_person(raw.extracted.nodes, user)

    # ----- #032 — canonicalize preference names (#032 fix-3) ---------------
    # Rewrite every preference's ``name`` to a deterministic slug of
    # ``properties.statement`` so the LLM's drift between e.g.
    # ``"prefers dark mode for editors"`` (sentence form) and
    # ``"prefers-light-mode"`` (kebab-case) doesn't break the
    # ``_id`` contract. Must run BEFORE the supersession resolver so
    # candidate-row IDs line up.
    canonicalize_preference_names(raws)

    # ----- Supersession resolver branch (pre-dedup) -----------------------
    # Runs BEFORE the standard dedup so contradiction trumps dedup.
    # Mutates `raws` in place: when a
    # preference / fact row supersedes a prior one we write the
    # supersession to MongoDB and mark the in-memory node so the
    # apply-writes step preserves ``valid_from``.
    judge_llm = get_llm()
    await resolve_supersessions(
        database=database,
        user_id=user_id,
        llm=judge_llm,
        # Supersession embeds the new statement and compares it against
        # PERSISTED preference (statement) vectors — same space → SEARCH
        # model, NOT the resolution model.
        embedding_model=search_embedding_model,
        raws=raws,
    )

    # ----- Deterministic ``has: person:self -> preference`` -----
    # The LLM is told NOT to emit ``has`` (it is
    # ``llm_extractable=False``). The pipeline owns the edge.
    await write_self_has_preference_edges(database=database, user_id=user_id, raws=raws)

    # ----- Task ⑤ — resolve ------------------------------------------------
    resolved = await resolve_entities_task(
        raws, database, resolver, user_id, opik_trace_headers=headers
    )

    # ----- Task ⑥ — embed entities (ALL run node-texts in one batched call) -
    # Single batched embed of every unique node-text for the run.
    # ``embed_entities_task`` packs them into as few synchronous requests as
    # the 1000-input / 320K-token caps allow.
    embeddable_texts = sorted(set(resolved.embeddable_text_by_key.values()))
    vectors = await embed_entities_task(embeddable_texts, opik_trace_headers=headers)
    embeddings = EmbeddingMap(vectors=vectors)

    # ----- Task ⑦ — dedup --------------------------------------------------
    dedup_results = await dedupe_entities_task(
        resolved,
        embeddings,
        database,
        dedup_config,
        user_id,
        opik_trace_headers=headers,
    )

    # ----- Task ⑧ — apply writes ------------------------------------------
    # Apply-writes persists the node ``embedding`` → SEARCH model. (The
    # task-⑥ vector reuse already pins the persisted vector to the search
    # model; this handle is the fallback ``embedding_model`` for any node
    # that lacks a pre-computed vector.)
    summary = await apply_writes_task(
        raws,
        resolved,
        embeddings,
        dedup_results,
        database,
        resolver,
        dedup_config,
        search_embedding_model,
        user_id,
        extractor,
        opik_trace_headers=headers,
    )
    # The rag rows are nodes too — count them once, here, so the summary means
    # "rows this run wrote" in both modes.
    summary.nodes_written += rag_rows

    log.info(
        "memory_extract_etl_worker complete (graphrag): documents=%d "
        "nodes_written=%d edges_written=%d "
        "nodes_merged=%d nodes_flagged=%d same_as_edges_emitted=%d",
        summary.documents_processed,
        summary.nodes_written,
        summary.edges_written,
        summary.nodes_merged,
        summary.nodes_flagged,
        summary.same_as_edges_emitted,
    )
    return summary


# ---------------------------------------------------------------------------
# Indexing flow — memory-indexing-etl
# ---------------------------------------------------------------------------
#
# Ingestion telemetry tags for the indexing tasks. Each task opens its span via
# :func:`tree.observability.span` attached to the flow's trace through the
# ``opik_trace_headers`` parameter (the coordinator forwards its trace headers
# so indexing nests under the same trace as the extraction it follows). Nested
# embedding spans (Voyage/Modal) attach to the task span via contextvars.
#
# Pipeline-identity tags: the memory-indexing pipeline's tags, shared 1:1 with
# its Prefect deployment / flow-run tags. The pipeline name also rides as span
# metadata (``pipeline="indexing"``).
_INDEXING_TAGS = TAGS_INDEXING
_INDEXING_METADATA = pipeline_metadata("indexing")


async def _embed_nodes(
    client: Any,
    database: str,
    user_id: PydanticObjectId,
    opik_trace_headers: dict[str, str] | None = None,
) -> int:
    with span(
        "embed_nodes_task", tags=_INDEXING_TAGS, trace_headers=opik_trace_headers
    ):
        embedding_model = get_embedding_model()
        return await embed_nodes(client, database, embedding_model, user_id)


async def _ensure_indexes(
    client: Any,
    database: str,
    user_id: PydanticObjectId,
    opik_trace_headers: dict[str, str] | None = None,
) -> None:
    with span(
        "ensure_indexes_task", tags=_INDEXING_TAGS, trace_headers=opik_trace_headers
    ):
        embedding_model = get_embedding_model()
        await ensure_indexes(
            client, database, embedding_model=embedding_model, user_id=user_id
        )


embed_nodes_task = task(
    _embed_nodes,
    name="embed-kg-nodes",
    retries=1,
    retry_delay_seconds=10,
    cache_policy=NO_CACHE,
)

ensure_indexes_task = task(
    _ensure_indexes,
    name="ensure-kg-indexes",
    retries=1,
    retry_delay_seconds=5,
    cache_policy=NO_CACHE,
)


@flow(name="memory-indexing-etl", log_prints=True)
async def memory_indexing(
    user_id: PydanticObjectId,
    opik_trace_headers: dict[str, str] | None = None,
) -> int:
    """Backfill missing embeddings and ensure search indexes for ``user_id``.

    ``user_id`` is required and threaded through both tasks. ``embed_nodes``
    only processes rows belonging to the run's tenant — and only the rows that
    are supposed to carry a vector (child chunks + entity nodes, ADR-006
    decision 4); ``ensure_indexes`` re-asserts the global compound indexes whose
    leading key is ``user_id``.

    Returns the number of rows embedded — the count the flow already computes
    for its final log line — so ``offline_pipeline``'s indexing phase can report
    ``{"embedded": n}`` per user instead of an opaque ``None``.

    Observability: configures Opik at entry (subprocess-safe) and owns ONE
    trace. ``opik_trace_headers`` is forwarded by the caller (``online_pipeline``
    for its single-document run) so the indexing run nests under the SAME trace;
    when triggered standalone it is ``None`` and indexing starts its own trace.
    Both tasks receive the run's distributed-trace headers so their spans nest
    under this trace.
    """

    configure_opik()
    try:
        with span(
            "memory-indexing-etl",
            tags=_INDEXING_TAGS,
            trace_headers=opik_trace_headers,
            metadata=_INDEXING_METADATA,
        ):
            client = await init_mongodb(
                settings.mongo.mongo_uri.get_secret_value(),
                settings.mongo.mongo_initdb_database,
            )
            database = settings.mongo.mongo_initdb_database

            # Headers for THIS run's trace (the indexing root span), passed to
            # each task so its span nests here.
            headers = get_distributed_trace_headers()

            count = await embed_nodes_task(
                client, database, user_id, opik_trace_headers=headers
            )
            await ensure_indexes_task(
                client, database, user_id, opik_trace_headers=headers
            )

            # Boot-time gate: assert the live mongot vector index agrees with
            # ``app_config.models.search_embedding.dimensions``. Runs AFTER
            # ``ensure_indexes`` so a freshly bootstrapped index passes;
            # mismatch → hard-fail before the next pipeline run silently writes
            # vectors of the wrong dimension.
            await assert_settings_match_live_vector_index(client, database)

            logger.info(
                "Indexing pipeline finished for user_id=%s. Embedded %d nodes.",
                user_id,
                count,
            )
            return count
    finally:
        # Flush batched Opik telemetry (fail-open; no-op without OPIK_API_KEY).
        flush_opik()
