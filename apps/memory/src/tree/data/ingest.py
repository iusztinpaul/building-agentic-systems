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

import functools
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from beanie import PydanticObjectId

from tree.config.app_config import (
    SubstackArticleSource,
    SubstackRssSource,
    app_config,
)
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


_SUPPORTED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


async def _ingest_substack_article(
    url: str, user_id: PydanticObjectId
) -> Document | None:
    """Ingest a Substack article via the Substack article pipeline."""

    from tree.data.substack.substack_article_pipeline import (
        ingest_substack_article,
    )

    return await ingest_substack_article(url, user_id)


async def _ingest_youtube_video(url: str, user_id: PydanticObjectId) -> Document | None:
    """Ingest a single YouTube video URL via the YouTube video pipeline.

    Note: ``ingest_url`` is single-document by contract, so YouTube channel
    RSS feeds (which yield many documents) are NOT routed here — they are
    rejected up-front in :func:`ingest_url` with a clear ``ValueError``.
    """

    from tree.data.youtube.youtube_video_pipeline import ingest_youtube_video

    return await ingest_youtube_video(url, user_id)


async def _ingest_web_url(url: str, user_id: PydanticObjectId) -> Document | None:
    """Ingest an arbitrary URL via the generic web (Bright Data) pipeline."""

    from tree.data.web.web_pipeline import ingest_web_url

    return await ingest_web_url(url, user_id)


# Registry: (domain_substring, handler).
# Order matters — first match wins. YouTube hosts are listed before
# ``substack.com`` (and therefore before the custom-Substack-domain
# fallback below) so YouTube URLs always route to the YouTube handler.
_URL_HANDLERS: list[
    tuple[str, Callable[[str, PydanticObjectId], Awaitable[Document | None]]]
] = [
    ("youtube.com", _ingest_youtube_video),
    ("youtu.be", _ingest_youtube_video),
    ("substack.com", _ingest_substack_article),
]


@functools.cache
def _get_configured_substack_domains() -> set[str]:
    """Extract unique bare domains from configured Substack sources.

    Walks the flat ``app_config.sources.sources`` list and collects the host
    (lower-cased, ``www.`` stripped) of every entry typed as
    ``substack_rss`` or ``substack_article``. Entries of any other type
    (``web``, ``huggingface_dataset``, ...) are ignored — even if their
    ``uri`` happens to look like a Substack custom domain.

    Entries whose ``uri`` does not parse to a host (e.g. a HuggingFace
    dataset id) contribute nothing to the set.
    """

    domains: set[str] = set()
    for entry in app_config.sources.sources:
        if not isinstance(entry, (SubstackRssSource, SubstackArticleSource)):
            continue
        parsed = urlparse(entry.uri)
        if parsed.netloc:
            domains.add(parsed.netloc.lower().removeprefix("www."))
    return domains


async def ingest_url(url: str, user_id: PydanticObjectId) -> Document | None:
    """Route a URL to the appropriate data pipeline and ingest it for ``user_id``.

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
    bare_domain_for_guard = domain.removeprefix("www.")

    # Guard: a YouTube channel RSS feed is feed-shaped (many docs), but
    # ``ingest_url`` returns a single Document. Reject up-front with a
    # message that points the user at the right config knob.
    if bare_domain_for_guard in {"youtube.com", "m.youtube.com"} and (
        parsed.path == "/feeds/videos.xml"
    ):
        raise ValueError(
            "RSS feed URLs are not supported by ingest_url; configure them as "
            "'youtube_rss' in app config."
        )

    # Static registry match.
    for pattern, handler in _URL_HANDLERS:
        if pattern in domain:
            logger.info("Routing URL to '%s' pipeline: %s", pattern, url)
            return await handler(url, user_id)

    # Custom Substack domain match.
    bare_domain = domain.removeprefix("www.")
    if bare_domain in _get_configured_substack_domains():
        logger.info("Routing URL to 'substack (custom domain)' pipeline: %s", url)
        return await _ingest_substack_article(url, user_id)

    # Fallback: generic web pipeline (Bright Data Web Unlocker).
    logger.info("Routing URL to 'web (Bright Data fallback)' pipeline: %s", url)
    return await _ingest_web_url(url, user_id)
