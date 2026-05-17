"""
MCP ingestion orchestration.

Runs the memory extraction and indexing core functions on an ingested
Document, making its content queryable in the knowledge graph. The
extraction step delegates to the same six-task pipeline that the Prefect
flow drives — invoked here as a plain async helper so we don't start a
flow run from inside the MCP server process.
"""

import logging
from typing import Any

from beanie import PydanticObjectId

from tree.entities.documents import Document
from tree.memory.extraction.pipeline import run_extraction_for_documents
from tree.memory.indexing.core import embed_nodes
from tree.models.base import BaseEmbeddingModel, BaseLLM

logger = logging.getLogger(__name__)


async def run_ingestion_pipeline(
    document: Document,
    *,
    client: Any,
    database: str,
    llm: BaseLLM,
    embedding_model: BaseEmbeddingModel,
    user_id: PydanticObjectId,
) -> dict[str, Any]:
    """Run memory extraction and indexing on a Document.

    Calls the same six-task extraction pipeline (in-process) and then the
    indexing core to fill any embedding gaps. ``llm`` and ``embedding_model``
    are caller-owned (constructed once in the FastMCP lifespan) so we don't
    re-instantiate per request.

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

    summary = await run_extraction_for_documents(
        [str(document.id)],
        user_id=user_id,
        client=client,
        database_name=database,
        llm=llm,
        embedding_model=embedding_model,
    )

    await embed_nodes(client, database, embedding_model, user_id)

    return {
        "status": "ingested",
        "document_id": str(document.id),
        "source_uri": document.source_uri,
        "title": document.title,
        "nodes_extracted": summary.nodes_written,
        "edges_extracted": summary.edges_written,
    }
