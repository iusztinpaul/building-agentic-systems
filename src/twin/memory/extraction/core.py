"""
Core business logic for knowledge-graph extraction.

Pure functions (aside from the LLM call and DB writes) that:
1. Chunk a document into token-bounded pieces.
2. Ask an LLM to extract nodes & edges per chunk.
3. Build structural entries (PART_OF, NEXT, MENTIONS) deterministically.
4. Normalise duplicate nodes via fuzzy matching.
5. Upsert the result to the knowledge_graph collection.
"""

import asyncio
import json
import logging
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
# 4. Normalisation (fuzzy dedup)
# ---------------------------------------------------------------------------


def normalize_nodes(result: ExtractionResult) -> ExtractionResult:
    """Merge near-duplicate nodes by fuzzy-matching names within each type."""

    # Map (type, original_name) → canonical_name
    canonical_map: dict[tuple[NodeType, str], str] = {}
    kept_nodes: list[ExtractedNode] = []

    for node in result.nodes:
        key = (node.type, node.name)
        if key in canonical_map:
            continue

        # Try to match against already-kept nodes of the same type.
        matched = False
        for kept in kept_nodes:
            if kept.type != node.type:
                continue
            ratio = SequenceMatcher(None, node.name, kept.name).ratio()
            if ratio >= app_config.extraction.similarity_threshold:
                canonical_map[key] = kept.name
                # Merge properties (kept node wins on conflicts).
                kept.properties = {**node.properties, **kept.properties}
                matched = True
                break

        if not matched:
            canonical_map[key] = node.name
            kept_nodes.append(node)

    # Rewrite edge endpoints using the canonical map.
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

    return ExtractionResult(nodes=kept_nodes, edges=remapped_edges)


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

    Returns the number of upsert operations executed.
    """

    now = datetime.now(tz=UTC)
    collection = client[database][_KG_COLLECTION]
    ops: list[UpdateOne] = []

    for node in result.nodes:
        node_id = build_node_id(node.type, node.name)
        ops.append(
            UpdateOne(
                {"_id": node_id},
                {
                    "$set": {
                        "kind": "node",
                        "type": node.type.value,
                        "name": node.name,
                        "properties": node.properties,
                    },
                    "$addToSet": {"sources": source_document_id},
                    "$min": {"created_at": now},
                    "$max": {"updated_at": now},
                    "$setOnInsert": {"embedding": []},
                },
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
                {
                    "$set": {
                        "kind": "edge",
                        "type": edge.type.value,
                        "source_node_id": src_id,
                        "source_type": edge.source_type.value,
                        "target_node_id": tgt_id,
                        "target_type": edge.target_type.value,
                        "properties": edge.properties,
                    },
                    "$addToSet": {"sources": source_document_id},
                    "$min": {"created_at": now},
                    "$max": {"updated_at": now},
                },
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

    # 4. Combine and normalise
    combined = llm_result.merge(structural)
    normalised = normalize_nodes(combined)

    # 5. Upsert to knowledge_graph
    await upsert_graph_entries(
        normalised,
        source_document_id=document_id,
        database=database,
        client=client,
    )

    return normalised
