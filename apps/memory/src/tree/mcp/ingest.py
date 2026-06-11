"""
MCP ingestion orchestration.

Two ways to turn an ingested :class:`Document` into knowledge-graph content:

* :func:`submit_ingestion` (DEFAULT for the MCP tools) — fire the
  ``memory-extract-etl-orchestrator`` Prefect deployment for the document and
  return immediately. The MCP server runs on a serverless host with a tight
  request budget, so it must not block on the multi-minute
  extraction/embedding/indexing pipeline; a served worker executes the run
  out-of-band.
* :func:`run_ingestion_pipeline` — run the same extraction + indexing
  in-process and block until done. Kept for non-serverless / scripted callers
  that genuinely want the result synchronously.
"""

import logging
from typing import Any

from beanie import PydanticObjectId
from prefect.client.orchestration import get_client

from tree.entities.documents import Document
from tree.memory.extraction.pipeline import run_extraction_for_documents
from tree.memory.indexing.core import embed_nodes
from tree.models.base import BaseEmbeddingModel, BaseLLM

logger = logging.getLogger(__name__)

# Operators always trigger the ORCHESTRATOR (extraction fan-out + trailing
# index); for a single just-ingested document we scope it via ``document_ids``.
# Mirrors ``scripts/run_memory_pipeline.py``.
_EXTRACT_ORCHESTRATOR_DEPLOYMENT = (
    "memory-extract-etl-orchestrator/memory-extract-etl-orchestrator"
)


async def submit_ingestion(
    document: Document, *, user_id: PydanticObjectId
) -> dict[str, Any]:
    """Submit extraction + indexing for ``document`` to Prefect; return at once.

    Creates a flow run for the ``memory-extract-etl-orchestrator`` deployment
    scoped to this single document and returns WITHOUT waiting for it to finish
    (async ingestion). The return dict reports whether the submission was
    accepted:

    * ``{"status": "submitted", "flow_run_id": ...}`` — Prefect accepted the run.
    * ``{"status": "not_submitted", "error" | "reason": ...}`` — the document had
      no content, or the Prefect API was unreachable / the deployment isn't
      registered (e.g. no worker has served it yet).

    A served worker (``make memory-serve-workflows``) picks up and executes the
    run; nothing in the MCP request path blocks on the heavy pipeline.
    """

    base = {
        "document_id": str(document.id),
        "source_uri": document.source_uri,
        "title": document.title,
    }
    if not document.content:
        logger.warning("Document %s has no content; nothing to submit", document.id)
        return {"status": "not_submitted", "reason": "empty_content", **base}

    try:
        async with get_client() as client:
            deployment = await client.read_deployment_by_name(
                _EXTRACT_ORCHESTRATOR_DEPLOYMENT
            )
            flow_run = await client.create_flow_run_from_deployment(
                deployment_id=deployment.id,
                parameters={
                    "user_id": str(user_id),
                    "document_ids": [str(document.id)],
                },
            )
    except Exception as exc:  # noqa: BLE001 — any Prefect/transport failure → not_submitted.
        logger.exception("Failed to submit ingestion for document %s", document.id)
        return {"status": "not_submitted", "error": str(exc), **base}

    logger.info(
        "Submitted ingestion for document %s as flow run %s", document.id, flow_run.id
    )
    return {"status": "submitted", "flow_run_id": str(flow_run.id), **base}


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
