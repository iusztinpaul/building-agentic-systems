"""
Prefect workflow orchestrator.

Registers and serves all workflow deployments.
The Prefect server runs via Docker Compose (make local-start).

Usage:
    make local-start       # start infra (MongoDB + Prefect)
    make serve-workflows   # serve workflow deployments
"""

from prefect import serve

from tree.data.conversation_pipeline import ingest_conversation
from tree.data.file_pipeline import ingest_file
from tree.data.pipeline import data_pipeline
from tree.memory.extraction.pipeline import memory_extraction
from tree.memory.indexing.pipeline import memory_indexing

if __name__ == "__main__":
    serve(
        data_pipeline.to_deployment(
            name="data-pipeline-etl",
            tags=["data-pipeline"],
        ),
        memory_extraction.to_deployment(
            name="memory-extraction-etl",
            tags=["memory-pipeline", "extraction"],
        ),
        memory_indexing.to_deployment(
            name="memory-indexing-etl",
            tags=["memory-pipeline", "indexing"],
        ),
        ingest_file.to_deployment(
            name="ingest-file-etl",
            tags=["data-pipeline", "file"],
        ),
        ingest_conversation.to_deployment(
            name="ingest-conversation-etl",
            tags=["data-pipeline", "conversation"],
        ),
    )
