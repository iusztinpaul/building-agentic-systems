"""
Core business logic for knowledge-graph extraction.

Pure functions (aside from the LLM call and DB writes) that:
1. Chunk a document into token-bounded pieces.
2. Ask an LLM to extract nodes & edges per chunk.
3. Build structural entries (PART_OF, NEXT, MENTIONS) deterministically.
4. Normalise duplicate nodes via fuzzy matching, alias resolution, and
   cross-document deduplication against the existing knowledge graph.
5. Upsert the result to the knowledge_graph collection.
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any
from uuid import uuid4

import tiktoken
from beanie import PydanticObjectId
from pymongo import UpdateOne

from twin.config.app_config import app_config
from twin.entities.knowledge_graph import (
    EdgeType,
    NodeType,
    build_edge_id,
    build_node_id,
)
from twin.entities.ontology import (
    EDGE_CONSTRAINTS,
    LLM_EXTRACTABLE_EDGE_TYPES,
    LLM_EXTRACTABLE_NODE_TYPES,
    get_ontology_schema,
)
from twin.memory.types import ExtractionResult, ExtractedEdge, ExtractedNode
from twin.models.base import BaseLLM

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"

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
      "properties": {{}}
    }}
  ]
}}

Rules:
- Node names MUST be lowercase.
- Only use node types and edge types listed in the ontology.
- Respect edge constraints (source_type → target_type).
- If no entities or relationships are found, return empty lists.
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

    return result


def _parse_extraction(raw: dict[str, Any]) -> ExtractionResult:
    """Validate and filter the raw LLM output against the ontology."""

    nodes: list[ExtractedNode] = []
    for n in raw.get("nodes", []):
        try:
            node_type = NodeType(n["type"])
        except KeyError, ValueError:
            logger.warning("Skipping node with invalid type: %s", n)
            continue
        if node_type not in LLM_EXTRACTABLE_NODE_TYPES:
            logger.warning("Skipping non-extractable node type: %s", node_type)
            continue
        nodes.append(
            ExtractedNode(
                name=str(n.get("name", "")).lower().strip(),
                type=node_type,
                properties=n.get("properties", {}),
            )
        )

    edges: list[ExtractedEdge] = []
    for e in raw.get("edges", []):
        try:
            edge_type = EdgeType(e["type"])
        except KeyError, ValueError:
            logger.warning("Skipping edge with invalid type: %s", e)
            continue
        if edge_type not in LLM_EXTRACTABLE_EDGE_TYPES:
            logger.warning("Skipping non-extractable edge type: %s", edge_type)
            continue

        constraint = EDGE_CONSTRAINTS[edge_type]
        try:
            src_type = NodeType(e["source_type"])
            tgt_type = NodeType(e["target_type"])
        except KeyError, ValueError:
            logger.warning("Skipping edge with invalid node types: %s", e)
            continue

        if src_type != constraint.source_type or tgt_type != constraint.target_type:
            logger.warning(
                "Edge %s violates constraint (%s→%s expected, got %s→%s)",
                edge_type,
                constraint.source_type,
                constraint.target_type,
                src_type,
                tgt_type,
            )
            continue

        edges.append(
            ExtractedEdge(
                source_node_id=str(e.get("source_node_id", "")).lower().strip(),
                source_type=src_type,
                target_node_id=str(e.get("target_node_id", "")).lower().strip(),
                target_type=tgt_type,
                type=edge_type,
                properties=e.get("properties", {}),
            )
        )

    return ExtractionResult(nodes=nodes, edges=edges)


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
# 4. Normalisation (fuzzy dedup + alias resolution + cross-document)
# ---------------------------------------------------------------------------


def _get_node_aliases(node: ExtractedNode) -> list[str]:
    """Extract the aliases list from a node's properties."""
    return node.properties.get("aliases", [])


def _merge_into_canonical(
    canonical: ExtractedNode,
    incoming: ExtractedNode,
    canonical_map: dict[tuple[NodeType, str], str],
) -> None:
    """Merge an incoming node into a canonical node.

    - Properties merged (canonical wins on conflicts).
    - Aliases accumulated as a union, including the non-canonical name.
    - canonical_map updated.
    """
    key = (incoming.type, incoming.name)
    canonical_map[key] = canonical.name

    # Collect aliases from BOTH nodes BEFORE merging properties.
    existing_aliases = set(canonical.properties.get("aliases", []))
    incoming_aliases = set(incoming.properties.get("aliases", []))

    # Merge properties (canonical wins on conflicts).
    canonical.properties = {**incoming.properties, **canonical.properties}

    # Union all aliases, adding the non-canonical name.
    all_aliases = existing_aliases | incoming_aliases
    if incoming.name != canonical.name:
        all_aliases.add(incoming.name)
    all_aliases.discard(canonical.name)
    if all_aliases:
        canonical.properties["aliases"] = sorted(all_aliases)


def _matches_node(
    node: ExtractedNode,
    candidate_name: str,
    candidate_aliases: list[str],
    threshold: float,
) -> bool:
    """Check if a node matches a candidate by name or alias."""
    # Fuzzy name match.
    if SequenceMatcher(None, node.name, candidate_name).ratio() >= threshold:
        return True
    # Node's name in candidate's aliases (exact).
    if node.name in candidate_aliases:
        return True
    # Node's aliases contain candidate's name (exact).
    if candidate_name in _get_node_aliases(node):
        return True
    return False


async def _fetch_candidate_nodes(
    names_by_type: dict[NodeType, set[str]],
    collection: Any,
) -> dict[NodeType, list[dict[str, Any]]]:
    """Batch-query MongoDB for existing nodes that might match new names.

    Issues one query per node type. Matches on name OR aliases.
    Returns candidates grouped by node type.
    """
    candidates: dict[NodeType, list[dict[str, Any]]] = {}
    for node_type, names in names_by_type.items():
        if not names:
            continue
        name_list = list(names)
        query = {
            "kind": "node",
            "type": node_type.value,
            "$or": [
                {"name": {"$in": name_list}},
                {"properties.aliases": {"$in": name_list}},
            ],
        }
        docs = await collection.find(query).to_list()
        if docs:
            candidates[node_type] = docs
    return candidates


async def normalize_nodes(
    result: ExtractionResult,
    *,
    client: Any | None = None,
    database: str | None = None,
) -> ExtractionResult:
    """Merge duplicate nodes by fuzzy-matching, alias resolution, and
    cross-document deduplication against the existing knowledge graph.

    When *client* and *database* are provided, queries MongoDB for existing
    nodes that may match the newly extracted ones. Without DB context, falls
    back to in-memory dedup only.
    """

    threshold = app_config.extraction.similarity_threshold

    # Map (type, original_name) → canonical_name
    canonical_map: dict[tuple[NodeType, str], str] = {}
    kept_nodes: list[ExtractedNode] = []

    # --- Phase A: In-memory dedup (enhanced with alias matching) ---
    for node in result.nodes:
        key = (node.type, node.name)
        if key in canonical_map:
            # Exact name already seen — still merge properties/aliases.
            canonical_name = canonical_map[key]
            for kept in kept_nodes:
                if kept.type == node.type and kept.name == canonical_name:
                    _merge_into_canonical(kept, node, canonical_map)
                    break
            continue

        matched = False
        for kept in kept_nodes:
            if kept.type != node.type:
                continue
            if _matches_node(node, kept.name, _get_node_aliases(kept), threshold):
                _merge_into_canonical(kept, node, canonical_map)
                matched = True
                break

        if not matched:
            canonical_map[key] = node.name
            kept_nodes.append(node)

    # --- Phase B: Cross-document resolution against MongoDB ---
    if client is not None and database is not None:
        collection = client[database][_KG_COLLECTION]

        # Collect names to query (only LLM-extractable types).
        names_by_type: dict[NodeType, set[str]] = defaultdict(set)
        for node in kept_nodes:
            if node.type in LLM_EXTRACTABLE_NODE_TYPES:
                names_by_type[node.type].add(node.name)
                for alias in _get_node_aliases(node):
                    names_by_type[node.type].add(alias)

        db_candidates = await _fetch_candidate_nodes(names_by_type, collection)

        for node in list(kept_nodes):
            candidates = db_candidates.get(node.type, [])
            for db_node in candidates:
                db_name = db_node.get("name", "")
                db_aliases = db_node.get("properties", {}).get("aliases", [])

                if db_name == node.name:
                    # Exact match on name — already handled by upsert.
                    continue

                if _matches_node(node, db_name, db_aliases, threshold):
                    # DB node's name becomes canonical (preserves existing edges).
                    old_name = node.name
                    old_key = (node.type, old_name)

                    # Create a synthetic canonical node with the DB name.
                    db_extracted = ExtractedNode(
                        name=db_name,
                        type=node.type,
                        properties=db_node.get("properties", {}),
                    )
                    _merge_into_canonical(db_extracted, node, canonical_map)

                    # Replace the kept node with the DB-canonical version.
                    idx = kept_nodes.index(node)
                    kept_nodes[idx] = db_extracted

                    # Also map the old canonical name.
                    canonical_map[old_key] = db_name

                    logger.info(
                        "Resolved '%s' → '%s' (cross-document, type=%s)",
                        old_name,
                        db_name,
                        node.type,
                    )
                    break

    # --- Remap edge endpoints and deduplicate edges ---
    remapped_edges: list[ExtractedEdge] = []
    for edge in result.edges:
        src_key = (edge.source_type, edge.source_node_id)
        tgt_key = (edge.target_type, edge.target_node_id)
        remapped_edges.append(
            edge.model_copy(
                update={
                    "source_node_id": canonical_map.get(src_key, edge.source_node_id),
                    "target_node_id": canonical_map.get(tgt_key, edge.target_node_id),
                }
            )
        )

    # Deduplicate edges that became identical after node resolution.
    seen_edges: dict[tuple, ExtractedEdge] = {}
    for edge in remapped_edges:
        edge_key = (
            edge.source_type,
            edge.source_node_id,
            edge.type,
            edge.target_type,
            edge.target_node_id,
        )
        if edge_key in seen_edges:
            seen_edges[edge_key].properties = {
                **edge.properties,
                **seen_edges[edge_key].properties,
            }
        else:
            seen_edges[edge_key] = edge
    deduped_edges = list(seen_edges.values())

    return ExtractionResult(nodes=kept_nodes, edges=deduped_edges)


# ---------------------------------------------------------------------------
# 5. Persistence (upsert to knowledge_graph)
# ---------------------------------------------------------------------------


async def upsert_graph_entries(
    result: ExtractionResult,
    *,
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
        node_id = build_node_id(node.type, node.name)
        aliases = node.properties.get("aliases", [])
        ops.append(
            UpdateOne(
                {"_id": node_id},
                [
                    {
                        "$set": {
                            "kind": "node",
                            "type": node.type.value,
                            "name": node.name,
                            "properties": {
                                "$mergeObjects": [
                                    {"$ifNull": ["$properties", {}]},
                                    node.properties,
                                ]
                            },
                            "properties.aliases": {
                                "$setUnion": [
                                    {"$ifNull": ["$properties.aliases", []]},
                                    aliases,
                                ]
                            },
                            "sources": {
                                "$setUnion": [
                                    {"$ifNull": ["$sources", []]},
                                    [source_document_id],
                                ]
                            },
                            "created_at": {"$ifNull": ["$created_at", now]},
                            "updated_at": now,
                            "embedding": {"$ifNull": ["$embedding", []]},
                        }
                    }
                ],
                upsert=True,
            )
        )

    for edge in result.edges:
        src_id = build_node_id(edge.source_type, edge.source_node_id)
        tgt_id = build_node_id(edge.target_type, edge.target_node_id)
        edge_id = build_edge_id(src_id, edge.type, tgt_id)
        ops.append(
            UpdateOne(
                {"_id": edge_id},
                [
                    {
                        "$set": {
                            "kind": "edge",
                            "type": edge.type.value,
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
                                "$setUnion": [
                                    {"$ifNull": ["$sources", []]},
                                    [source_document_id],
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


# ---------------------------------------------------------------------------
# 6. Orchestration helper
# ---------------------------------------------------------------------------


async def extract_and_store(
    llm: BaseLLM,
    *,
    document_id: PydanticObjectId,
    content: str,
    source_type: str,
    source_uri: str,
    date: str | None = None,
    reference_uris: list[str] | None = None,
    database: str,
    client: Any,
) -> ExtractionResult:
    """End-to-end: chunk → extract → structural → normalise → upsert."""

    # 1. Chunk
    chunks = chunk_document(content)
    if not chunks:
        logger.warning("No chunks produced for document %s", document_id)
        return ExtractionResult()

    # Assign a unique id per chunk for provenance tracking.
    chunk_ids = [str(uuid4()) for _ in chunks]

    # 2. LLM extraction per chunk (parallel, capped by semaphore).
    semaphore = asyncio.Semaphore(app_config.extraction.llm_concurrency)

    async def _extract(chunk: str, chunk_id: str) -> ExtractionResult:
        async with semaphore:
            return await extract_entities(llm, chunk, chunk_id=chunk_id)

    results = await asyncio.gather(
        *[_extract(chunk, cid) for chunk, cid in zip(chunks, chunk_ids)]
    )

    llm_result = ExtractionResult()
    for r in results:
        llm_result = llm_result.merge(r)

    # 3. Structural entries
    structural = build_structural_entries(
        document_id=document_id,
        source_type=source_type,
        source_uri=source_uri,
        date=date,
        chunk_texts=chunks,
        chunk_ids=chunk_ids,
        extracted=llm_result,
        reference_uris=reference_uris,
    )

    # 4. Combine and normalise (with cross-document resolution)
    combined = llm_result.merge(structural)
    normalised = await normalize_nodes(combined, client=client, database=database)

    # 5. Upsert to knowledge_graph
    await upsert_graph_entries(
        normalised,
        source_document_id=document_id,
        database=database,
        client=client,
    )

    return normalised
