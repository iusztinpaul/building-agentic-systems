"""
Extraction-pipeline submission glue.

:func:`submit_ingestion` turns an ingested :class:`Document` into
knowledge-graph content asynchronously: it fires the
``memory-extract-etl-coordinator`` Prefect deployment for the document and
returns immediately. Nothing in the caller's request path blocks on the
multi-minute extraction/embedding/indexing pipeline; a served worker executes
the run out-of-band. Called by ``tree.online.data_etl_online`` (the realtime
ingest flow) after the data step lands the document.
"""

import logging
from typing import Any

from beanie import PydanticObjectId
from prefect.client.orchestration import get_client

from tree.entities.documents import Document

logger = logging.getLogger(__name__)


async def submit_ingestion(
    document: Document, *, user_id: PydanticObjectId
) -> dict[str, Any]:
    """Submit extraction + indexing for ``document`` to Prefect; return at once.

    Creates a flow run for the ``memory-extract-etl-coordinator`` deployment
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
                "memory-extract-etl-coordinator/memory-extract-etl-coordinator"
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
