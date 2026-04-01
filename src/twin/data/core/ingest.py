"""
URL dispatcher for data ingestion.

Routes a URL to the appropriate data pipeline based on domain matching.
New pipelines register their URL pattern here to be automatically
available via the MCP ``ingest_url`` tool.
"""

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from twin.entities.documents import Document

logger = logging.getLogger(__name__)


async def _ingest_substack_article(url: str) -> Document | None:
    """Ingest a Substack article via the Substack article pipeline."""

    from twin.data.substack.substack_article_pipeline import (
        ingest_substack_article,
    )

    return await ingest_substack_article(url)


# Registry: (domain_substring, handler).
# Order matters — first match wins.
_URL_HANDLERS: list[tuple[str, Callable[[str], Awaitable[Document | None]]]] = [
    ("substack.com", _ingest_substack_article),
]


async def ingest_url(url: str) -> Document | None:
    """Route a URL to the appropriate data pipeline and ingest it.

    Returns the persisted Document, or None if the URL was a duplicate.

    Raises:
        ValueError: If no data pipeline is registered for the URL's domain.
    """

    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    for pattern, handler in _URL_HANDLERS:
        if pattern in domain:
            logger.info("Routing URL to '%s' pipeline: %s", pattern, url)
            return await handler(url)

    supported = [p for p, _ in _URL_HANDLERS]
    raise ValueError(
        f"No data pipeline registered for domain '{domain}'. "
        f"Supported domains: {', '.join(supported)}"
    )
