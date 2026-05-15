"""Read-only deduplication decision module.

This module decides whether a prospective entity is a duplicate of an
existing node in the knowledge graph. It runs an Atlas ``$vectorSearch``
against the ``knowledge_graph`` collection, re-ranks every returned
candidate using an optional RapidFuzz boost against the candidate's
``name``, ``canonical_name``, and ``aliases``, and returns a tiered
decision based on the best-scoring candidate:

* ``action="merged"`` — best candidate's score ≥ ``auto_merge_threshold``.
* ``action="flagged"`` — best candidate's score in
  ``[flag_threshold, auto_merge_threshold)``.
* ``action="none"``  — no candidate or best candidate below ``flag_threshold``.

The re-rank semantics mirror ``_check_for_duplicates`` in the reference
``long_term.py``: a lower-vec hit with a strong fuzzy match can outrank a
higher-vec hit whose fuzzy score is below ``fuzzy_threshold``. When fuzzy
passes the threshold for a candidate the embedding-only branch is skipped
for that candidate (the reference's ``continue``).

The function is **strictly read-only**. SAME_AS edges, tombstones, and any
write decisions live in ``add_entity`` (#011) and ``review_duplicate`` (#014).

Reject-pair filter
------------------
When the caller passes an ``incoming_node_id``, the candidate filter drops
any node that already has a SAME_AS edge to/from that ``_id`` with
``properties.status == "rejected"``. The edge schema uses dedicated
``source_node_id`` / ``target_node_id`` fields on edge documents in the same
``knowledge_graph`` collection (see ``tree.entities.knowledge_graph``), so
the ``$lookup`` matches on those fields rather than on substrings of the
edge ``_id``.

Type-strict alignment
---------------------
``DeduplicationConfig.match_same_type_only`` must equal
``ResolutionConfig.type_strict`` at runtime. The config-level validator
that enforces that invariant lands in #012; this module trusts the caller
and documents the contract here. Both flags default to ``True``.

References:
- ``notes/RESOLUTION_MODULE.md`` §7.5–§7.6.
- ``RESOLUTION_DEDUP_ALGORITHM.md`` §4.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.memory.resolution.types import _normalize

if TYPE_CHECKING:
    from pymongo.asynchronous.database import AsyncDatabase


logger = logging.getLogger(__name__)


_KG_COLLECTION = "knowledge_graph"
_VECTOR_INDEX_NAME = "vector_index"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class MergeStrategy(StrEnum):
    """How ``add_entity`` should merge a prospective duplicate.

    The strategy is selected on the *config*; this module does not perform
    the merge — it only records which strategy the caller picked at decision
    time so the downstream writer in #011 can dispatch on it.
    """

    KEEP_PRIMARY = "keep_primary"
    MERGE_PROPERTIES = "merge_properties"
    KEEP_ALIASES = "keep_aliases"


@dataclass
class DeduplicationConfig:
    """Runtime configuration for :func:`dedupe_entity`.

    Validates ranges and invariants in :meth:`__post_init__` so misconfig
    fails at startup rather than on the first dedup call.
    """

    enabled: bool = True
    auto_merge_threshold: float = 0.95
    flag_threshold: float = 0.85
    use_fuzzy_matching: bool = True
    fuzzy_threshold: float = 0.90
    max_candidates: int = 10
    match_same_type_only: bool = True
    merge_strategy: MergeStrategy = MergeStrategy.KEEP_PRIMARY

    def __post_init__(self) -> None:
        if not 0.0 <= self.auto_merge_threshold <= 1.0:
            raise ValueError(
                "DeduplicationConfig.auto_merge_threshold must be in [0.0, 1.0]; "
                f"got {self.auto_merge_threshold!r}."
            )
        if not 0.0 <= self.flag_threshold <= 1.0:
            raise ValueError(
                "DeduplicationConfig.flag_threshold must be in [0.0, 1.0]; "
                f"got {self.flag_threshold!r}."
            )
        if self.auto_merge_threshold <= self.flag_threshold:
            raise ValueError(
                "DeduplicationConfig.auto_merge_threshold must be strictly greater "
                "than DeduplicationConfig.flag_threshold; "
                f"got auto_merge_threshold={self.auto_merge_threshold!r}, "
                f"flag_threshold={self.flag_threshold!r}."
            )
        if not 0.0 <= self.fuzzy_threshold <= 1.0:
            raise ValueError(
                "DeduplicationConfig.fuzzy_threshold must be in [0.0, 1.0]; "
                f"got {self.fuzzy_threshold!r}."
            )
        if self.max_candidates <= 0:
            raise ValueError(
                "DeduplicationConfig.max_candidates must be a positive integer; "
                f"got {self.max_candidates!r}."
            )


@dataclass
class DeduplicationResult:
    """Outcome of a :func:`dedupe_entity` call.

    ``applied_strategy`` is populated by ``add_entity`` (#011) when it acts
    on an ``action="merged"`` result; ``dedupe_entity`` itself never sets it.
    """

    action: Literal["none", "merged", "flagged"]
    matched_node_id: str | None = None
    matched_node_name: str | None = None
    similarity_score: float = 0.0
    match_type: Literal["embedding", "fuzzy", "both"] | None = None
    applied_strategy: MergeStrategy | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def dedupe_entity(
    *,
    database: AsyncDatabase,
    name: str,
    entity_type: NodeType,
    embedding: list[float],
    config: DeduplicationConfig,
    incoming_node_id: str | None = None,
) -> DeduplicationResult:
    """Decide whether ``name`` is a duplicate of an existing node.

    The function is read-only: it never inserts, updates, or deletes
    documents. It runs an Atlas ``$vectorSearch`` against the
    ``knowledge_graph`` collection, re-ranks every returned candidate with
    an optional RapidFuzz boost against ``name + canonical_name + aliases``,
    and returns the tiered decision based on the best-scoring candidate.

    Args:
        database: An ``AsyncDatabase`` handle to the project's MongoDB
            database (typically ``client[settings.mongo.mongo_initdb_database]``).
        name: The prospective entity's surface form (raw input — not yet
            normalized).
        entity_type: The prospective entity's :class:`NodeType`.
        embedding: The prospective entity's embedding vector (same model and
            dimensionality as the corpus).
        config: A validated :class:`DeduplicationConfig`.
        incoming_node_id: Optional pre-computed ``_id`` for the prospective
            entity. When provided, candidates that share a
            ``SAME_AS{status:"rejected"}`` edge with this ``_id`` are dropped
            from the search results. When omitted, the reject-pair filter is
            a no-op (used by exploratory queries that don't yet have an
            ``_id``).

    Returns:
        A :class:`DeduplicationResult` describing the tiered decision.

    Note:
        Self-match is **NOT** excluded by this function. When a caller passes
        ``incoming_node_id`` and the prospective entity already exists in the
        graph under that ``_id``, it will appear in the candidates (typically
        at cos≈1.0). Filtering ``matched_node_id == incoming_node_id`` is the
        caller's responsibility (see ``add_entity`` in #011).
    """

    if not config.enabled:
        return DeduplicationResult(action="none")

    collection = database[_KG_COLLECTION]
    pipeline = _build_pipeline(
        entity_type=entity_type,
        embedding=embedding,
        config=config,
        incoming_node_id=incoming_node_id,
    )

    candidates: list[dict[str, Any]] = []
    try:
        cursor = await collection.aggregate(pipeline)
        async for doc in cursor:
            candidates.append(doc)
    except Exception:
        logger.warning(
            "dedupe_entity: $vectorSearch failed; treating as no candidates",
            exc_info=True,
        )
        return DeduplicationResult(action="none")

    if not candidates:
        return DeduplicationResult(action="none")

    # Atlas $vectorSearch returns hits ordered by score descending, but we
    # re-rank every candidate so a lower-vec hit with a strong fuzzy match
    # can still win — matching the reference ``_check_for_duplicates`` loop
    # in ``long_term.py``. Atlas' cosine score ``(1 + cos) / 2`` is converted
    # back to raw cosine per candidate so the tier thresholds (and the
    # public ``similarity_score`` field) speak raw cosine — matching
    # ``resolution.semantic._cosine_similarity`` and the rest of the codebase.
    best_match: dict[str, Any] | None = None
    best_score: float = 0.0
    best_match_type: Literal["embedding", "both"] = "embedding"

    for candidate in candidates:
        raw_atlas_score = float(candidate.get("similarity_score", 0.0))
        semantic_score = 2.0 * raw_atlas_score - 1.0
        semantic_score = max(-1.0, min(1.0, semantic_score))

        if config.use_fuzzy_matching:
            fuzzy_score = _fuzzy_score(name, candidate)
            if fuzzy_score is not None and fuzzy_score >= config.fuzzy_threshold:
                combined = (semantic_score + fuzzy_score) / 2.0
                if combined > best_score:
                    best_score = combined
                    best_match = candidate
                    best_match_type = "both"
                # Mirror the reference loop's ``continue``: when fuzzy
                # passes the threshold, the semantic-only branch is skipped
                # for this candidate even if the combined score did not win.
                continue

        if semantic_score > best_score:
            best_score = semantic_score
            best_match = candidate
            best_match_type = "embedding"

    if best_match is None:
        return DeduplicationResult(action="none", candidates=candidates)

    # Tier decision.
    if best_score >= config.auto_merge_threshold:
        action: Literal["none", "merged", "flagged"] = "merged"
    elif best_score >= config.flag_threshold:
        action = "flagged"
    else:
        return DeduplicationResult(
            action="none",
            candidates=candidates,
        )

    return DeduplicationResult(
        action=action,
        matched_node_id=str(best_match.get("_id")),
        matched_node_name=best_match.get("name"),
        similarity_score=best_score,
        match_type=best_match_type,
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_pipeline(
    *,
    entity_type: NodeType,
    embedding: list[float],
    config: DeduplicationConfig,
    incoming_node_id: str | None,
) -> list[dict[str, Any]]:
    """Build the ``$vectorSearch`` + filter + reject-pair aggregation."""

    vector_filter: dict[str, Any] = {"kind": "node"}
    if config.match_same_type_only:
        vector_filter["type"] = entity_type.value

    pipeline: list[dict[str, Any]] = [
        {
            "$vectorSearch": {
                "index": _VECTOR_INDEX_NAME,
                "path": "embedding",
                "queryVector": embedding,
                "numCandidates": max(100, config.max_candidates * 10),
                "limit": config.max_candidates,
                "filter": vector_filter,
            }
        },
        {"$addFields": {"similarity_score": {"$meta": "vectorSearchScore"}}},
        # Tombstone exclusion is a filter on the search results because
        # ``merged_into`` is not declared as a filter-path on the vector
        # index (only ``kind`` and ``type`` are — see
        # ``tree.memory.indexing.core._ensure_vector_index``). We accept
        # either an absent field or an explicit null/empty value so test
        # fixtures that seed ``merged_into=None`` still pass.
        {
            "$match": {
                "$or": [
                    {"merged_into": {"$exists": False}},
                    {"merged_into": None},
                    {"merged_into": ""},
                ]
            }
        },
    ]

    if incoming_node_id is not None:
        pipeline.extend(
            [
                {
                    "$lookup": {
                        "from": _KG_COLLECTION,
                        "let": {"candidate_id": "$_id"},
                        "pipeline": [
                            {
                                "$match": {
                                    "kind": "edge",
                                    "type": EdgeType.SAME_AS.value,
                                    "properties.status": "rejected",
                                    "$expr": {
                                        "$or": [
                                            {
                                                "$and": [
                                                    {
                                                        "$eq": [
                                                            "$source_node_id",
                                                            incoming_node_id,
                                                        ]
                                                    },
                                                    {
                                                        "$eq": [
                                                            "$target_node_id",
                                                            "$$candidate_id",
                                                        ]
                                                    },
                                                ]
                                            },
                                            {
                                                "$and": [
                                                    {
                                                        "$eq": [
                                                            "$source_node_id",
                                                            "$$candidate_id",
                                                        ]
                                                    },
                                                    {
                                                        "$eq": [
                                                            "$target_node_id",
                                                            incoming_node_id,
                                                        ]
                                                    },
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            {"$limit": 1},
                        ],
                        "as": "_rejected_edges",
                    }
                },
                {"$match": {"_rejected_edges": {"$size": 0}}},
                {"$project": {"_rejected_edges": 0}},
            ]
        )

    return pipeline


def _fuzzy_score(name: str, candidate: dict[str, Any]) -> float | None:
    """Return the best RapidFuzz ratio score (0..1) for ``candidate``.

    Compares the normalized input against the candidate's ``name``,
    ``canonical_name`` (when present), and ``aliases``. The scorer is
    :func:`rapidfuzz.fuzz.ratio` — matching the reference
    ``_check_for_duplicates`` implementation. The resolver chain still uses
    ``token_sort_ratio`` for its own fuzzy matching; the choice of scorer is
    deliberately scoped to dedup.

    Returns ``None`` when ``rapidfuzz`` is unavailable or the candidate has
    no ``name``/``canonical_name``/``aliases`` to compare against.
    """

    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover - dep is required at runtime
        return None

    normalized_input = _normalize(name)
    surfaces: list[str] = []

    candidate_name = candidate.get("name")
    if candidate_name:
        surfaces.append(str(candidate_name))

    canonical_name = candidate.get("canonical_name")
    if canonical_name:
        surfaces.append(str(canonical_name))

    aliases = candidate.get("aliases") or []
    if not aliases:
        # Older nodes may keep aliases under ``properties.aliases``; honor
        # both for compatibility with the resolver / data-model conventions.
        aliases = (candidate.get("properties") or {}).get("aliases", [])
    surfaces.extend(str(alias) for alias in aliases if alias)

    # Deduplicate surfaces while preserving order: a node may have
    # ``name == canonical_name`` (or repeated aliases) and we do not want to
    # waste comparisons on identical strings.
    seen: set[str] = set()
    unique_surfaces: list[str] = []
    for surface in surfaces:
        if surface not in seen:
            seen.add(surface)
            unique_surfaces.append(surface)

    if not unique_surfaces:
        return None

    best = 0.0
    for surface in unique_surfaces:
        score = fuzz.ratio(normalized_input, _normalize(surface)) / 100.0
        if score > best:
            best = score
    return best
