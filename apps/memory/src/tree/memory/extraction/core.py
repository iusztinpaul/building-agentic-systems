"""
Core business logic for knowledge-graph extraction.

Pure functions (aside from the LLM call and DB writes) that:
1. Chunk a document into token-bounded pieces.
2. Ask an LLM to extract nodes & edges per chunk.
3. Build structural entries (PART_OF, NEXT, MENTIONS) deterministically.
4. Upsert the result to the ``knowledge_graph`` collection.

Resolution + deduplication used to live here (``normalize_nodes`` and four
helpers); those have moved to :mod:`tree.memory.resolution` (composite chain),
:mod:`tree.memory.extraction.dedup` (vector-search decision), and
:mod:`tree.memory.extraction.add_entity` (write-side orchestrator). The
pipeline in :mod:`tree.memory.extraction.pipeline` is the single caller that
ties them together.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import tiktoken
from beanie import PydanticObjectId
from pymongo import UpdateOne

from tree.config.app_config import app_config
from tree.entities.knowledge_graph import (
    EdgeType,
    NodeType,
    build_edge_id,
    build_node_id,
)
from tree.entities.ontology import (
    EDGE_CONSTRAINTS,
    LLM_EXTRACTABLE_EDGE_TYPES,
    LLM_EXTRACTABLE_NODE_TYPES,
    RELATION_SEMANTICS,
    get_ontology_schema,
)
from tree.memory.types import (
    ExtractedEdge,
    ExtractedNode,
    ExtractionResult,
    RawRejection,
)
from tree.models.base import BaseLLM

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"
_MAX_ALIASES = 50
_MAX_SOURCES = 500

# ---------------------------------------------------------------------------
# 1. Chunking
# ---------------------------------------------------------------------------

_ENCODER = tiktoken.get_encoding("cl100k_base")


def chunk_document(
    text: str,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[str]:
    """Split *text* into token-bounded chunks with overlap."""

    chunk_size = (
        chunk_size if chunk_size is not None else app_config.extraction.chunk_size
    )
    chunk_overlap = (
        chunk_overlap
        if chunk_overlap is not None
        else app_config.extraction.chunk_overlap
    )
    tokens = _ENCODER.encode(text)
    if not tokens:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        end = start + chunk_size
        chunk_tokens = tokens[start:end]
        chunks.append(_ENCODER.decode(chunk_tokens))
        start += chunk_size - chunk_overlap

    return chunks


# ---------------------------------------------------------------------------
# 2. LLM extraction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a knowledge-graph extraction engine.

Given a chunk of text, extract entities (nodes) and relationships (edges)
according to the ontology below.  Return **only** valid JSON that matches
the output schema.

## Ontology
{ontology}

## Output schema
{{
  "nodes": [
    {{
      "name": "<canonical lowercase name>",
      "type": "<node type from ontology>",
      "subtype": "<subtype from the type's `subtypes` list, or null>",
      "properties": {{ ... }}
    }}
  ],
  "edges": [
    {{
      "source_node_id": "<name of source node>",
      "source_type": "<node type of source>",
      "target_node_id": "<name of target node>",
      "target_type": "<node type of target>",
      "type": "<edge type from ontology>",
      "semantic_type": "<one of the `semantic_types` keys when type=related_to, else null>",
      "properties": {{}}
    }}
  ]
}}

Rules:
- Node names MUST be lowercase.
- Only use node types and edge types listed in the ontology.
- When the ontology lists a `subtypes` array for a node type, pick the
  best matching subtype value from that array and put it on the node's
  `subtype` field. When the type has no `subtypes` array, omit `subtype`
  (or set it to null).
- Respect edge constraints (source_type → target_type).
- For `related_to` edges (the umbrella domain-relation edge), you MUST
  set `semantic_type` to one of the keys under `edge_types.related_to.semantic_types`
  in the ontology. The `(source_type, target_type)` pair MUST appear in
  that semantic's `allowed_pairs`. Use the semantic's `properties` schema
  to populate the edge's `properties`.
- For every other edge type, omit `semantic_type` (or set it to null).
- If no entities or relationships are found, return empty lists.

## Emitting facts vs. typed relations vs. preferences

Use this decision tree for any proposition of the form
"subject — predicate — object" (e.g. "Earth orbits the Sun",
"Paul works at Anthropic", "I prefer dark mode"):

1. **First-person preference** ("I prefer X over Y", "I like dark mode"):
   emit a `preference` node with the typed-slot properties shape:
     - `statement`: short canonical phrase, <=80 chars
       (e.g. "prefers dark mode")
     - `category`: one of the closed enum members surfaced under
       `node_types.preference.properties.category` (ui / language /
       food / communication / work_style / time / social / aesthetic /
       other). Use `other` only as a last resort.
     - `target` (optional): what is preferred (e.g. "dark mode")
     - `over` (optional): what is dis-preferred when comparative
     - `context` (optional): when / where the preference applies
     - `strength` (optional, defaults "moderate"): weak / moderate /
       strong
   Don't emit a fact or a typed edge for first-person preferences.
   Example: "I really love dark mode in editors" -> a `preference`
   node with `name="prefers-dark-mode"`, properties
   `{{"statement": "prefers dark mode", "category": "ui",
   "target": "dark mode", "context": "in editors",
   "strength": "strong"}}`.

   STRICT-MODE POLICY (per `plan.md:461-465`):
     * Preferences are first-person only. If the speaker attributes
       a preference to a third party ("Alice prefers vegetarian
       food"), DO NOT emit a `preference` node - emit a `fact`
       instead (see branch 3).
     * Do NOT emit any `has` edge. The pipeline writes the
       deterministic `has: person:self -> preference` edge itself
       post-extraction.
     * If you notice that the new preference CONTRADICTS an earlier
       preference the user expressed, just emit the new one; a
       resolver downstream handles bi-temporal supersession
       automatically.

2. **Both subject and object resolve to POLE+O entities AND the relation
   matches one of the registered `related_to` semantics**: emit a
   `related_to` edge with the matching `semantic_type`. Don't emit a fact.
   Example: "Anthropic is headquartered in San Francisco" → a
   `related_to + semantic_type=headquarters_at` edge from
   `organization:anthropic` to `location:san francisco`.

3. **Otherwise** — free-text subject or object, relation doesn't match
   any registered semantic, or the proposition is a third-party claim
   (e.g. "Alice prefers dark mode") — emit a `fact` node with
   `subject` / `predicate` / `object` properties. Facts are **island
   nodes**: do NOT emit any edge (`mentions`, `same_as`, `related_to`,
   `has`, ...) whose source or target is a `fact`. Such edges are
   rejected at the validator and add no value.
   Example: "Earth orbits the Sun" → a `fact` node with
   `name="earth-orbits-sun"` and properties `subject="earth"`,
   `predicate="orbits"`, `object="sun"` — and NO edges.

   Like preferences, contradictory facts (e.g. two statements on the
   same `(subject, predicate)`) are bi-temporally superseded by the
   resolver - just emit the new fact, don't try to retract the old.
"""


async def extract_entities(
    llm: BaseLLM, chunk: str, *, chunk_id: str = ""
) -> ExtractionResult:
    """Ask the LLM to extract nodes and edges from a single chunk."""

    ontology = json.dumps(get_ontology_schema(), indent=2)
    system = _SYSTEM_PROMPT.format(ontology=ontology)
    raw = await llm.generate_json(chunk, system=system)
    result = _parse_extraction(raw)

    # Stamp every extracted item with this chunk's id.
    for node in result.nodes:
        node.chunk_id = chunk_id
    for edge in result.edges:
        edge.chunk_id = chunk_id
    # #030: raw rejections from this chunk carry the same chunk_id so
    # the audit collection's ``chunk_id`` column points at the source.
    for rejection in result.raw_rejections:
        rejection.chunk_id = chunk_id

    return result


# --- Phase-3 #028: legacy LLM emissions for the pre-POLE+O top-level
# node type ``task`` are silently re-routed to the new (parent, subtype)
# shape. The LLM prompt has been updated to emit the new shape directly
# (with a ``subtype`` field), but this rewrite keeps the parser tolerant
# of older prompts and saved/cached examples during the staging window
# between #028 and #033.
_LEGACY_NODE_TYPE_REWRITES: dict[str, tuple[NodeType, str]] = {
    "task": (NodeType.OBJECT, "task"),
}


def _parse_extraction(raw: dict[str, Any]) -> ExtractionResult:
    """Validate and filter the raw LLM output against the ontology.

    #030: rows that this function drops (unknown type, missing type,
    non-extractable type, invalid endpoint types, edge-constraint
    violations) are carried forward as
    :class:`tree.memory.types.RawRejection` entries on
    :attr:`ExtractionResult.raw_rejections`. The validator-pipeline
    step at :mod:`tree.memory.extraction.pipeline` then turns each one
    into an ``extraction_rejections`` row so the signal is structured
    rather than lost to ``logger.warning``.
    """

    nodes: list[ExtractedNode] = []
    raw_rejections: list[RawRejection] = []
    for n in raw.get("nodes", []):
        try:
            type_value = n["type"]
        except KeyError:
            logger.warning("Skipping node with missing type: %s", n)
            raw_rejections.append(
                RawRejection(kind="node", reason="missing_type", raw=dict(n))
            )
            continue

        # Legacy → POLE+O subtype re-route (see _LEGACY_NODE_TYPE_REWRITES).
        legacy_rewrite = _LEGACY_NODE_TYPE_REWRITES.get(type_value)
        emitted_subtype = n.get("subtype")
        if legacy_rewrite is not None:
            node_type, derived_subtype = legacy_rewrite
            # An LLM that already learned the new shape may emit the
            # legacy ``type`` but pre-populate ``subtype``; trust the
            # caller's value when present.
            subtype: str | None = (
                emitted_subtype if emitted_subtype is not None else derived_subtype
            )
        else:
            try:
                node_type = NodeType(type_value)
            except ValueError:
                logger.warning("Skipping node with invalid type: %s", n)
                raw_rejections.append(
                    RawRejection(kind="node", reason="unknown_type", raw=dict(n))
                )
                continue
            subtype = emitted_subtype if emitted_subtype is not None else None

        if node_type not in LLM_EXTRACTABLE_NODE_TYPES:
            logger.warning("Skipping non-extractable node type: %s", node_type)
            raw_rejections.append(
                RawRejection(kind="node", reason="non_extractable_type", raw=dict(n))
            )
            continue
        nodes.append(
            ExtractedNode(
                name=str(n.get("name", "")).lower().strip(),
                type=node_type,
                subtype=subtype,
                properties=n.get("properties", {}),
            )
        )

    edges: list[ExtractedEdge] = []
    for e in raw.get("edges", []):
        raw_type = e.get("type")
        if raw_type is None:
            logger.warning("Skipping edge with missing type: %s", e)
            raw_rejections.append(
                RawRejection(kind="edge", reason="missing_type", raw=dict(e))
            )
            continue

        # #029: re-route legacy LLM emissions ``todo`` / ``experienced``
        # to the new umbrella shape. The prompt no longer asks for
        # those types — this branch keeps the parser tolerant of older
        # prompts and cached examples during the staging window.
        legacy_semantic: str | None = None
        if raw_type == "todo":
            raw_type = "related_to"
            legacy_semantic = "has_task"
        elif raw_type == "experienced":
            raw_type = "related_to"
            legacy_semantic = "experienced_by"

        try:
            edge_type = EdgeType(raw_type)
        except ValueError:
            logger.warning("Skipping edge with invalid type: %s", e)
            raw_rejections.append(
                RawRejection(kind="edge", reason="unknown_type", raw=dict(e))
            )
            continue
        if edge_type not in LLM_EXTRACTABLE_EDGE_TYPES:
            logger.warning("Skipping non-extractable edge type: %s", edge_type)
            raw_rejections.append(
                RawRejection(kind="edge", reason="non_extractable_type", raw=dict(e))
            )
            continue

        try:
            src_type_value = e["source_type"]
            tgt_type_value = e["target_type"]
        except KeyError:
            logger.warning("Skipping edge with missing endpoint types: %s", e)
            raw_rejections.append(
                RawRejection(kind="edge", reason="missing_endpoint_type", raw=dict(e))
            )
            continue

        # #029: re-route the legacy endpoint type ``task`` to its POLE+O
        # parent now that it no longer exists as a top-level node type.
        if src_type_value == "task":
            src_type_value = "object"
        if tgt_type_value == "task":
            tgt_type_value = "object"

        try:
            src_type = NodeType(src_type_value)
            tgt_type = NodeType(tgt_type_value)
        except ValueError:
            logger.warning("Skipping edge with invalid node types: %s", e)
            raw_rejections.append(
                RawRejection(kind="edge", reason="invalid_endpoint_type", raw=dict(e))
            )
            continue

        # Resolve semantic_type for ``related_to`` rows. Legacy
        # rewrites already populated ``legacy_semantic``; the prompt
        # surfaces the field as ``semantic_type``.
        semantic_type = legacy_semantic or e.get("semantic_type")

        # Per-semantic constraint check for ``related_to``. Constraints
        # for the other edges fall through to the umbrella check below.
        if edge_type == EdgeType.RELATED_TO:
            if not isinstance(semantic_type, str) or not semantic_type:
                logger.warning(
                    "Skipping related_to edge with missing semantic_type: %s", e
                )
                raw_rejections.append(
                    RawRejection(
                        kind="edge",
                        reason="missing_semantic_type",
                        raw=dict(e),
                    )
                )
                continue
            spec = RELATION_SEMANTICS.get(semantic_type)
            if spec is None:
                logger.warning(
                    "Skipping related_to edge with unknown semantic_type=%r: %s",
                    semantic_type,
                    e,
                )
                raw_rejections.append(
                    RawRejection(kind="edge", reason="unknown_semantic", raw=dict(e))
                )
                continue
            if (src_type.value, tgt_type.value) not in spec.allowed_pairs:
                logger.warning(
                    "related_to[%s] disallows pair (%s, %s); allowed=%s",
                    semantic_type,
                    src_type.value,
                    tgt_type.value,
                    spec.allowed_pairs,
                )
                raw_rejections.append(
                    RawRejection(kind="edge", reason="disallowed_pair", raw=dict(e))
                )
                continue
        else:
            constraints = EDGE_CONSTRAINTS.get(edge_type, [])
            if not any(
                src_type == c.source_type and tgt_type == c.target_type
                for c in constraints
            ):
                logger.warning(
                    "Edge %s violates constraint (expected one of %s, got %s→%s)",
                    edge_type,
                    [(c.source_type, c.target_type) for c in constraints],
                    src_type,
                    tgt_type,
                )
                raw_rejections.append(
                    RawRejection(kind="edge", reason="disallowed_pair", raw=dict(e))
                )
                continue
            # Non-related_to edges must NOT carry a semantic_type.
            semantic_type = None

        edges.append(
            ExtractedEdge(
                source_node_id=str(e.get("source_node_id", "")).lower().strip(),
                source_type=src_type,
                target_node_id=str(e.get("target_node_id", "")).lower().strip(),
                target_type=tgt_type,
                type=edge_type,
                semantic_type=semantic_type,
                properties=e.get("properties", {}),
            )
        )

    return ExtractionResult(nodes=nodes, edges=edges, raw_rejections=raw_rejections)


# ---------------------------------------------------------------------------
# 3. Structural entries (deterministic, not LLM)
# ---------------------------------------------------------------------------


def build_structural_entries(
    *,
    document_id: PydanticObjectId,
    source_type: str,
    source_uri: str,
    date: str | None,
    chunk_texts: list[str],
    chunk_ids: list[str],
    extracted: ExtractionResult,
    reference_uris: list[str] | None = None,
) -> ExtractionResult:
    """Create DOCUMENT, CHUNK, PART_OF, NEXT, MENTIONS, and REFERENCED entries."""

    structural_chunk_id = str(uuid4())
    doc_name = source_uri
    doc_node = ExtractedNode(
        name=doc_name,
        type=NodeType.DOCUMENT,
        properties={
            "source_type": source_type,
            "source_uri": source_uri,
            "date": date,
        },
        chunk_id=structural_chunk_id,
    )

    chunk_nodes: list[ExtractedNode] = []
    part_of_edges: list[ExtractedEdge] = []
    next_edges: list[ExtractedEdge] = []

    for idx, text in enumerate(chunk_texts):
        cid = chunk_ids[idx] if idx < len(chunk_ids) else structural_chunk_id
        chunk_name = f"{doc_name}#chunk-{idx}"
        chunk_nodes.append(
            ExtractedNode(
                name=chunk_name,
                type=NodeType.CHUNK,
                properties={
                    "source_type": source_type,
                    "source_uri": source_uri,
                    "content": text,
                    "date": date,
                },
                chunk_id=cid,
            )
        )
        part_of_edges.append(
            ExtractedEdge(
                source_node_id=chunk_name,
                source_type=NodeType.CHUNK,
                target_node_id=doc_name,
                target_type=NodeType.DOCUMENT,
                type=EdgeType.PART_OF,
                chunk_id=cid,
            )
        )
        if idx > 0:
            prev_name = f"{doc_name}#chunk-{idx - 1}"
            next_edges.append(
                ExtractedEdge(
                    source_node_id=prev_name,
                    source_type=NodeType.CHUNK,
                    target_node_id=chunk_name,
                    target_type=NodeType.CHUNK,
                    type=EdgeType.NEXT,
                    chunk_id=cid,
                )
            )

    # MENTIONS: Document → Person for every unique person node extracted.
    person_names = {n.name for n in extracted.nodes if n.type == NodeType.PERSON}
    mentions_edges = [
        ExtractedEdge(
            source_node_id=doc_name,
            source_type=NodeType.DOCUMENT,
            target_node_id=person,
            target_type=NodeType.PERSON,
            type=EdgeType.MENTIONS,
            chunk_id=structural_chunk_id,
        )
        for person in person_names
    ]

    # REFERENCED: Document → Document for pre-populated references.
    referenced_edges: list[ExtractedEdge] = []
    for ref_uri in reference_uris or []:
        referenced_edges.append(
            ExtractedEdge(
                source_node_id=doc_name,
                source_type=NodeType.DOCUMENT,
                target_node_id=ref_uri,
                target_type=NodeType.DOCUMENT,
                type=EdgeType.REFERENCED,
                chunk_id=structural_chunk_id,
            )
        )

    return ExtractionResult(
        nodes=[doc_node, *chunk_nodes],
        edges=[*part_of_edges, *next_edges, *mentions_edges, *referenced_edges],
    )


# ---------------------------------------------------------------------------
# 4. Persistence (upsert to knowledge_graph)
# ---------------------------------------------------------------------------
#
# Resolution + dedup live in dedicated modules (``tree.memory.resolution``,
# ``tree.memory.extraction.dedup``, ``tree.memory.extraction.add_entity``).
# The pipeline in ``tree.memory.extraction.pipeline`` is the single caller
# that wires them together.


async def upsert_graph_entries(
    result: ExtractionResult,
    *,
    user_id: PydanticObjectId,
    source_document_id: PydanticObjectId,
    database: str,
    client: Any,
) -> int:
    """Upsert extraction results directly to the knowledge_graph collection.

    Uses aggregation pipeline updates to merge properties (not overwrite)
    and accumulate aliases and sources.

    Returns the number of upsert operations executed.
    """

    now = datetime.now(tz=UTC)
    collection = client[database][_KG_COLLECTION]
    ops: list[UpdateOne] = []

    for node in result.nodes:
        node_id = build_node_id(user_id, node.type, node.name)
        aliases = node.properties.get("aliases", [])
        # Exclude aliases from the merge so Stage 1 does not overwrite
        # the existing aliases array — Stage 2 handles alias accumulation.
        props_without_aliases = {
            k: v for k, v in node.properties.items() if k != "aliases"
        }
        ops.append(
            UpdateOne(
                {"_id": node_id},
                [
                    # Stage 1: merge properties and set scalar fields.
                    {
                        "$set": {
                            "user_id": user_id,
                            "kind": "node",
                            "type": node.type.value,
                            # #028: persist the POLE+O subtype slot. ``None``
                            # is written as a NULL column on first insert and
                            # left untouched on subsequent merges; once
                            # populated, later writes overwrite with the
                            # latest LLM-emitted value (last-write-wins on
                            # the slot, mirroring ``properties``-merge).
                            "subtype": node.subtype,
                            "name": node.name,
                            "properties": {
                                "$mergeObjects": [
                                    {"$ifNull": ["$properties", {}]},
                                    props_without_aliases,
                                ]
                            },
                            "sources": {
                                "$slice": [
                                    {
                                        "$setUnion": [
                                            {"$ifNull": ["$sources", []]},
                                            [source_document_id],
                                        ]
                                    },
                                    _MAX_SOURCES,
                                ]
                            },
                            "created_at": {"$ifNull": ["$created_at", now]},
                            "updated_at": now,
                            "embedding": {"$ifNull": ["$embedding", []]},
                        }
                    },
                    # Stage 2: union aliases (separate stage to avoid
                    # conflicting paths with 'properties'). Capped to
                    # prevent unbounded growth from typos/variants.
                    {
                        "$set": {
                            "properties.aliases": {
                                "$slice": [
                                    {
                                        "$setUnion": [
                                            {"$ifNull": ["$properties.aliases", []]},
                                            aliases,
                                        ]
                                    },
                                    _MAX_ALIASES,
                                ]
                            },
                        }
                    },
                ],
                upsert=True,
            )
        )

    for edge in result.edges:
        src_id = build_node_id(user_id, edge.source_type, edge.source_node_id)
        tgt_id = build_node_id(user_id, edge.target_type, edge.target_node_id)
        edge_id = build_edge_id(src_id, edge.type, tgt_id)
        ops.append(
            UpdateOne(
                {"_id": edge_id},
                [
                    {
                        "$set": {
                            "user_id": user_id,
                            "kind": "edge",
                            "type": edge.type.value,
                            # #029: persist ``semantic_type`` on every edge
                            # row (None for non-related_to). The partial
                            # index ``user_type_semantic_type`` only
                            # indexes non-null rows.
                            "semantic_type": edge.semantic_type,
                            "source_node_id": src_id,
                            "source_type": edge.source_type.value,
                            "target_node_id": tgt_id,
                            "target_type": edge.target_type.value,
                            "properties": {
                                "$mergeObjects": [
                                    {"$ifNull": ["$properties", {}]},
                                    edge.properties,
                                ]
                            },
                            "sources": {
                                "$slice": [
                                    {
                                        "$setUnion": [
                                            {"$ifNull": ["$sources", []]},
                                            [source_document_id],
                                        ]
                                    },
                                    _MAX_SOURCES,
                                ]
                            },
                            "created_at": {"$ifNull": ["$created_at", now]},
                            "updated_at": now,
                        }
                    }
                ],
                upsert=True,
            )
        )

    if ops:
        await collection.bulk_write(ops, ordered=False)

    logger.info(
        "Upserted %d entries (nodes=%d, edges=%d) for document %s",
        len(ops),
        len(result.nodes),
        len(result.edges),
        source_document_id,
    )

    return len(ops)
