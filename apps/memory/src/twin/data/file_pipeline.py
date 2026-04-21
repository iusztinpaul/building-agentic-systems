"""
Prefect tasks and flow for file ingestion.

Thin wrappers around file.py that add retries and logging.
"""

import logging

from prefect import flow, task

from twin.data.file import load_file_document
from twin.entities.documents import Document

logger = logging.getLogger(__name__)


@task(name="load-file-document", retries=1, retry_delay_seconds=2)
async def load_file_document_task(
    file_path: str,
    title: str | None = None,
) -> Document | None:
    return await load_file_document(file_path, title)


@flow(name="ingest-file-etl", log_prints=True)
async def ingest_file(
    file_path: str,
    title: str | None = None,
) -> Document | None:
    """Read a local file and ingest it as a Document.

    Assumes MongoDB/Beanie is already initialised by the caller
    (MCP lifespan, orchestrator, or batch flow).
    """

    result = await load_file_document_task(file_path, title)

    if result:
        logger.info("Ingested file: %s", file_path)
    else:
        logger.info("Skipped duplicate file: %s", file_path)

    return result
