from beanie import init_beanie
from pymongo import AsyncMongoClient

from tree.config.app_config import app_config
from tree.entities.clusters import MemoryCluster
from tree.entities.documents import Document
from tree.entities.extraction_audit import (
    ExtractionDroppedField,
    ExtractionRejection,
)
from tree.entities.memory import MemoryEntry, memory_indexes
from tree.entities.meta_state import KnowledgeGraphMetaState
from tree.entities.sessions import Session
from tree.entities.users import User

ALL_DOCUMENT_MODELS = [
    Document,
    MemoryEntry,
    MemoryCluster,
    KnowledgeGraphMetaState,
    Session,
    User,
    ExtractionRejection,
    ExtractionDroppedField,
]


async def init_mongodb(uri: str, database: str) -> AsyncMongoClient:
    client = AsyncMongoClient(uri, tz_aware=True)
    # Beanie reads ``Settings.indexes`` at init: bind the configured mode's set
    # first, so every entry point creates exactly that mode's classic indexes.
    MemoryEntry.Settings.indexes = memory_indexes(app_config.memory.mode)
    await init_beanie(database=client[database], document_models=ALL_DOCUMENT_MODELS)

    return client
