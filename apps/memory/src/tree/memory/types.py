from typing import Any

from pydantic import BaseModel, Field

from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.memory.resolution.types import ResolvedEntity


class ExtractedNode(BaseModel):
    """A node extracted by the LLM (before persistence).

    Phase-3 #028: ``subtype`` carries the LLM-emitted (or pipeline-derived)
    closed-vocabulary slot — e.g. ``("object", "task")`` for what used
    to be a top-level ``task`` row. ``None`` is accepted at construction;
    the strict subtype-required envelope check lands at #030.
    """

    name: str
    type: NodeType
    subtype: str | None = None
    properties: dict[str, Any] = {}
    chunk_id: str = ""


class ExtractedEdge(BaseModel):
    """An edge extracted by the LLM (before persistence).

    Phase-3 #029: ``semantic_type`` carries the discriminator for the
    new ``related_to`` umbrella edge. Required on every ``related_to``
    row, ``None`` on every other edge type — enforced again by the
    :class:`KnowledgeGraphEntry` model validator at write time.
    """

    source_node_id: str
    source_type: NodeType
    target_node_id: str
    target_type: NodeType
    type: EdgeType
    semantic_type: str | None = None
    properties: dict[str, Any] = {}
    chunk_id: str = ""


class RawRejection(BaseModel):
    """A raw LLM emission ``_parse_extraction`` chose to drop (#030).

    Carried forward through :class:`ExtractionResult` so the
    validator-pipeline step in :mod:`tree.memory.extraction.pipeline`
    can turn it into an ``extraction_rejections`` row instead of
    losing the signal to a ``logger.warning`` line.

    The two reasons today (per :func:`_parse_extraction`'s drop list)
    are ``unknown_type`` (the LLM emitted a type string we don't
    register) and ``invalid_endpoint_types`` (an edge with one of its
    endpoint types not in :class:`NodeType`).
    """

    kind: str
    reason: str
    raw: dict[str, Any] = Field(default_factory=dict)
    chunk_id: str = ""


class ExtractionResult(BaseModel):
    """Aggregated extraction output from one or more chunks."""

    nodes: list[ExtractedNode] = []
    edges: list[ExtractedEdge] = []
    # #030: rows the LLM-emission parser dropped before the envelope
    # validator could see them. Carried to the validator-task so the
    # ``extraction_rejections`` audit collection receives every drop,
    # not just envelope-level ones.
    raw_rejections: list[RawRejection] = Field(default_factory=list)

    def merge(self, other: "ExtractionResult") -> "ExtractionResult":
        """Combine two results (e.g. from different chunks)."""
        return ExtractionResult(
            nodes=self.nodes + other.nodes,
            edges=self.edges + other.edges,
            raw_rejections=self.raw_rejections + other.raw_rejections,
        )


class QueryResult(BaseModel):
    """Result of a memory query: seed nodes, expanded nodes, and edges."""

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Extraction-pipeline transit types
# ---------------------------------------------------------------------------
#
# Pydantic models that cross Prefect task boundaries. Each one is serializable
# without custom adapters so caching on INPUTS keys consistently and per-task
# logs can quote stable counts.


class ChunkedDocument(BaseModel):
    """Output of task ① — chunks + provenance-stamped structural entries.

    ``document_id``/``source_uri``/``source_type``/``date`` are carried so
    later tasks do not have to re-load the source ``Document`` from MongoDB.
    """

    document_id: str
    source_uri: str
    source_type: str
    date: str | None = None
    reference_uris: list[str] = Field(default_factory=list)
    chunk_texts: list[str] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)
    structural: ExtractionResult = Field(default_factory=ExtractionResult)


class RawExtraction(BaseModel):
    """Output of task ② — per-document LLM extraction (unresolved)."""

    document_id: str
    source_uri: str
    chunked: ChunkedDocument
    extracted: ExtractionResult = Field(default_factory=ExtractionResult)


class ResolvedEntityKey(BaseModel):
    """Stable identifier for one extracted entity across a flow run.

    Used as the dictionary key in :class:`DedupMap` and the look-up map in
    task ⑥ to remap edge endpoints from raw extracted names to final ids.
    """

    document_id: str
    entity_type: NodeType
    name: str

    model_config = {"frozen": True}

    def as_tuple(self) -> tuple[str, NodeType, str]:
        return (self.document_id, self.entity_type, self.name)


class ResolutionOutput(BaseModel):
    """Output of task ③ — resolved entities + the look-up tables task ⑥ needs.

    ``resolved_by_key`` keeps the raw resolution result for every entity so
    downstream tasks can read ``canonical_name``/``match_type``/``confidence``.

    ``name_to_owner_id`` maps a per-type (canonical or surface) name from the
    existing graph to the owning node's ``_id``. Built from the **set-union**
    of ``name`` and non-null ``canonical_name`` projected from the candidate
    fetch — keyed on the type-prefix so the same string under two types stays
    disambiguated (e.g. ``PERSON:"alice"`` vs ``TASK:"alice"``).

    ``candidates_seen_by_type`` records the number of candidate rows the
    resolver actually saw per type (capped at ``max_candidates_per_type``);
    cap-hits are logged WARNING by task ③.

    ``embeddable_text_by_key`` (#042) maps each entity key to the text it
    is embedded on — the GENERIC node-text (the shared #041 builder) for
    most types, or ``properties.statement`` / ``properties.object`` for
    PREFERENCE / FACT. Task ④ embeds the unique set of these texts, task ⑤
    deduplicates each entity against its own text-vector, and task ⑥
    persists the same vector — so the dedup decision and the stored vector
    share the search corpus' space.
    """

    entities: list[tuple[str, NodeType]] = Field(default_factory=list)
    resolved_by_key: dict[str, ResolvedEntity] = Field(default_factory=dict)
    name_to_owner_id: dict[str, str] = Field(default_factory=dict)
    candidates_seen_by_type: dict[str, int] = Field(default_factory=dict)
    embeddable_text_by_key: dict[str, str] = Field(default_factory=dict)


class EmbeddingMap(BaseModel):
    """Output of task ④ — embeddable-text → embedding vector.

    Modeled as a plain dict carrier (not a list of tuples) so callers can
    look up a vector in O(1) by its embeddable text.

    #042: the key is the **embeddable text** task ④ embeds — the GENERIC
    node-text (the shared #041 ``node_to_embedding_text``) for most types,
    or ``properties.statement`` / ``properties.object`` for PREFERENCE /
    FACT. Before #042 the key was the canonical NAME and the vector was a
    name-embedding; switching the grain to node-text is what puts the
    dedup query vector and the persisted node vector in the same space as
    the search corpus.
    """

    vectors: dict[str, list[float]] = Field(default_factory=dict)


class DedupDecision(BaseModel):
    """Per-entity dedup decision, carried across the ⑤→⑥ boundary."""

    action: str = "none"  # "none" | "merged" | "flagged"
    matched_node_id: str | None = None
    matched_node_name: str | None = None
    similarity_score: float = 0.0
    match_type: str | None = None


class DedupMap(BaseModel):
    """Output of task ⑤ — keyed by ``"{doc_id}|{type}|{name}"``."""

    decisions: dict[str, DedupDecision] = Field(default_factory=dict)


class WriteSummary(BaseModel):
    """Output of task ⑥ — flow-level counters returned to the caller."""

    nodes_written: int = 0
    edges_written: int = 0
    nodes_merged: int = 0
    nodes_flagged: int = 0
    same_as_edges_emitted: int = 0
    documents_processed: int = 0


def make_entity_key(document_id: str, entity_type: NodeType, name: str) -> str:
    """Build the stable string key used across :class:`ResolutionOutput` and
    :class:`DedupMap`. Hash-friendly across Prefect serialization."""

    return f"{document_id}|{entity_type.value}|{name}"


def make_type_name_key(entity_type: NodeType, name: str) -> str:
    """Build the type-prefixed key used in :attr:`ResolutionOutput.name_to_owner_id`."""

    return f"{entity_type.value}|{name}"
