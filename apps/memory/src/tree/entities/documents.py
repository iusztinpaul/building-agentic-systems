from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from beanie import Document as BeanieDocument
from beanie import Link, PydanticObjectId
from pydantic import Field, field_validator
from pymongo import IndexModel

# Query keys that only track how a link was shared, never which page it is.
# Identity-bearing keys (YouTube ``v``, Hacker News ``id``) are deliberately
# absent: dropping them would merge distinct documents onto one natural key.
_TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_",)
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "si"}
)


def clean_source_uri(source_uri: str) -> str:
    """Reduce an ``http(s)`` URI to the clean form stored as ``source_uri``.

    Lowercases the scheme and host, drops the ``#fragment`` and every tracking
    query param, and keeps every other param byte-identical, in order (the
    query is split on ``&`` rather than re-encoded). Any other scheme
    (``file://``, ``conversation://``, bare paths) is returned unchanged: a
    file name may legitimately contain ``#``. Idempotent.

    e.g. ``https://X.com/Post?utm_source=x&id=7#top`` -> ``https://x.com/Post?id=7``
    """

    parts = urlsplit(source_uri.strip())
    if parts.scheme.lower() not in {"http", "https"}:
        return source_uri

    query = "&".join(
        pair
        for pair in parts.query.split("&")
        if pair and not _is_tracking_param(pair.split("=", 1)[0])
    )
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, query, "")
    )


def _is_tracking_param(key: str) -> bool:
    key = key.lower()
    return key in _TRACKING_PARAMS or key.startswith(_TRACKING_PARAM_PREFIXES)


class SourceType(StrEnum):
    SUBSTACK = "substack"
    HUGGINGFACE = "huggingface"
    LATENT = "latent"
    FILE = "file"
    CONVERSATION = "conversation"
    WEB = "web"
    YOUTUBE = "youtube"


class Document(BeanieDocument):
    source_type: SourceType
    source_uri: str
    user_id: PydanticObjectId
    title: str | None = None
    summary: str | None = None
    content: str | None = None
    authors: list[str] = []
    date: datetime | None = None
    references: list[Link["Document"]] = []
    # Per-source free-form metadata. Phase-2 conversation ingestion stores
    # ``session_started_at`` (tz-aware UTC ``datetime``) here when the
    # caller supplies one; other sources are free to add their own keys.
    # No index is created on this field — it is a bag, not a queryable
    # surface.
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Normalized ingest-failure marker: ``"<code>: <message>"`` — a short
    # stable code, a colon, then a human message (e.g.
    # ``"no_transcript: no captions on either backend"``,
    # ``"invalid_url: no video id in input"``). NEVER a raw exception dump.
    # Set when ingestion could not produce content, so a failure row carries
    # ``content=None`` and is excluded from extraction by the existing
    # ``{"content": {"$ne": None}}`` filters. ``None`` on every successfully
    # ingested row. Nullable → no migration; not indexed (inspected ad hoc,
    # not a query surface).
    ingest_error: str | None = None

    @field_validator("source_uri")
    @classmethod
    def _clean_source_uri(cls, value: str) -> str:
        # Every row stores the clean form, so every layer downstream
        # (chunks, memory row ids, receipts) inherits it. Lookups must key on
        # ``clean_source_uri`` too, or a raw URI misses the stored row.
        return clean_source_uri(value)

    class Settings:
        name = "documents"
        indexes = [
            # Tenant-scoped uniqueness: the same source_uri may be ingested
            # independently by different users; only (user_id, source_uri) is
            # unique. source_type is a row attribute a LATENT upgrade
            # rewrites, not part of the key (ADR-010).
            IndexModel(
                [("user_id", 1), ("source_uri", 1)],
                unique=True,
                name="user_source_uri_unique",
            ),
        ]
