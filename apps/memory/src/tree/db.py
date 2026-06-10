from beanie import init_beanie
from pymongo import AsyncMongoClient

from tree.entities.documents import Document
from tree.entities.extraction_audit import (
    ExtractionDroppedField,
    ExtractionRejection,
)
from tree.entities.knowledge_graph import KnowledgeGraphEntry
from tree.entities.meta_state import KnowledgeGraphMetaState
from tree.entities.sessions import Session
from tree.entities.users import User

ALL_DOCUMENT_MODELS = [
    Document,
    KnowledgeGraphEntry,
    KnowledgeGraphMetaState,
    Session,
    User,
    ExtractionRejection,
    ExtractionDroppedField,
]


async def init_mongodb(uri: str, database: str) -> AsyncMongoClient:
    client = AsyncMongoClient(uri, tz_aware=True)
    await init_beanie(database=client[database], document_models=ALL_DOCUMENT_MODELS)

    return client
