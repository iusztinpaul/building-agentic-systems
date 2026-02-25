from datetime import datetime
from enum import StrEnum

from beanie import Document as BeanieDocument
from beanie import Indexed
from pymongo import IndexModel


class SourceType(StrEnum):
    SUBSTACK = "substack"


class Document(BeanieDocument):
    source_type: SourceType
    source_uri: Indexed(str, unique=True)
    title: str
    summary: str
    summary_embedding: list[float] = []
    content: str
    authors: list[str]
    date: datetime
    references: list[str] = []

    class Settings:
        name = "documents"
        indexes = [
            IndexModel(
                [("source_type", 1), ("source_uri", 1)],
                unique=True,
            ),
        ]
