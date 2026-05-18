from datetime import datetime
from enum import StrEnum
from typing import Any

from beanie import Document as BeanieDocument
from beanie import Link, PydanticObjectId
from pydantic import Field
from pymongo import IndexModel


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
    # No standalone single-key index on ``user_id``: the compound
    # ``user_source_uri_unique`` index already leads with ``user_id`` so
    # ``find({"user_id": X, ...})`` queries hit the index prefix without
    # paying for a redundant single-key index per row.
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

    class Settings:
        name = "documents"
        indexes = [
            # Tenant-scoped uniqueness: the same (source_type, source_uri)
            # may be ingested independently by different users; only the
            # full (user_id, source_type, source_uri) triple is unique.
            IndexModel(
                [("user_id", 1), ("source_type", 1), ("source_uri", 1)],
                unique=True,
                name="user_source_uri_unique",
            ),
        ]
