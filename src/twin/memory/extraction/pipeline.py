"""
Prefect tasks and flows for the memory extraction pipeline.

Thin wrappers around core.py that add retries, logging, and DB init.
"""

import logging

from beanie import PydanticObjectId
from prefect import flow, task
from prefect.cache_policies import NO_CACHE
from pymongo import AsyncMongoClient

from twin.config.settings import settings
from twin.db import init_mongodb
from twin.entities.documents import Document
from twin.memory.extraction.core import extract_and_store
from twin.memory.types import ExtractionResult
from twin.models.base import BaseLLM
from twin.models.get_model import get_llm

logger = logging.getLogger(__name__)


@task(
    name="extract-document-to-kg",
    retries=1,
    retry_delay_seconds=10,
    cache_policy=NO_CACHE,
)
async def extract_document_task(
    llm: BaseLLM,
    doc: Document,
    client: AsyncMongoClient,
    database: str,
) -> ExtractionResult:
    """Extract knowledge graph entries from a single document."""

    if not doc.content:
        logger.warning("Document %s has no content, skipping", doc.id)
        return ExtractionResult()

    # Resolve reference URIs from the populated Document.references links.
    reference_uris = [
        ref.source_uri for ref in doc.references if isinstance(ref, Document)
    ]

    return await extract_and_store(
        llm,
        document_id=doc.id,
        content=doc.content,
        source_type=doc.source_type.value,
        source_uri=doc.source_uri,
        date=doc.date.isoformat() if doc.date else None,
        reference_uris=reference_uris or None,
        database=database,
        client=client,
    )


@flow(name="memory-extraction-etl", log_prints=True)
async def memory_extraction(
    document_ids: list[str] | None = None,
) -> list[ExtractionResult]:
    """Extract knowledge graph entries from documents.

    Args:
        document_ids: Optional list of document ObjectId strings to process.
                     If None, processes all documents that have content.
    """

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = settings.mongo.mongo_initdb_database

    llm = get_llm()

    if document_ids:
        docs = await Document.find(
            {"_id": {"$in": [PydanticObjectId(did) for did in document_ids]}}
        ).to_list()
    else:
        docs = await Document.find(
            {"content": {"$ne": None}},
        ).to_list()

    logger.info("Processing %d documents for KG extraction", len(docs))

    results: list[ExtractionResult] = []
    for doc in docs:
        result = await extract_document_task(llm, doc, client, database)
        results.append(result)

    total_nodes = sum(len(r.nodes) for r in results)
    total_edges = sum(len(r.edges) for r in results)
    logger.info(
        "Extraction complete: %d documents → %d nodes, %d edges",
        len(docs),
        total_nodes,
        total_edges,
    )

    return results
