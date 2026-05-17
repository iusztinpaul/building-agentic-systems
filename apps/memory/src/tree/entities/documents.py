from datetime import datetime
from enum import StrEnum

from beanie import Document as BeanieDocument
from beanie import Link, PydanticObjectId
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
