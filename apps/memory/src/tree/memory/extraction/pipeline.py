"""Prefect tasks and flow for the memory extraction pipeline.

Six explicit Prefect tasks. Expensive stages (LLM extract, embedding) cache
on ``INPUTS`` so re-runs only redo the cheap stages. The flow constructs the
resolver + dedup config ONCE at entry; the cross-key validator on
:class:`tree.config.app_config.ExtractionConfig` raises before any work runs
when the resolver and dedup type-strictness disagree.

External interface unchanged: ``memory_extraction(document_ids=...)``.
Internal task topology (per the §7 spec in
``tracker/012-extraction-pipeline-six-tasks.groomed.md``):

* ① ``extract_chunks_and_structural_task`` — per-doc, ``INPUTS`` cache.
* ② ``llm_extract_entities_task`` — per-doc, ``INPUTS`` cache, 2 retries.
* ③ ``resolve_entities_task`` — batched, ``NO_CACHE``, 1 retry.
* ④ ``embed_entities_task`` — ``.map(unique_canonical_names)``, ``INPUTS`` cache.
* ⑤ ``dedupe_entities_task`` — batched, ``NO_CACHE``, 1 retry.
* ⑥ ``apply_writes_task`` — batched, ``NO_CACHE``, 3 retries.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from beanie import PydanticObjectId
from prefect import flow, get_run_logger, task
from prefect.cache_policies import INPUTS, NO_CACHE

from tree.config.app_config import load_app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.documents import Document
from tree.entities.extraction_audit import (
    ExtractionDroppedField,
    ExtractionRejection,
    truncate_raw_row,
    truncate_raw_value,
)
from tree.entities.knowledge_graph import (
    EdgeType,
    ExtractorInfo,
    NodeType,
    build_edge_id,
    build_node_id,
)
from tree.entities.ontology import LLM_EXTRACTABLE_NODE_TYPES
from tree.entities.users import User
from tree.memory.embedding_text import embed_in_batches, node_to_embedding_text
from tree.memory.extraction.add_entity import add_entity
from tree.memory.extraction.core import (
    build_structural_entries,
    chunk_document,
    extract_entities,
)
from tree.memory.extraction.dedup import (
    DeduplicationConfig,
    DeduplicationResult,
    MergeStrategy,
    dedupe_entity,
)
from tree.memory.extraction.first_person_resolver import redirect_first_person
from tree.memory.extraction.preference_supersession import (
    canonicalize_preference_names,
    resolve_supersessions,
    write_self_has_preference_edges,
)
from tree.memory.extraction.validation import (
    get_edge_property_schema,
    get_node_property_schemas,
    validate_envelope,
    validate_properties,
)
from tree.memory.resolution.composite import CompositeResolver
from tree.memory.resolution.types import ResolvedEntity, _normalize
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
    get_llm,
    get_resolution_embedding_model,
    get_search_embedding_model,
)

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"


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
    """Embeddable text for one extracted entity (#042).

    Mirrors :func:`tree.memory.extraction.add_entity._embeddable_text` so
    the vector task ④ pre-computes (and that ⑤ deduplicates against and ⑥
    persists) is byte-for-byte the text ``add_entity`` would build for the
    same node — GENERIC types embed their node-text (the shared #041
    :func:`node_to_embedding_text`), PREFERENCE / FACT embed
    ``properties.statement`` / ``properties.object`` (the #032
    statement-embedding contract). Keeping the two builders in lock-step is
    what lets the inline ``_CachedSingleEmbedding`` reuse work and what
    makes the indexing backfill a no-op for dedup-created nodes.
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
    """Construct the composite resolver from the YAML resolution config."""

    cfg = _live_app_config().extraction.resolution
    return CompositeResolver(
        embedding_model,
        fuzzy_threshold=cfg.fuzzy_threshold,
        semantic_threshold=cfg.semantic_threshold,
        type_strict=cfg.type_strict,
        embedding_cache_max_size=cfg.embedding_cache_max_size,
    )


# ---------------------------------------------------------------------------
# Task ① — extract chunks + structural entries
# ---------------------------------------------------------------------------


async def _extract_chunks_and_structural(document: Document) -> ChunkedDocument:
    """Pure-function body. Stamps chunk ids and structural entries deterministically."""

    log = _get_run_logger()
    content = document.content or ""
    chunk_texts = chunk_document(content) if content else []
    chunk_ids = [str(uuid4()) for _ in chunk_texts]

    structural = (
        build_structural_entries(
            document_id=document.id,
            source_type=document.source_type.value,
            source_uri=document.source_uri,
            date=document.date.isoformat() if document.date else None,
            chunk_texts=chunk_texts,
            chunk_ids=chunk_ids,
            extracted=ExtractionResult(),  # MENTIONS edges added in task ⑥ post-LLM
            reference_uris=_reference_uris(document),
        )
        if chunk_texts
        else ExtractionResult()
    )

    reference_uris = _reference_uris(document)
    chunked = ChunkedDocument(
        document_id=str(document.id),
        source_uri=document.source_uri,
        source_type=document.source_type.value,
        date=document.date.isoformat() if document.date else None,
        reference_uris=reference_uris,
        chunk_texts=chunk_texts,
        chunk_ids=chunk_ids,
        structural=structural,
    )
    log.info(
        "extract_chunks_and_structural: doc_id=%s n_chunks=%d n_structural_entries=%d",
        chunked.document_id,
        len(chunked.chunk_texts),
        len(structural.nodes) + len(structural.edges),
    )
    return chunked


def _reference_uris(document: Document) -> list[str]:
    """Resolve already-populated ``Document.references`` to their source URIs."""

    return [ref.source_uri for ref in document.references if isinstance(ref, Document)]


extract_chunks_and_structural_task = task(
    _extract_chunks_and_structural,
    name="extract-chunks-and-structural",
    cache_policy=INPUTS,
    cache_expiration=timedelta(days=30),
    retries=1,
)


# ---------------------------------------------------------------------------
# Task ② — LLM extraction
# ---------------------------------------------------------------------------


async def _llm_extract_entities(
    chunked: ChunkedDocument, llm: BaseLLM | None = None
) -> RawExtraction:
    """Invoke the LLM per chunk and merge per-chunk extractions.

    ``llm`` is optional so the MCP ingest path can inject a caller-owned
    handle (the FastMCP lifespan already constructed one). When omitted the
    flow uses the default ``get_llm()`` factory.
    """

    log = _get_run_logger()
    if not chunked.chunk_texts:
        return RawExtraction(
            document_id=chunked.document_id,
            source_uri=chunked.source_uri,
            chunked=chunked,
            extracted=ExtractionResult(),
        )

    if llm is None:
        llm = get_llm()
    semaphore = asyncio.Semaphore(_live_app_config().extraction.llm_concurrency)

    async def _one(chunk: str, chunk_id: str) -> ExtractionResult:
        async with semaphore:
            return await extract_entities(llm, chunk, chunk_id=chunk_id)

    per_chunk = await asyncio.gather(
        *[_one(t, cid) for t, cid in zip(chunked.chunk_texts, chunked.chunk_ids)]
    )

    merged = ExtractionResult()
    for piece in per_chunk:
        merged = merged.merge(piece)

    log.info(
        "llm_extract_entities: doc_id=%s n_entities_raw=%d n_edges_raw=%d",
        chunked.document_id,
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
    cache_policy=INPUTS,
    cache_expiration=timedelta(days=30),
    retries=2,
    retry_delay_seconds=15,
)


# ---------------------------------------------------------------------------
# Task ②.5 — envelope + field validation (NEW in #030)
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


async def _write_envelope_rejection(
    *,
    database: Any,
    user_id: PydanticObjectId,
    document_id: PydanticObjectId | None,
    chunk_id: str | None,
    raw_row: dict[str, Any],
    reason: str,
    extractor: ExtractorInfo,
) -> None:
    """Insert one ``extraction_rejections`` audit row.

    Uses the raw Mongo collection (not Beanie's ``.insert()``) so the
    write is scoped to the same ``database`` handle the rest of the
    pipeline uses — keeps test patches uniform.
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
    await database["extraction_rejections"].insert_one(
        rejection.model_dump(by_alias=True, exclude={"id"})
    )


async def _write_dropped_field(
    *,
    database: Any,
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
) -> None:
    """Insert one ``extraction_dropped_fields`` audit row."""

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
    await database["extraction_dropped_fields"].insert_one(
        dropped.model_dump(by_alias=True, exclude={"id"})
    )


async def _validate_raws(
    *,
    raws: list[RawExtraction],
    database: Any,
    user_id: PydanticObjectId,
    extractor: ExtractorInfo,
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
            await _write_envelope_rejection(
                database=database,
                user_id=user_id,
                document_id=document_id,
                chunk_id=rejection.chunk_id or None,
                raw_row=rejection.raw,
                reason=rejection.reason,
                extractor=extractor,
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
                await _write_envelope_rejection(
                    database=database,
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
                await _write_dropped_field(
                    database=database,
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
                await _write_envelope_rejection(
                    database=database,
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
                await _write_dropped_field(
                    database=database,
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
            edge.properties = validated_props
            validated_edges.append(edge)

        raw.extracted.nodes = validated_nodes
        raw.extracted.edges = validated_edges

    return raws


validate_raws_task = task(
    _validate_raws,
    name="validate-raws",
    cache_policy=NO_CACHE,
    retries=1,
)


# ---------------------------------------------------------------------------
# Task ③ — resolve entities (batched)
# ---------------------------------------------------------------------------


async def _resolve_entities(
    raws: list[RawExtraction],
    database: Any,
    resolver: CompositeResolver,
    user_id: PydanticObjectId,
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

    # Collect all entities to resolve (LLM-extractable types only).
    entities: list[tuple[str, NodeType]] = []
    entities_by_doc: dict[str, list[tuple[NodeType, str]]] = {}
    # #042: keep the originating node (with its properties) per entity key
    # so we can build the per-entity embeddable text once the resolver has
    # chosen a canonical_name.
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

    # Group entities by type for type-strict resolution.
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
        collection = database[_KG_COLLECTION]
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

        # Resolve every entity of this type against the per-type candidate set.
        existing_entities = {etype: sorted(candidate_names)}
        results = await resolver.resolve_with_types(
            entity_pairs,
            existing_entities=existing_entities,
            existing_aliases=alias_map,
        )
        for (name, _t), resolved in zip(entity_pairs, results):
            # Find the originating doc id for this (type, name).
            for doc_id, doc_entities in entities_by_doc.items():
                if (etype, name) in doc_entities:
                    key = make_entity_key(doc_id, etype, name)
                    resolved_by_key[key] = resolved
                    # #042: precompute the embeddable text now that the
                    # resolver has picked a canonical_name. Generic types →
                    # node-text; PREFERENCE/FACT → statement/object.
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
# Task ④ — embed (ALL run node-texts in one batched call, #044)
# ---------------------------------------------------------------------------


async def _embed_entities(texts: list[str]) -> dict[str, list[float]]:
    """Embed every embeddable text for the run via the #044 batcher.

    Pre-#044 this task was ``.map()``'d at single-text grain, issuing ONE
    Voyage request per unique canonical/node-text — the dominant source of
    the operator's free-tier ``429 rate-limit retries exhausted``. #044
    replaces that with a SINGLE batched call: all the run's node-texts are
    packed into as few synchronous ``/v1/multimodalembeddings`` requests as
    the per-request caps (1000 inputs / 320K tokens) allow via
    :func:`tree.memory.embedding_text.embed_in_batches`. The vectors come
    back positionally aligned, so we zip them back to their texts and return
    a ``text -> vector`` map that task ⑤/⑥ index by embeddable text exactly
    as before.

    Tradeoff vs. #042: the prior per-text ``INPUTS`` cache (one cache key per
    node-text, surviving across runs) is given up in exchange for far fewer
    requests — the win the operator asked for. The task still caches on
    ``INPUTS`` (the whole text list), so an identical re-run of the same
    document set is still a cache hit; only partial-overlap re-runs lose the
    finer-grained reuse. Per-run dedup of identical texts still happens
    upstream (the flow embeds ``sorted(set(...))``).

    The embedding model is the **search** model
    (``get_search_embedding_model``) — the persisted, index-coupled vector.
    The 429 backoff is untouched: it lives inside ``.embed()`` and the
    batcher calls ``.embed()`` once per chunk.
    """

    log = _get_run_logger()
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
    cache_policy=INPUTS,
    cache_expiration=timedelta(days=90),
    retries=2,
)


# ---------------------------------------------------------------------------
# Task ⑤ — dedup (batched)
# ---------------------------------------------------------------------------


async def _dedupe_entities(
    resolved: ResolutionOutput,
    embeddings: EmbeddingMap,
    database: Any,
    dedup_config: DeduplicationConfig,
    user_id: PydanticObjectId,
) -> DedupMap:
    """For each resolved entity, run :func:`dedupe_entity` and bucket the
    decision under a stable per-entity key."""

    log = _get_run_logger()
    decisions: dict[str, DedupDecision] = {}
    n_merged = n_flagged = n_none = 0

    for key, resolved_entity in resolved.resolved_by_key.items():
        doc_id, type_value, name = key.split("|", maxsplit=2)
        entity_type = NodeType(type_value)

        # #042: dedup against the node-text vector (the same vector that
        # will be persisted on a non-merged node), keyed by the embeddable
        # text task ④ embedded. Falls back to the canonical-name key only
        # for legacy callers that did not populate ``embeddable_text_by_key``.
        embeddable_text = resolved.embeddable_text_by_key.get(
            key, resolved_entity.canonical_name
        )
        embedding = embeddings.vectors.get(embeddable_text) or []

        if not embedding or not dedup_config.enabled:
            decisions[key] = DedupDecision(action="none")
            n_none += 1
            continue

        prospective_id = build_node_id(user_id, entity_type, _normalize(name))
        raw = await dedupe_entity(
            database=database,
            user_id=user_id,
            name=name,
            entity_type=entity_type,
            embedding=embedding,
            config=dedup_config,
            incoming_node_id=prospective_id,
        )
        decision = _to_decision(raw, prospective_id)
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
# Task ⑥ — apply writes (batched)
# ---------------------------------------------------------------------------


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

    # ----- Structural nodes/edges (DOCUMENT, CHUNK, PART_OF, NEXT, REFERENCED) -----
    # Built per-doc in task ①; upserted directly here. MENTIONS edges are
    # built locally from the LLM-extracted PERSON nodes so they can be remapped
    # to the final target ids.
    structural_node_ids: set[str] = set()
    structural_edges: list[ExtractedEdge] = []
    for raw in raws:
        for node in raw.chunked.structural.nodes:
            node_id = build_node_id(user_id, node.type, node.name)
            await _upsert_structural_node(
                database=database,
                user_id=user_id,
                node=node,
                node_id=node_id,
                source_document_id=raw.document_id,
            )
            structural_node_ids.add(node_id)
            summary.nodes_written += 1
            # Document and chunk nodes carry stable names — register so
            # MENTIONS edges (built later) can find them.
            name_to_target_id[make_type_name_key(node.type, node.name)] = node_id
        structural_edges.extend(raw.chunked.structural.edges)

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
        # Source/target endpoints may need remapping.
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

    # Upsert each collapsed edge.
    for edge_id, edge in seen_edge_ids.items():
        # Only stamp ``extractor`` on LLM-extractable edges (today only
        # ``related_to``). Structural edges (``part_of``, ``next``,
        # ``mentions``, ``referenced``, ``has``, ``same_as``) skip the
        # column per ``plan.md:210``.
        edge_type_value = (
            edge.type.value if hasattr(edge.type, "value") else str(edge.type)
        )
        edge_extractor = extractor if edge_type_value == "related_to" else None
        await _upsert_edge(
            database=database,
            user_id=user_id,
            edge=edge,
            edge_id=edge_id,
            source_document_ids=[PydanticObjectId(raw.document_id) for raw in raws],
            extractor=edge_extractor,
        )
        summary.edges_written += 1

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
    # #042: task ④ already embedded this entity's embeddable text — the
    # GENERIC node-text for most types, or ``properties.statement`` /
    # ``properties.object`` for PREFERENCE / FACT (the #032 supersession
    # contract). We look that vector up by the same embeddable-text key and
    # wrap it in ``_CachedSingleEmbedding`` so ``add_entity``'s internal
    # ``embedding_model.embed([...])`` returns it WITHOUT a second embed
    # call. ``add_entity`` rebuilds the identical text via its own
    # ``_embeddable_text`` and persists this same vector on the non-merged
    # path — dedup vector == persisted vector, computed once.
    #
    # The on-the-fly statement embed that used to live here is gone: the
    # statement/object text is now part of the task-④ embed batch (keyed in
    # ``embeddable_text_by_key``), so PREFERENCE/FACT no longer pay for a
    # duplicate embedding inside apply-writes.
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


async def _upsert_structural_node(
    *,
    database: Any,
    user_id: PydanticObjectId,
    node: ExtractedNode,
    node_id: str,
    source_document_id: str,
) -> None:
    """Upsert a structural node (DOCUMENT or CHUNK). No resolution, no dedup."""

    from datetime import UTC, datetime

    collection = database[_KG_COLLECTION]
    now = datetime.now(tz=UTC)
    props = node.properties.copy()
    await collection.update_one(
        {"_id": node_id},
        [
            {
                "$set": {
                    "user_id": user_id,
                    "kind": "node",
                    "type": node.type.value,
                    "name": node.name,
                    "canonical_name": {"$ifNull": ["$canonical_name", node.name]},
                    "properties": {
                        "$mergeObjects": [
                            {"$ifNull": ["$properties", {}]},
                            props,
                        ]
                    },
                    "aliases": {"$ifNull": ["$aliases", []]},
                    "confidence": {"$ifNull": ["$confidence", 1.0]},
                    "embedding": {"$ifNull": ["$embedding", []]},
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


async def _upsert_edge(
    *,
    database: Any,
    user_id: PydanticObjectId,
    edge: ExtractedEdge,
    edge_id: str,
    source_document_ids: list[PydanticObjectId],
    extractor: ExtractorInfo | None = None,
) -> None:
    """Upsert one collapsed edge document.

    #030: ``extractor`` is stamped on LLM-extracted edges (today only
    ``related_to`` rows). Structural edges pass ``extractor=None`` and
    leave the column unset on the row.
    """

    from datetime import UTC, datetime

    collection = database[_KG_COLLECTION]
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
    await collection.update_one(
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
# Flow
# ---------------------------------------------------------------------------


@flow(name="memory-extraction-etl", log_prints=True)
async def memory_extraction(
    user_id: PydanticObjectId,
    document_ids: list[str] | None = None,
) -> WriteSummary:
    """Extract knowledge-graph entries from documents for ``user_id``.

    ``user_id`` is a required, non-Optional Prefect parameter. Every task
    in the six-task pipeline receives the value; candidate fetches and
    writes are scoped to ``user_id`` end-to-end. Cross-tenant rows are
    invisible to resolution / dedup.

    Cross-key config validation runs at import time (``settings = Settings()``
    in ``app_config.py``). If the validator raises, the flow never starts;
    callers see the ``ValueError`` straight from the first attribute access on
    ``app_config.extraction``.
    """

    log = _get_run_logger()

    # Validate config invariants up-front (also re-checked on every dedup call).
    # We re-load so env-var changes since import-time are honored — gives tests
    # a deterministic seam for the "misconfig fails at flow entry" AC.
    try:
        load_app_config()
    except Exception as exc:
        # Surface the underlying ValueError directly (Pydantic wraps it in
        # ValidationError; unwrap so callers can match on ``ValueError``).
        unwrapped = _unwrap_validation_error(exc)
        raise unwrapped from exc

    dedup_config = _build_dedup_config()
    # #043: two distinct embedding handles now coexist in the flow.
    #   * ``resolution_embedding_model`` — feeds the resolver's semantic stage,
    #     which embeds the entity NAME only. Transient: the per-instance bounded
    #     LRU in ``SemanticMatchResolver`` discards it and no write path persists
    #     it. Not dimension-coupled to the live ``vector_index`` (#039).
    #   * ``search_embedding_model`` — the persisted, index-coupled vector. Feeds
    #     dedup, supersession (statement-vs-persisted-statement), and the node
    #     ``embedding`` written by apply-writes (#042).
    resolution_embedding_model = get_resolution_embedding_model()
    search_embedding_model = get_search_embedding_model()
    resolver = _build_resolver(resolution_embedding_model)

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
        "Processing %d documents for KG extraction (user_id=%s)", len(docs), user_id
    )

    if not docs:
        return WriteSummary(documents_processed=0)

    # ----- Load the active user (for the first-person resolver) -------------
    user = await User.get(user_id)
    if user is None:
        # Defensive — the upstream Prefect parameter validation guarantees
        # a non-None user_id, but a stale id is possible.
        raise ValueError(f"User {user_id} not found; cannot run extraction.")

    # ----- Tasks ① and ② — per-doc fan-out ---------------------------------
    chunked_docs: list[ChunkedDocument] = []
    for doc in docs:
        chunked_docs.append(await extract_chunks_and_structural_task(doc))

    raws: list[RawExtraction] = []
    for chunked in chunked_docs:
        raws.append(await llm_extract_entities_task(chunked))

    # ----- Task ②.5 — envelope + field validation (#030) -------------------
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

    # ----- #032 — supersession resolver branch (pre-dedup) -----------------
    # Runs BEFORE the standard dedup so contradiction trumps dedup
    # (per `plan.md:534`). Mutates `raws` in place: when a
    # preference / fact row supersedes a prior one we write the
    # supersession to MongoDB and mark the in-memory node so the
    # apply-writes step preserves ``valid_from``.
    judge_llm = get_llm()
    await resolve_supersessions(
        database=database,
        user_id=user_id,
        llm=judge_llm,
        # #043: supersession embeds the new statement and compares it against
        # PERSISTED preference (statement) vectors — same space → SEARCH model,
        # NOT the resolution model.
        embedding_model=search_embedding_model,
        raws=raws,
    )

    # ----- #032 — deterministic ``has: person:self -> preference`` -----
    # The LLM is told NOT to emit ``has`` (it is
    # ``llm_extractable=False``). The pipeline owns the edge.
    await write_self_has_preference_edges(database=database, user_id=user_id, raws=raws)

    # ----- Task ③ — resolve ------------------------------------------------
    resolved = await resolve_entities_task(raws, database, resolver, user_id)

    # ----- Task ④ — embed (ALL run node-texts in one batched call, #044) ---
    # #044: replace the per-text ``.map()`` (one Voyage request per node-text,
    # the free-tier 429 hotspot) with a SINGLE batched embed of every unique
    # node-text for the run. ``embed_entities_task`` packs them into as few
    # synchronous requests as the 1000-input / 320K-token caps allow.
    embeddable_texts = sorted(set(resolved.embeddable_text_by_key.values()))
    vectors = await embed_entities_task(embeddable_texts)
    embeddings = EmbeddingMap(vectors=vectors)

    # ----- Task ⑤ — dedup --------------------------------------------------
    dedup_results = await dedupe_entities_task(
        resolved, embeddings, database, dedup_config, user_id
    )

    # ----- Task ⑥ — apply writes ------------------------------------------
    # #043: apply-writes persists the node ``embedding`` → SEARCH model. (The
    # task-④ vector reuse from #042 already pins the persisted vector to the
    # search model; this handle is the fallback ``embedding_model`` for any
    # node that lacks a pre-computed vector.)
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
    )

    log.info(
        "memory_extraction complete: documents=%d nodes_written=%d edges_written=%d "
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
# Backwards-compat shim for the MCP ingestion path
# ---------------------------------------------------------------------------


async def run_extraction_for_documents(
    document_ids: list[str],
    *,
    user_id: PydanticObjectId,
    client: Any,
    database_name: str,
    llm: BaseLLM | None = None,
    embedding_model: BaseEmbeddingModel | None = None,
) -> WriteSummary:
    """Convenience entry point for callers that already hold an open client.

    Mirrors :func:`memory_extraction` but reuses caller-owned handles
    instead of opening a new one (the MCP ingest path holds its own client
    from the FastMCP lifespan). The flow itself is short-circuited — we
    re-implement the same pipeline shape inline so we don't have to start a
    Prefect flow run from inside the MCP server process.

    ``user_id`` is required and threaded through every step exactly as the
    Prefect flow does.

    Returns:
        A :class:`WriteSummary` for the processed documents.
    """

    # Re-validate config invariants.
    try:
        load_app_config()
    except Exception as exc:
        raise _unwrap_validation_error(exc) from exc
    dedup_config = _build_dedup_config()
    # #043: split the embedding handles exactly like ``memory_extraction``.
    #   * The injected ``embedding_model`` (the MCP lifespan's caller-owned
    #     handle) is the SEARCH / persisted model — it drives supersession, the
    #     node-text embed, and the apply-writes node ``embedding``. Falls back
    #     to ``get_search_embedding_model()`` when the caller injects nothing.
    #   * The resolver ALWAYS builds from ``get_resolution_embedding_model()``:
    #     its semantic stage embeds the entity NAME only and the vector is
    #     transient (never persisted). The injected handle never reaches the
    #     resolver.
    search_embedding_model = embedding_model or get_search_embedding_model()
    resolver = _build_resolver(get_resolution_embedding_model())
    database = client[database_name]

    docs = await Document.find(
        {
            "_id": {"$in": [PydanticObjectId(did) for did in document_ids]},
            "user_id": user_id,
        }
    ).to_list()

    if not docs:
        return WriteSummary(documents_processed=0)

    user = await User.get(user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found; cannot run extraction.")

    chunked = [await _extract_chunks_and_structural(d) for d in docs]
    raws = [await _llm_extract_entities(c, llm=llm) for c in chunked]
    extractor = _make_extractor_info()
    await _validate_raws(
        raws=raws, database=database, user_id=user_id, extractor=extractor
    )
    for raw in raws:
        raw.extracted.nodes = redirect_first_person(raw.extracted.nodes, user)
    # #032 - canonicalize preference names, then supersession resolver
    # branch, then deterministic ``has`` edges.
    canonicalize_preference_names(raws)
    judge_llm = llm if llm is not None else get_llm()
    await resolve_supersessions(
        database=database,
        user_id=user_id,
        llm=judge_llm,
        # #043: statement-vs-persisted-statement → SEARCH model.
        embedding_model=search_embedding_model,
        raws=raws,
    )
    await write_self_has_preference_edges(database=database, user_id=user_id, raws=raws)
    resolved = await _resolve_entities(raws, database, resolver, user_id)
    # #042: embed at node-text grain (see ``memory_extraction`` task ④).
    # The MCP ingest path injects a caller-owned SEARCH/persisted handle (the
    # FastMCP lifespan holds its own); use it directly here rather than the
    # ``get_search_embedding_model`` factory so the injected model actually
    # drives the embed step — task ④ in the Prefect flow uses the factory
    # because it has no injected handle. (The resolution model is NOT used
    # here: the node-text vector is persisted, so it must be the search space.)
    embeddable_texts = sorted(set(resolved.embeddable_text_by_key.values()))
    vectors: dict[str, list[float]] = {}
    if embeddable_texts:
        # #044: route through the batcher so a large document set is packed
        # into request-sized chunks (1000 inputs / 320K tokens) rather than a
        # single call that would 400 on the per-request cap.
        embedded = await embed_in_batches(embeddable_texts, search_embedding_model)
        vectors = dict(zip(embeddable_texts, embedded))
    embeddings = EmbeddingMap(vectors=vectors)
    dedup_results = await _dedupe_entities(
        resolved, embeddings, database, dedup_config, user_id
    )
    return await _apply_writes(
        raws,
        resolved,
        embeddings,
        dedup_results,
        database,
        resolver,
        dedup_config,
        # #043: apply-writes persists node vectors → SEARCH/persisted model.
        search_embedding_model,
        user_id,
        extractor,
    )
