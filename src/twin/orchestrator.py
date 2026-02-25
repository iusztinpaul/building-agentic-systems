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
    )
