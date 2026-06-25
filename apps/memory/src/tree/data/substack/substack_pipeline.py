"""Single-article Substack path — scrape core + thin MCP flow.

The article path SCRAPES each URL (Extract+Transform fuse — one scrape yields the
Document). ``_ingest_substack_article_one`` is the plain async core: the pure
``substack_article.fetch_and_extract`` (scrape) then the SHARED
``substack_article.load_article_document`` (which delegates to
``substack_rss.load_document``). ``ingest_substack_article`` is a THIN 1-line @flow
wrapper used ONLY by the MCP URL router (``tree.data.online_pipeline``).

The unified batch (``substack_pipeline_batch.ingest_substack_batch``) reuses the same
``fetch_and_extract`` + ``load_document`` for its single-article entries, so both the
online and offline article paths build + load identically.
"""

import logging

from beanie import PydanticObjectId
from prefect import flow

from tree.data.substack.substack_article import (
    fetch_and_extract,
    load_article_document,
)
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


async def _ingest_substack_article_one(
    article_url: str, user_id: PydanticObjectId
) -> Document | None:
    """Scrape + persist a single Substack article (plain async core, NO decorators).

    Fetches and extracts the article via the pure ``fetch_and_extract``, then loads it
    via the SHARED ``load_article_document``. Returns the persisted Document, or
    ``None`` if it was a duplicate. Shared by the thin MCP flow (one URL) — the batch
    path uses the batch tasks instead.
    """

    doc, body_html = await fetch_and_extract(article_url, user_id)
    result = await load_article_document(doc, body_html)

    if result:
        logger.info("Ingested article: %s", article_url)
    else:
        logger.info("Skipped duplicate article: %s", article_url)

    return result


@flow(name="ingest-substack-article-etl", log_prints=True)
async def ingest_substack_article(
    article_url: str, user_id: PydanticObjectId
) -> Document | None:
    """Thin MCP-only @flow: ingest ONE article via the core.

    The MCP ``ingest_url`` router (``tree.data.online_pipeline._ingest_substack_article``) calls
    this so single-URL ingest still gets its own Prefect flow run + Opik trace. The
    BATCH path does NOT call this — it runs the batch tasks directly.
    """

    return await _ingest_substack_article_one(article_url, user_id)
