"""
MCP ingestion orchestration.

Runs the memory extraction and indexing core functions on an ingested Document,
making its content queryable in the knowledge graph.
"""

import logging
from typing import Any

from twin.entities.documents import Document
from twin.memory.extraction.core import extract_and_store
from twin.memory.indexing.core import embed_nodes
from twin.models.base import BaseEmbeddingModel, BaseLLM

logger = logging.getLogger(__name__)


async def run_ingestion_pipeline(
    document: Document,
    *,
    client: Any,
    database: str,
    llm: BaseLLM,
    embedding_model: BaseEmbeddingModel,
) -> dict[str, Any]:
    """Run memory extraction and indexing on a Document.

    Calls extraction and indexing core functions with the provided
    dependencies (from the MCP lifespan context).

    Returns a summary dict with extraction counts.
    """

    if not document.content:
        logger.warning("Document %s has no content, skipping extraction", document.id)
        return {
            "status": "ingested",
            "document_id": str(document.id),
            "source_uri": document.source_uri,
            "title": document.title,
            "nodes_extracted": 0,
            "edges_extracted": 0,
        }

    # Resolve reference URIs from the Document.
    reference_uris = [
        ref.source_uri for ref in document.references if isinstance(ref, Document)
    ]

    result = await extract_and_store(
        llm,
        document_id=document.id,
        content=document.content,
        source_type=document.source_type.value,
        source_uri=document.source_uri,
        date=document.date.isoformat() if document.date else None,
        reference_uris=reference_uris or None,
        database=database,
        client=client,
    )

    await embed_nodes(client, database, embedding_model)

    return {
        "status": "ingested",
        "document_id": str(document.id),
        "source_uri": document.source_uri,
        "title": document.title,
        "nodes_extracted": len(result.nodes),
        "edges_extracted": len(result.edges),
    }
