from datetime import datetime
from enum import StrEnum

from beanie import Document as BeanieDocument
from beanie import Indexed, Link
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
    source_uri: Indexed(str, unique=True)
    title: str | None = None
    summary: str | None = None
    content: str | None = None
    authors: list[str] = []
    date: datetime | None = None
    references: list[Link["Document"]] = []

    class Settings:
        name = "documents"
        indexes = [
            IndexModel(
                [("source_type", 1), ("source_uri", 1)],
                unique=True,
            ),
        ]
