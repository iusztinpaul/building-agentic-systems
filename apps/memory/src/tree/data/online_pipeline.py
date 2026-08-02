"""Realtime (online) DATA-pipeline step.

:func:`online_ingest` routes a typed :data:`OnlineSource` to its leaf pipeline
and returns the persisted :class:`Document` — it never triggers extraction.
The cross-pipeline orchestration (the ``online-pipeline`` flow + caller
dispatch) lives one level up in :mod:`tree.online`.
"""

import functools
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal, Union
from urllib.parse import urlparse

from beanie import PydanticObjectId
from prefect import tags
from pydantic import BaseModel, Field

from tree.config.sources import (
    SubstackArticleSource,
    SubstackRssSource,
)
from tree.config.sources import default_configured_sources
from tree.entities.documents import Document
from tree.config.constants import TAGS_DATA_ONLINE
from tree.observability import (
    configure_opik,
    pipeline_metadata,
    span,
)

logger = logging.getLogger(__name__)

# Opik tags + metadata for the online data pipeline's trace
_ONLINE_METADATA = pipeline_metadata("data")


_SUPPORTED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


async def _ingest_substack_article(
    url: str, user_id: PydanticObjectId
) -> Document | None:
    from tree.data.substack.substack_pipeline import (
        ingest_substack_article,
    )

    return await ingest_substack_article(url, user_id)


async def _ingest_youtube_video(url: str, user_id: PydanticObjectId) -> Document | None:
    """Ingest a single YouTube video URL via the YouTube video pipeline.

    Note: ``ingest_url`` is single-document by contract, so YouTube channel
    RSS feeds (which yield many documents) are NOT routed here — they are
    rejected up-front in :func:`ingest_url` with a clear ``ValueError``.
    """

    from tree.data.youtube.youtube_pipeline import ingest_youtube_video

    return await ingest_youtube_video(url, user_id)


async def _ingest_web_url(url: str, user_id: PydanticObjectId) -> Document | None:
    from tree.data.web.web_pipeline import ingest_web_url

    return await ingest_web_url(url, user_id)


# Registry: (domain_substring, handler).
# Order matters — first match wins.
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

    Walks the shared source loader's ``default_configured_sources()`` list
    (backfill + listen) and collects the host (lower-cased, ``www.`` stripped)
    of every entry typed as ``substack_rss`` or ``substack_article``. Entries of
    any other type (``web``, ``huggingface_dataset``, ...) are ignored — even if
    their ``uri`` happens to look like a Substack custom domain.

    Entries whose ``uri`` does not parse to a host (e.g. a HuggingFace
    dataset id) contribute nothing to the set.

    Iterates the loader's cached list read-only — never mutates it in place
    (the loader hands back its process-global cached object).
    """

    domains: set[str] = set()
    for entry in default_configured_sources():
        if not isinstance(entry, (SubstackRssSource, SubstackArticleSource)):
            continue
        parsed = urlparse(entry.uri)
        if parsed.netloc:
            domains.add(parsed.netloc.lower().removeprefix("www."))
    return domains


def validate_url(url: str) -> None:
    """Validate that ``url`` is ingestable — pure, no I/O.

    Shared by :func:`_ingest_url` (in the pipeline) and
    :func:`validate_online_source` (at the caller's edge, BEFORE submitting a
    deployment run) — so garbage fails synchronously with a clear message
    instead of becoming a remote flow-run failure nobody sees.

    Raises:
        ValueError: If the scheme is not ``http``/``https``, the host is
            missing, or the URL is a YouTube channel RSS feed (feed-shaped —
            many documents — while online ingest is single-document).
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

    bare_domain = parsed.netloc.lower().removeprefix("www.")

    # Guard: a YouTube channel RSS feed is feed-shaped (many docs), but
    # ``ingest_url`` returns a single Document. Reject up-front with a
    # message that points the user at the right config knob.
    if bare_domain in {"youtube.com", "m.youtube.com"} and (
        parsed.path == "/feeds/videos.xml"
    ):
        raise ValueError(
            "RSS feed URLs are not supported by ingest_url; configure them as "
            "'youtube_rss' in app config."
        )


async def _ingest_url(url: str, user_id: PydanticObjectId) -> Document | None:
    """Route a URL to the appropriate data pipeline and ingest it for ``user_id``.

    Matches against:
    1. Static registry patterns (e.g. ``substack.com``).
    2. Custom Substack domains derived from the shared source loader.
    3. Fallback: the generic web pipeline (Bright Data Web Unlocker).

    Returns the persisted Document, or None if the URL was a duplicate.

    Raises:
        ValueError: If ``url`` fails :func:`validate_url`.
    """

    validate_url(url)
    domain = urlparse(url).netloc.lower()

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


# ---------------------------------------------------------------------------
# Online ingestion orchestrator
# ---------------------------------------------------------------------------
# The realtime counterpart to ``offline_pipeline.offline_ingest_batch``. Each
# variant carries ONLY its own fields; ``online_ingest`` dispatches on ``type``
# (mirrors the offline ``SourceEntry`` discriminated union).


class UrlSource(BaseModel):
    """A realtime URL, routed by domain through :func:`ingest_url`."""

    type: Literal["url"] = "url"
    uri: str = Field(min_length=1)


class FileSource(BaseModel):
    """A file read at the CALLER's edge (CLI / MCP client) into a Document.

    ``path`` is identity only (source_uri + default title) — the pipeline
    never opens it; the file exists only on the caller's machine. ``content``
    is the payload, read there. Mirrors :class:`ConversationSource`'s
    identity/payload split (``session_uri`` / ``text``).
    """

    type: Literal["file"] = "file"
    path: str = Field(min_length=1)
    content: str = Field(min_length=1)
    title: str | None = None


class ConversationSource(BaseModel):
    """Conversation text captured from an agent session."""

    type: Literal["conversation"] = "conversation"
    text: str = Field(min_length=1)
    title: str | None = None
    session_uri: str | None = None
    session_started_at: datetime | None = None


OnlineSource = Annotated[
    Union[UrlSource, FileSource, ConversationSource],
    Field(discriminator="type"),
]


async def _ingest_file(
    source: FileSource, user_id: PydanticObjectId
) -> Document | None:
    from tree.data.file.file_pipeline import ingest_file

    return await ingest_file(source.path, source.content, user_id, source.title)


async def _ingest_conversation(
    source: ConversationSource, user_id: PydanticObjectId
) -> Document | None:
    from tree.data.conversation.conversation_pipeline import ingest_conversation

    return await ingest_conversation(
        source.text,
        user_id,
        title=source.title,
        session_uri=source.session_uri,
        session_started_at=source.session_started_at,
    )


async def online_ingest(
    source: OnlineSource, user_id: PydanticObjectId
) -> Document | None:
    """Route a realtime ``source`` to its leaf pipeline and ingest it for ``user_id``.

    The online counterpart to ``offline_ingest_batch``: distributes a single typed
    input (URL / file / conversation) to its leaf pipeline and returns the
    persisted Document, or ``None`` if it was a duplicate. Like the offline batch,
    it does NOT trigger extraction — the MCP layer submits that out-of-band.
    """

    configure_opik()
    with (
        tags(*TAGS_DATA_ONLINE),
        span("online_ingest", tags=TAGS_DATA_ONLINE, metadata=_ONLINE_METADATA),
    ):
        match source:
            case UrlSource():
                return await _ingest_url(source.uri, user_id)
            case FileSource():
                return await _ingest_file(source, user_id)
            case ConversationSource():
                return await _ingest_conversation(source, user_id)
            # ponytail: unreachable (discriminated union); guard a silent None.
            case _:
                raise TypeError(f"Unsupported online source: {type(source).__name__}")
