"""
Prefect workflow orchestrator.

Registers and serves all workflow deployments.
The Prefect server runs via Docker Compose (make local-start).

Usage:
    make local-start       # start infra (MongoDB + Prefect)
    make serve-workflows   # serve workflow deployments
"""

from prefect import serve

from twin.data.conversation_pipeline import ingest_conversation
from twin.data.file_pipeline import ingest_file
from twin.data.huggingface.arxiv_dataset_pipeline import ingest_arxiv_dataset
from twin.data.pipeline import ingest_all_data
from twin.data.substack.substack_article_pipeline import (
    ingest_substack_article,
    ingest_substack_article_batch,
)
from twin.data.substack.substack_rss_pipeline import (
    ingest_substack_rss_feed,
    ingest_substack_rss_feed_batch,
)
from twin.memory.extraction.pipeline import memory_extraction
from twin.memory.indexing.pipeline import memory_indexing

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
        ingest_arxiv_dataset.to_deployment(
            name="ingest-arxiv-dataset-etl",
            tags=["data-pipeline", "huggingface"],
        ),
        memory_extraction.to_deployment(
            name="memory-extraction-etl",
            tags=["memory-pipeline", "extraction"],
        ),
        ingest_substack_article.to_deployment(
            name="ingest-substack-article-etl",
            tags=["data-pipeline", "substack"],
        ),
        ingest_substack_article_batch.to_deployment(
            name="ingest-substack-article-batch-etl",
            tags=["data-pipeline", "substack"],
        ),
        ingest_all_data.to_deployment(
            name="ingest-all-data-etl",
            tags=["data-pipeline"],
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
