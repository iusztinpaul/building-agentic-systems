"""
URL dispatcher for data ingestion.

Routes a URL to the appropriate data pipeline based on domain matching.
New pipelines register their URL pattern here to be automatically
available via the MCP ``ingest_url`` tool.

Match order:
1. Static registry ``_URL_HANDLERS`` (e.g. ``substack.com``).
2. Custom Substack domains derived from ``app_config.sources``.
3. Fallback: the generic web pipeline backed by Bright Data Web Unlocker.
"""

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from tree.config.app_config import app_config
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


_SUPPORTED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


async def _ingest_substack_article(url: str) -> Document | None:
    """Ingest a Substack article via the Substack article pipeline."""

    from tree.data.substack.substack_article_pipeline import (
        ingest_substack_article,
    )

    return await ingest_substack_article(url)


async def _ingest_web_url(url: str) -> Document | None:
    """Ingest an arbitrary URL via the generic web (Bright Data) pipeline."""

    from tree.data.web.web_pipeline import ingest_web_url

    return await ingest_web_url(url)


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
    3. Fallback: the generic web pipeline (Bright Data Web Unlocker).

    Returns the persisted Document, or None if the URL was a duplicate.

    Raises:
        ValueError: If ``url`` is empty, its scheme is not ``http``/``https``,
            or it is missing a host component.
    """

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in _SUPPORTED_SCHEMES:
        raise ValueError(
            f"Unsupported URL scheme '{scheme}': only http and https are accepted "
            f"(got {url!r})."
        )

    if not parsed.netloc:
        raise ValueError(f"URL is missing a host: {url!r}")

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

    # Fallback: generic web pipeline (Bright Data Web Unlocker).
    logger.info("Routing URL to 'web (Bright Data fallback)' pipeline: %s", url)
    return await _ingest_web_url(url)
