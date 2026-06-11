"""
Prefect workflow orchestrator.

Registers and serves all workflow deployments. Every flow now exposes
``user_id`` as a required, non-Optional parameter — operators MUST pass
it when triggering the deployment, e.g.::

    prefect deployment run \\
        memory-extract-etl-orchestrator/memory-extract-etl-orchestrator \\
        -p user_id=507f1f77bcf86cd799439011

Omitting ``user_id`` raises a ``TypeError`` at flow entry before any
side-effects happen.

The Prefect server runs via Docker Compose (make local-start).

Usage:
    make local-start       # start infra (MongoDB + Prefect)
    make serve-workflows   # serve workflow deployments
"""

import inspect

from prefect import serve

from tree.config.app_config import app_config
from tree.data.pipeline import data_etl_orchestrator, data_etl_worker
from tree.memory.extraction.pipeline import (
    memory_extract_etl_orchestrator,
    memory_extract_etl_worker,
)
from tree.memory.indexing.pipeline import memory_indexing
from tree.observability import configure_opik

# --- [Prefect Cloud free-tier cap: 5 deployments] --------------------------
# The free tier allows only 5 deployments per workspace, so the five flows
# below are temporarily not served. Re-enable (uncomment these imports AND the
# matching ``.to_deployment(...)`` blocks in ``serve_deployments``) once the
# Cloud plan is upgraded.
# from tree.data.conversation_pipeline import ingest_conversation
# from tree.data.file_pipeline import ingest_file
# from tree.data.youtube.youtube_rss_pipeline import ingest_youtube_rss_feed_batch
# from tree.data.youtube.youtube_video_pipeline import ingest_youtube_video_batch
# from tree.memory.consolidation.dream import dream_consolidation_all_users
# ---------------------------------------------------------------------------


def build_deployments() -> list:
    """Build (but neither serve nor apply) the workflow ``RunnerDeployment`` set.

    Single source of truth for the deployment topology, consumed by two callers:

    * :func:`serve_deployments` — the long-running worker (``make memory-serve-workflows``)
      that registers AND executes these via the in-process Prefect runner.
    * :func:`apply_deployments` — the CD path (``deploy/prefect_pipelines.py``) that
      only registers/updates the definitions on the configured Prefect API
      (Prefect Cloud) without serving, so pushing to ``main`` keeps the Cloud
      deployments in sync with the code while the worker runs elsewhere.

    Keeping construction here means both paths can never drift apart.
    """

    return [
        # Data ingestion orchestrator/worker split (#068 / ADR-002 §3 amended #066).
        # Operators trigger the ORCHESTRATOR; it reads the configured ``sources:``
        # list, partitions it into ``min(num_shards, N)`` balanced shards, and
        # dispatches one ``data-etl-worker`` run per shard (NO recursion — a distinct
        # worker deployment). There is NO trailing step — the data pipeline only
        # produces ``documents``; there is no index. The WORKER ingests one shard of
        # sources (reusing the per-source-type batch logic); it is the orchestrator's
        # internal dispatch target but may also be triggered directly for a bare
        # shard ingestion.
        data_etl_orchestrator.to_deployment(
            name="data-etl-orchestrator",
            tags=["data-pipeline", "orchestrator"],
        ),
        data_etl_worker.to_deployment(
            name="data-etl-worker",
            tags=["data-pipeline", "worker"],
        ),
        # Memory extraction orchestrator/worker split (#067 / ADR-002 §3 amended
        # #066). Operators trigger the ORCHESTRATOR; it resolves the user's pending
        # docs, partitions them into ``min(num_shards, N)`` shards, dispatches one
        # ``memory-extract-etl-worker`` run per shard (NO recursion — a distinct
        # worker deployment), then one trailing ``memory-indexing-etl`` run. The
        # WORKER is the pure six-task extraction body (no fan-out, no indexing); it
        # is the orchestrator's internal dispatch target but may also be triggered
        # directly for a bare extraction with no index.
        memory_extract_etl_orchestrator.to_deployment(
            name="memory-extract-etl-orchestrator",
            tags=["memory-pipeline", "extraction", "orchestrator"],
        ),
        memory_extract_etl_worker.to_deployment(
            name="memory-extract-etl-worker",
            tags=["memory-pipeline", "extraction", "worker"],
        ),
        memory_indexing.to_deployment(
            name="memory-indexing-etl",
            tags=["memory-pipeline", "indexing"],
        ),
        # --- [Prefect Cloud free-tier cap: 5 deployments] ------------------
        # The five deployments below exceed the free tier's 5-deployment
        # ceiling and are temporarily disabled. Re-enable them (and the
        # matching imports above) once the Cloud plan is upgraded.
        # ingest_file.to_deployment(
        #     name="ingest-file-etl",
        #     tags=["data-pipeline", "file"],
        # ),
        # ingest_conversation.to_deployment(
        #     name="ingest-conversation-etl",
        #     tags=["data-pipeline", "conversation"],
        # ),
        # ingest_youtube_video_batch.to_deployment(
        #     name="ingest-youtube-video-batch-etl",
        #     tags=["data-pipeline", "youtube"],
        # ),
        # ingest_youtube_rss_feed_batch.to_deployment(
        #     name="ingest-youtube-rss-feed-batch-etl",
        #     tags=["data-pipeline", "youtube"],
        # ),
        # Scheduled dream-consolidation fan-out: one cron, fans out one
        # per-user dream run across every active user (#052). The parent
        # flow takes no ``user_id`` — it enumerates active users itself.
        # dream_consolidation_all_users.to_deployment(
        #     name="dream-consolidation-etl",
        #     cron=app_config.dream.cron,
        #     tags=["dream"],
        # ),
        # -------------------------------------------------------------------
    ]


def serve_deployments(limit: int) -> None:
    """Register and serve every workflow deployment with admission control.

    Extracted from ``__main__`` so the ``serve(...)`` invocation is importable
    and unit-testable (#065). ``limit`` is forwarded to ``prefect.serve`` as its
    ``limit`` parameter — admission control (ADR-002 §4) capping how many flow
    runs the server admits concurrently, kept close to ``concurrency.voyage_rpm``
    so we never admit far more runs than the shared embed budget can feed.

    Configures Opik observability once at serve startup (no-op without
    ``OPIK_API_KEY``) so pipeline task spans / cost are recorded across every
    flow run this worker executes.
    """

    configure_opik()
    serve(*build_deployments(), limit=limit)


async def apply_deployments() -> list[str]:
    """Register/update the deployments on the configured Prefect API, no serving.

    This is the CD entrypoint (``deploy/prefect_pipelines.py``): on every push to
    ``main`` it upserts each deployment definition against ``PREFECT_API_URL``
    (Prefect Cloud) via ``RunnerDeployment.aapply()`` and exits — it does NOT
    block on a runner. The long-running worker (``serve_deployments``) executes
    runs separately; CD only keeps the Cloud-side definitions in lock-step with
    the code. Returns the applied deployment ids (for logging / assertions).
    """

    deployment_ids: list[str] = []
    for deployment in build_deployments():
        # ``flow.to_deployment`` returns a coroutine when called inside a running
        # event loop (Prefect async-dispatch); in the sync serve path it returns
        # the RunnerDeployment directly. Resolve either form before applying.
        if inspect.isawaitable(deployment):
            deployment = await deployment
        deployment_ids.append(await deployment.aapply())
    return deployment_ids


if __name__ == "__main__":
    serve_deployments(app_config.concurrency.runner_global_limit)
