from typing import Any

from pydantic import BaseModel, Field

from tree.entities.memory import EdgeType, NodeType
from tree.memory.rag.types import ParentChunk
from tree.memory.graph.resolution.types import ResolvedEntity


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
    :class:`MemoryEntry` model validator at write time.
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
    validator-pipeline step in :mod:`tree.memory.pipeline`
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
    """Output of ``clean-and-chunk`` — one cleaned document split into its hierarchy.

    ``document_id``/``source_uri``/``source_type``/``title``/``date`` are carried
    so later tasks do not have to re-load the source ``Document`` from MongoDB;
    ``title`` and each parent's ``heading_path`` are the two **Contextual
    header** inputs the child embeddings are built from (ADR-006 decision 4).

    ``parents`` is the WHOLE payload of the split: every **Parent chunk** carries
    its own **Child chunk**s, so the loader (rag) and the structural-edge builder
    (graphrag) both read the hierarchy from this ONE field. Chunk ids are no
    longer carried — they are derived deterministically from ``source_uri`` +
    position (:func:`tree.memory.rag.load.parent_row_id`), which is what keeps
    ``clean-and-chunk`` ``INPUTS``-cache-safe and the load stage idempotent.
    """

    document_id: str
    source_uri: str
    source_type: str
    title: str | None = None
    date: str | None = None
    reference_uris: list[str] = Field(default_factory=list)
    parents: list[ParentChunk] = Field(default_factory=list)


class RawExtraction(BaseModel):
    """Output of ``llm-extract-entities`` — per-document extraction (unresolved)."""

    document_id: str
    source_uri: str
    chunked: ChunkedDocument
    extracted: ExtractionResult = Field(default_factory=ExtractionResult)


class ResolvedEntityKey(BaseModel):
    """Stable identifier for one extracted entity across a flow run.

    Used as the dictionary key in :class:`DedupMap` and the look-up map in
    ``apply-writes`` to remap edge endpoints from raw extracted names to final ids.
    """

    document_id: str
    entity_type: NodeType
    name: str

    model_config = {"frozen": True}

    def as_tuple(self) -> tuple[str, NodeType, str]:
        return (self.document_id, self.entity_type, self.name)


class ResolutionOutput(BaseModel):
    """Output of ``resolve-entities`` — plus the look-up tables ``apply-writes`` needs.

    ``resolved_by_key`` keeps the raw resolution result for every entity so
    downstream tasks can read ``canonical_name``/``match_type``/``confidence``.

    ``name_to_owner_id`` maps a per-type (canonical or surface) name from the
    existing graph to the owning node's ``_id``. Built from the **set-union**
    of ``name`` and non-null ``canonical_name`` projected from the candidate
    fetch — keyed on the type-prefix so the same string under two types stays
    disambiguated (e.g. ``PERSON:"alice"`` vs ``TASK:"alice"``).

    ``candidates_seen_by_type`` records the number of candidate rows the
    resolver actually saw per type (capped at ``max_candidates_per_type``);
    cap-hits are logged WARNING by ``resolve-entities``.

    ``embeddable_text_by_key`` maps each entity key to the text it is
    embedded on — the GENERIC node-text for most types, or
    ``properties.statement`` / ``properties.object`` for PREFERENCE / FACT.
    ``embed-entities`` embeds the unique set of these texts, ``dedupe-entities``
    deduplicates each entity against its own text-vector, and ``apply-writes``
    persists the same vector — so the dedup decision and the stored vector share
    the search corpus' space.
    """

    entities: list[tuple[str, NodeType]] = Field(default_factory=list)
    resolved_by_key: dict[str, ResolvedEntity] = Field(default_factory=dict)
    name_to_owner_id: dict[str, str] = Field(default_factory=dict)
    candidates_seen_by_type: dict[str, int] = Field(default_factory=dict)
    embeddable_text_by_key: dict[str, str] = Field(default_factory=dict)


class EmbeddingMap(BaseModel):
    """Output of ``embed-entities`` — embeddable-text → embedding vector.

    Modeled as a plain dict carrier (not a list of tuples) so callers can
    look up a vector in O(1) by its embeddable text. The key is the
    **embeddable text** ``embed-entities`` embeds — the GENERIC node-text
    (``node_to_embedding_text``) for most types, or
    ``properties.statement`` / ``properties.object`` for PREFERENCE / FACT.
    Keying on node-text (rather than the canonical name) is what puts the
    dedup query vector and the persisted node vector in the same space as
    the search corpus.
    """

    vectors: dict[str, list[float]] = Field(default_factory=dict)


class DedupDecision(BaseModel):
    """Per-entity dedup decision, carried to ``apply-writes``."""

    action: str = "none"  # "none" | "merged" | "flagged"
    matched_node_id: str | None = None
    matched_node_name: str | None = None
    similarity_score: float = 0.0
    match_type: str | None = None


class DedupMap(BaseModel):
    """Output of ``dedupe-entities`` — keyed by ``"{doc_id}|{type}|{name}"``."""

    decisions: dict[str, DedupDecision] = Field(default_factory=dict)


class WriteSummary(BaseModel):
    """Output of ``apply-writes`` — flow-level counters returned to the caller."""

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
