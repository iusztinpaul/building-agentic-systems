"""
URL dispatcher for data ingestion.

Routes a URL to the appropriate data pipeline based on domain matching.
New pipelines register their URL pattern here to be automatically
available via the MCP ``ingest_url`` tool.
"""

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from twin.config.app_config import app_config
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


def _get_configured_substack_domains() -> set[str]:
    """Extract unique domains from configured Substack sources."""

    domains: set[str] = set()
    for feed_url in app_config.sources.substack:
        parsed = urlparse(feed_url)
        if parsed.netloc:
            domains.add(parsed.netloc.lower().removeprefix("www."))
    for article_url in app_config.sources.substack_articles:
        parsed = urlparse(article_url)
        if parsed.netloc:
            domains.add(parsed.netloc.lower().removeprefix("www."))
    return domains


# Custom Substack domains (e.g. decodingai.com) derived from config.
_SUBSTACK_CUSTOM_DOMAINS: set[str] = _get_configured_substack_domains()


async def ingest_url(url: str) -> Document | None:
    """Route a URL to the appropriate data pipeline and ingest it.

    Matches against:
    1. Static registry patterns (e.g. ``substack.com``).
    2. Custom Substack domains derived from ``app_config.sources``.

    Returns the persisted Document, or None if the URL was a duplicate.

    Raises:
        ValueError: If no data pipeline is registered for the URL's domain.
    """

    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    # Static registry match.
    for pattern, handler in _URL_HANDLERS:
        if pattern in domain:
            logger.info("Routing URL to '%s' pipeline: %s", pattern, url)
            return await handler(url)

    # Custom Substack domain match.
    bare_domain = domain.removeprefix("www.")
    if bare_domain in _SUBSTACK_CUSTOM_DOMAINS:
        logger.info("Routing URL to 'substack (custom domain)' pipeline: %s", url)
        return await _ingest_substack_article(url)

    supported = [p for p, _ in _URL_HANDLERS]
    supported.extend(sorted(_SUBSTACK_CUSTOM_DOMAINS))
    raise ValueError(
        f"No data pipeline registered for domain '{domain}'. "
        f"Supported domains: {', '.join(supported)}"
    )
