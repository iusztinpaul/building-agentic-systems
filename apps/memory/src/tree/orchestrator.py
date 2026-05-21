"""
Prefect workflow orchestrator.

Registers and serves all workflow deployments. Every flow now exposes
``user_id`` as a required, non-Optional parameter — operators MUST pass
it when triggering the deployment, e.g.::

    prefect deployment run memory-extraction-etl/memory-extraction-etl \\
        -p user_id=507f1f77bcf86cd799439011

Omitting ``user_id`` raises a ``TypeError`` at flow entry before any
side-effects happen.

The Prefect server runs via Docker Compose (make local-start).

Usage:
    make local-start       # start infra (MongoDB + Prefect)
    make serve-workflows   # serve workflow deployments
"""

from prefect import serve

from tree.config.app_config import app_config
from tree.data.conversation_pipeline import ingest_conversation
from tree.data.file_pipeline import ingest_file
from tree.data.pipeline import data_pipeline
from tree.data.youtube.youtube_rss_pipeline import ingest_youtube_rss_feed_batch
from tree.data.youtube.youtube_video_pipeline import ingest_youtube_video_batch
from tree.memory.consolidation.dream import dream_consolidation_all_users
from tree.memory.extraction.fanout import memory_extraction_sharded
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
        ingest_youtube_video_batch.to_deployment(
            name="ingest-youtube-video-batch-etl",
            tags=["data-pipeline", "youtube"],
        ),
        ingest_youtube_rss_feed_batch.to_deployment(
            name="ingest-youtube-rss-feed-batch-etl",
            tags=["data-pipeline", "youtube"],
        ),
        # Scheduled dream-consolidation fan-out: one cron, fans out one
        # per-user dream run across every active user (#052). The parent
        # flow takes no ``user_id`` — it enumerates active users itself.
        dream_consolidation_all_users.to_deployment(
            name="dream-consolidation-etl",
            cron=app_config.dream.cron,
            tags=["dream"],
        ),
        # Document-shard fan-out parent flow (#056 / ADR-002 §3): partitions
        # ONE user's pending documents into shards and launches one
        # ``memory-extraction-etl`` child run per shard, then a single
        # ``memory-indexing-etl`` run afterwards.
        memory_extraction_sharded.to_deployment(
            name="memory-extraction-fanout-etl",
            tags=["memory-pipeline", "extraction", "fanout"],
        ),
        # Admission control (ADR-002 §4): cap how many flow runs the server
        # admits concurrently, kept close to ``concurrency.voyage_rpm`` so we
        # never admit far more runs than the shared embed budget can feed.
        global_limit=app_config.concurrency.runner_global_limit,
    )
