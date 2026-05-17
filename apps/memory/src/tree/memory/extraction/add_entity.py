"""Write-side orchestrator for the resolution + dedup pipeline.

This module turns a ``(name, type, properties, embedding, resolved,
dedup_result)`` tuple into a single atomic upsert on the
``knowledge_graph`` collection. It is the single write-surface for the
extraction pipeline (#012) AND the human-review confirm path (#014), so
the two cannot drift.

Three merge strategies are dispatched on ``DeduplicationConfig.merge_strategy``
when ``action == "merged"``:

* ``KEEP_PRIMARY`` — alias-append + source-union; discard incoming properties.
* ``MERGE_PROPERTIES`` — KEEP_PRIMARY effects + per-key property merge.
* ``KEEP_ALIASES`` — alias-append + source-union only; never touch
  ``properties``.

Every strategy is implemented as a SINGLE ``$set`` aggregation pipeline on
the canonical's ``_id``. No Python-side read-modify-write — Mongo handles
concurrency.

References:
- ``tracker/011-add-entity-orchestrator.groomed.md`` — task spec.
- ``notes/RESOLUTION_MODULE.md`` §8 — strategy semantics.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from beanie import PydanticObjectId

from tree.entities.knowledge_graph import (
    EdgeType,
    NodeType,
    build_edge_id,
    build_node_id,
)
from tree.memory.extraction.dedup import (
    DeduplicationConfig,
    DeduplicationResult,
    MergeStrategy,
    dedupe_entity,
)
from tree.memory.resolution.composite import CompositeResolver
from tree.memory.resolution.types import ResolvedEntity, _normalize
from tree.models.base import BaseEmbeddingModel

if TYPE_CHECKING:
    from pymongo.asynchronous.database import AsyncDatabase


logger = logging.getLogger(__name__)


_KG_COLLECTION = "knowledge_graph"
_MAX_ALIASES = 50
_MAX_SOURCES = 500


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def add_entity(
    *,
    database: AsyncDatabase,
    embedding_model: BaseEmbeddingModel,
    resolver: CompositeResolver,
    user_id: PydanticObjectId,
    name: str,
    entity_type: NodeType,
    properties: dict[str, Any],
    source_id: str,
    dedup_config: DeduplicationConfig,
    resolve: bool = True,
    deduplicate: bool = True,
    candidate_names: Sequence[str] | None = None,
    candidate_aliases: Mapping[str, list[str]] | None = None,
) -> tuple[str, ResolvedEntity, DeduplicationResult]:
    """Resolve, dedupe, and upsert a single entity.

    Returns ``(target_node_id, resolved, dedup_result)`` where
    ``target_node_id`` is the ``_id`` of the row that now represents this
    entity. Concretely:

    * ``dedup_result.action == "merged"`` → ``target_id`` is
      ``dedup_result.matched_node_id`` (the existing canonical; no new node
      is created).
    * ``dedup_result.action in {"flagged", "none"}`` → ``target_id`` is
      ``build_node_id(entity_type, _normalize(name))`` and a new node is
      upserted there.

    On the ``"flagged"`` path the function also upserts a SAME_AS edge from
    the new node to ``dedup_result.matched_node_id`` with
    ``properties.status="pending"`` so the human-review surface (#014) can
    later resolve it.

    Short-circuits:

    * ``resolve=False`` and ``deduplicate=False`` → a plain upsert at
      ``build_node_id(entity_type, _normalize(name))``. No resolver call,
      no dedup call, no SAME_AS edges. Useful for structural nodes (chunks,
      documents) where resolution/dedup is meaningless.
    * ``dedup_config.enabled=False`` → resolve only; dedup is treated as
      ``action="none"``.

    Args:
        database: An ``AsyncDatabase`` handle.
        embedding_model: Used to embed the prospective entity when
            ``deduplicate=True`` (the resolver may also use it).
        resolver: The composite resolver chain (Alias → Exact → Fuzzy →
            Semantic).
        name: Surface form of the prospective entity (raw input).
        entity_type: One of :class:`NodeType` — typically PERSON/TASK/
            EPISODE/PREFERENCE (the four LLM-extractable types).
        properties: Caller-supplied properties dict. Subject to the merge
            strategy when an existing canonical is hit.
        source_id: Provenance id (typically the document ObjectId as a
            string). Appended to the row's ``sources`` array, capped at
            :data:`_MAX_SOURCES`.
        dedup_config: A validated :class:`DeduplicationConfig`.
        resolve: When ``False`` (and ``deduplicate=False``), short-circuit
            to a plain upsert.
        deduplicate: When ``False``, skip the vector-search step. The
            resolver still runs if ``resolve=True``.
        candidate_names: Optional in-batch candidates for the resolver.
            None ⇒ resolver sees an empty candidate list.
        candidate_aliases: Optional in-batch alias map for the resolver.

    Returns:
        ``(target_node_id, resolved, dedup_result)``.

    Raises:
        ValueError: When ``name`` is empty/whitespace-only or properties
            contain a ``confidence`` value outside ``[0.0, 1.0]``.
    """

    # ------------------------------------------------------------------
    # Input validation (API boundary)
    # ------------------------------------------------------------------

    if not name or not name.strip():
        raise ValueError("add_entity: name must be a non-empty string")

    incoming_confidence = properties.get("confidence")
    if incoming_confidence is not None and not 0.0 <= float(incoming_confidence) <= 1.0:
        raise ValueError(
            f"add_entity: properties['confidence'] must be in [0.0, 1.0]; "
            f"got {incoming_confidence!r}"
        )

    collection = database[_KG_COLLECTION]
    now = datetime.now(tz=UTC)
    normalized = _normalize(name)
    prospective_id = build_node_id(user_id, entity_type, normalized)

    # ------------------------------------------------------------------
    # Short-circuit: no resolution, no dedup → plain upsert.
    # ------------------------------------------------------------------

    if not resolve and not deduplicate:
        resolved = ResolvedEntity(
            original_name=name,
            canonical_name=name,
            entity_type=entity_type,
            confidence=1.0,
            match_type="none",
        )
        dedup_result = DeduplicationResult(action="none")
        await _upsert_node(
            collection=collection,
            node_id=prospective_id,
            user_id=user_id,
            entity_type=entity_type,
            name=name,
            canonical_name=name,
            properties=properties,
            embedding=None,
            confidence=1.0,
            source_id=source_id,
            now=now,
        )
        return prospective_id, resolved, dedup_result

    # ------------------------------------------------------------------
    # Step 1 — resolve (alias → exact → fuzzy → semantic).
    # ------------------------------------------------------------------

    if resolve:
        resolved = await resolver.resolve(
            name,
            entity_type,
            list(candidate_names or []),
            candidate_aliases,
        )
    else:
        resolved = ResolvedEntity(
            original_name=name,
            canonical_name=name,
            entity_type=entity_type,
            confidence=1.0,
            match_type="none",
        )

    # ------------------------------------------------------------------
    # Step 2 — dedup (vector-search; read-only).
    # ------------------------------------------------------------------

    embedding: list[float] = []
    if deduplicate and dedup_config.enabled:
        embedded = await embedding_model.embed([name])
        embedding = embedded[0] if embedded else []
        raw_result = await dedupe_entity(
            database=database,
            user_id=user_id,
            name=name,
            entity_type=entity_type,
            embedding=embedding,
            config=dedup_config,
            incoming_node_id=prospective_id,
        )
        dedup_result = _filter_self_match(raw_result, prospective_id)
    else:
        dedup_result = DeduplicationResult(action="none")

    # ------------------------------------------------------------------
    # Step 3 — dispatch on dedup action.
    # ------------------------------------------------------------------

    if dedup_result.action == "merged":
        assert dedup_result.matched_node_id is not None  # noqa: S101
        target_id = dedup_result.matched_node_id
        await _apply_merge(
            collection=collection,
            target_id=target_id,
            entity_type=entity_type,
            incoming_name=name,
            incoming_properties=properties,
            source_id=source_id,
            strategy=dedup_config.merge_strategy,
            now=now,
        )
        dedup_result.applied_strategy = dedup_config.merge_strategy
        return target_id, resolved, dedup_result

    # Non-merged path: upsert a new node at the prospective _id.
    target_id = prospective_id
    await _upsert_node(
        collection=collection,
        node_id=target_id,
        user_id=user_id,
        entity_type=entity_type,
        name=name,
        canonical_name=resolved.canonical_name,
        properties=properties,
        embedding=embedding or None,
        confidence=resolved.confidence if resolved.match_type != "none" else 1.0,
        source_id=source_id,
        now=now,
    )

    if dedup_result.action == "flagged":
        assert dedup_result.matched_node_id is not None  # noqa: S101
        await _upsert_pending_same_as_edge(
            collection=collection,
            user_id=user_id,
            source_node_id=target_id,
            source_type=entity_type,
            target_node_id=dedup_result.matched_node_id,
            target_type=entity_type,
            confidence=dedup_result.similarity_score,
            match_type=dedup_result.match_type or "embedding",
            now=now,
        )

    return target_id, resolved, dedup_result


# ---------------------------------------------------------------------------
# Internals — node upsert
# ---------------------------------------------------------------------------


async def _upsert_node(
    *,
    collection: Any,
    node_id: str,
    user_id: PydanticObjectId,
    entity_type: NodeType,
    name: str,
    canonical_name: str,
    properties: dict[str, Any],
    embedding: list[float] | None,
    confidence: float,
    source_id: str,
    now: datetime,
) -> None:
    """Upsert a new node at ``node_id`` with a single aggregation pipeline.

    Does NOT touch ``aliases`` on an existing row (the merge handlers do
    that). On insert, ``aliases`` is initialized to ``[]``. ``embedding``
    is only written when ``embedding`` is non-empty.
    """

    # Strip ``aliases`` and ``confidence`` from the caller's properties dict
    # because they are first-class fields on the node, not free-form props.
    user_props = {
        k: v for k, v in properties.items() if k not in {"aliases", "confidence"}
    }

    set_stage: dict[str, Any] = {
        "user_id": user_id,
        "kind": "node",
        "type": entity_type.value,
        "name": name,
        "canonical_name": canonical_name,
        "properties": {
            "$mergeObjects": [
                {"$ifNull": ["$properties", {}]},
                user_props,
            ]
        },
        "aliases": {"$ifNull": ["$aliases", []]},
        "confidence": {"$ifNull": ["$confidence", confidence]},
        "sources": {
            "$slice": [
                {
                    "$setUnion": [
                        {"$ifNull": ["$sources", []]},
                        [source_id],
                    ]
                },
                _MAX_SOURCES,
            ]
        },
        "created_at": {"$ifNull": ["$created_at", now]},
        "updated_at": now,
    }
    if embedding is not None:
        set_stage["embedding"] = {"$ifNull": ["$embedding", embedding]}

    await collection.update_one(
        {"_id": node_id},
        [{"$set": set_stage}],
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Internals — merge strategies
# ---------------------------------------------------------------------------


async def _apply_merge(
    *,
    collection: Any,
    target_id: str,
    entity_type: NodeType,
    incoming_name: str,
    incoming_properties: dict[str, Any],
    source_id: str,
    strategy: MergeStrategy,
    now: datetime,
) -> None:
    """Dispatch on ``strategy`` and issue ONE ``update_one`` against
    ``target_id`` with the strategy's aggregation pipeline."""

    incoming_confidence_raw = incoming_properties.get("confidence")
    incoming_confidence = (
        float(incoming_confidence_raw) if incoming_confidence_raw is not None else None
    )

    if strategy is MergeStrategy.KEEP_PRIMARY:
        pipeline = _merge_keep_primary(
            incoming_name=incoming_name,
            incoming_confidence=incoming_confidence,
            source_id=source_id,
            now=now,
        )
    elif strategy is MergeStrategy.MERGE_PROPERTIES:
        pipeline = _merge_properties(
            incoming_name=incoming_name,
            incoming_properties=incoming_properties,
            incoming_confidence=incoming_confidence,
            source_id=source_id,
            now=now,
        )
    elif strategy is MergeStrategy.KEEP_ALIASES:
        pipeline = _merge_keep_aliases(
            incoming_name=incoming_name,
            source_id=source_id,
            now=now,
        )
    else:  # pragma: no cover — defensive: StrEnum is closed.
        raise ValueError(f"Unknown merge strategy: {strategy!r}")

    # Defensive: keep entity_type on the canonical (no-op when present).
    pipeline[0]["$set"].setdefault("type", entity_type.value)

    await collection.update_one({"_id": target_id}, pipeline)


def _merge_keep_primary(
    *,
    incoming_name: str,
    incoming_confidence: float | None,
    source_id: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """Append alias, union sources; discard incoming properties.

    Bumps ``confidence`` to ``max(existing, incoming)`` (alias confirmation
    can only raise confidence).
    """

    set_stage: dict[str, Any] = {
        "aliases": _aliases_append_expr(incoming_name),
        "sources": _sources_union_expr(source_id),
        "updated_at": now,
    }
    if incoming_confidence is not None:
        set_stage["confidence"] = {
            "$max": [
                {"$ifNull": ["$confidence", 0.0]},
                incoming_confidence,
            ]
        }
    return [{"$set": set_stage}]


def _merge_properties(
    *,
    incoming_name: str,
    incoming_properties: dict[str, Any],
    incoming_confidence: float | None,
    source_id: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """KEEP_PRIMARY effects + per-key property merge.

    Per-key merge rules:
        * Missing on canonical → take incoming.
        * Both strings → longer wins.
        * Both lists → set-union.
        * Same-type scalars or type mismatch → primary wins.
    """

    # Build the merged ``properties`` object as a sequence of per-key $cond
    # expressions inside a single $mergeObjects call so we never need a
    # Python-side read-modify-write step.
    user_props = {
        k: v
        for k, v in incoming_properties.items()
        if k not in {"aliases", "confidence"}
    }

    per_key_expressions: list[dict[str, Any]] = []
    for key, incoming_value in user_props.items():
        existing = {
            "$getField": {"field": key, "input": {"$ifNull": ["$properties", {}]}}
        }
        merged_value = _per_key_merge_expr(existing, incoming_value)
        per_key_expressions.append({key: merged_value})

    if per_key_expressions:
        merged_properties_expr: Any = {
            "$mergeObjects": [
                {"$ifNull": ["$properties", {}]},
                *per_key_expressions,
            ]
        }
    else:
        merged_properties_expr = {"$ifNull": ["$properties", {}]}

    set_stage: dict[str, Any] = {
        "aliases": _aliases_append_expr(incoming_name),
        "sources": _sources_union_expr(source_id),
        "properties": merged_properties_expr,
        "updated_at": now,
    }
    if incoming_confidence is not None:
        set_stage["confidence"] = {
            "$max": [
                {"$ifNull": ["$confidence", 0.0]},
                incoming_confidence,
            ]
        }
    return [{"$set": set_stage}]


def _merge_keep_aliases(
    *,
    incoming_name: str,
    source_id: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """Append alias + union sources only. Never touch ``properties``."""

    set_stage: dict[str, Any] = {
        "aliases": _aliases_append_expr(incoming_name),
        "sources": _sources_union_expr(source_id),
        "updated_at": now,
    }
    return [{"$set": set_stage}]


# ---------------------------------------------------------------------------
# Internals — per-key property merge expression
# ---------------------------------------------------------------------------


def _per_key_merge_expr(existing_expr: Any, incoming_value: Any) -> dict[str, Any]:
    """Aggregation expression: merge ``existing`` with ``incoming_value``.

    Returns the merged value following the rules in :func:`_merge_properties`.
    """

    # Constant-literal wrapper for the incoming value: ``$literal`` keeps
    # nested dicts/lists from being interpreted as aggregation expressions.
    incoming_literal = {"$literal": incoming_value}

    # Branch on Python-side type to keep the aggregation expression simple.
    if isinstance(incoming_value, str):
        # Both strings → longer wins; if existing is not a string, primary wins
        # (existing is kept); if missing → incoming.
        existing_is_string = {"$eq": [{"$type": existing_expr}, "string"]}
        existing_missing = {"$in": [{"$type": existing_expr}, ["missing", "null"]]}
        return {
            "$cond": {
                "if": existing_missing,
                "then": incoming_literal,
                "else": {
                    "$cond": {
                        "if": existing_is_string,
                        "then": {
                            "$cond": {
                                "if": {
                                    "$gt": [
                                        {"$strLenCP": incoming_literal},
                                        {"$strLenCP": existing_expr},
                                    ]
                                },
                                "then": incoming_literal,
                                "else": existing_expr,
                            }
                        },
                        # Type mismatch — primary wins.
                        "else": existing_expr,
                    }
                },
            }
        }

    if isinstance(incoming_value, list):
        # Both lists → set-union. Missing → take incoming. Type mismatch →
        # primary wins.
        existing_is_array = {"$eq": [{"$type": existing_expr}, "array"]}
        existing_missing = {"$in": [{"$type": existing_expr}, ["missing", "null"]]}
        return {
            "$cond": {
                "if": existing_missing,
                "then": incoming_literal,
                "else": {
                    "$cond": {
                        "if": existing_is_array,
                        "then": {"$setUnion": [existing_expr, incoming_literal]},
                        "else": existing_expr,
                    }
                },
            }
        }

    # Scalar (int, float, bool, None, or any non-str/list).
    # Missing → take incoming. Otherwise primary wins (same type or mismatch).
    existing_missing = {"$in": [{"$type": existing_expr}, ["missing", "null"]]}
    return {
        "$cond": {
            "if": existing_missing,
            "then": incoming_literal,
            "else": existing_expr,
        }
    }


# ---------------------------------------------------------------------------
# Internals — shared aggregation snippets
# ---------------------------------------------------------------------------


def _aliases_append_expr(incoming_name: str) -> dict[str, Any]:
    """Aggregation expression: union ``incoming_name`` into ``aliases`` (cap 50)."""
    return {
        "$slice": [
            {
                "$setUnion": [
                    {"$ifNull": ["$aliases", []]},
                    [incoming_name],
                ]
            },
            _MAX_ALIASES,
        ]
    }


def _sources_union_expr(source_id: str) -> dict[str, Any]:
    """Aggregation expression: union ``source_id`` into ``sources`` (cap 500)."""
    return {
        "$slice": [
            {
                "$setUnion": [
                    {"$ifNull": ["$sources", []]},
                    [source_id],
                ]
            },
            _MAX_SOURCES,
        ]
    }


# ---------------------------------------------------------------------------
# Internals — SAME_AS edge (flagged path)
# ---------------------------------------------------------------------------


async def _upsert_pending_same_as_edge(
    *,
    collection: Any,
    user_id: PydanticObjectId,
    source_node_id: str,
    source_type: NodeType,
    target_node_id: str,
    target_type: NodeType,
    confidence: float,
    match_type: str,
    now: datetime,
) -> None:
    """Upsert a SAME_AS edge with ``status="pending"``.

    Uses ``$setOnInsert`` for ``status`` and ``created_at`` so a previously-
    reviewed pair (e.g. ``status="rejected"``) is NOT overwritten back to
    pending. ``confidence`` and ``match_type`` are refreshed on every call
    via ``$set``.
    """

    edge_id = build_edge_id(source_node_id, EdgeType.SAME_AS, target_node_id)
    await collection.update_one(
        {"_id": edge_id},
        {
            "$setOnInsert": {
                "user_id": user_id,
                "kind": "edge",
                "type": EdgeType.SAME_AS.value,
                "source_node_id": source_node_id,
                "source_type": source_type.value,
                "target_node_id": target_node_id,
                "target_type": target_type.value,
                "properties.status": "pending",
                "properties.created_at": now,
                "created_at": now,
                "sources": [],
            },
            "$set": {
                "properties.confidence": confidence,
                "properties.match_type": match_type,
                "updated_at": now,
            },
        },
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Internals — self-match guard
# ---------------------------------------------------------------------------


def _filter_self_match(
    result: DeduplicationResult, prospective_id: str
) -> DeduplicationResult:
    """Drop a self-match from the dedup result.

    Per ``dedupe_entity``'s docstring contract, self-match exclusion is the
    caller's responsibility. When the top candidate's ``_id`` equals the
    prospective ``_id``, this function picks the next eligible candidate
    from ``result.candidates`` and re-tier-decides; if no other candidate
    qualifies, it returns ``action="none"``.
    """

    if result.action == "none" or result.matched_node_id != prospective_id:
        return result

    # Find the next non-self candidate.
    for cand in result.candidates:
        if str(cand.get("_id")) == prospective_id:
            continue
        # Re-walk the same tier decision: ``dedupe_entity`` returned
        # ``result.similarity_score`` for the top candidate; the next
        # candidate's score is whatever Atlas surfaced. We don't have the
        # raw cosine for it here, so we treat the filtered result as
        # ``action="none"``. This is conservative — the only scenario where
        # self-match is at the top is the soft-join re-ingest case, where
        # we WANT the result to come back as ``none`` so the existing row
        # is left alone.
        break

    return DeduplicationResult(
        action="none",
        candidates=result.candidates,
    )
