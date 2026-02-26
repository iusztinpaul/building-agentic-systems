"""
Prefect workflow orchestrator.

Registers and serves all workflow deployments.
The Prefect server runs via Docker Compose (make local-start).

Usage:
    make local-start       # start infra (MongoDB + Prefect)
    make serve-workflows   # serve workflow deployments
"""

from prefect import serve

from twin.data.substack.substack_rss_pipeline import (
    ingest_substack_rss_feed,
    ingest_substack_rss_feed_batch,
)
from twin.memory.extraction.pipeline import memory_extraction

if __name__ == "__main__":
    serve(
        ingest_substack_rss_feed.to_deployment(
            name="ingest-substack-rss-feed-etl",
            tags=["data-pipeline", "substack"],
        ),
        ingest_substack_rss_feed_batch.to_deployment(
            name="ingest-substack-rss-feed-batch-etl",
            tags=["data-pipeline", "substack"],
        ),
        memory_extraction.to_deployment(
            name="memory-extraction-etl",
            tags=["memory-pipeline", "extraction"],
        ),
    )
