from beanie import init_beanie
from pymongo import AsyncMongoClient

from twin.entities.documents import Document
from twin.entities.knowledge_graph import KnowledgeGraphEntry, KnowledgeGraphLogEntry

ALL_DOCUMENT_MODELS = [Document, KnowledgeGraphLogEntry, KnowledgeGraphEntry]


async def init_mongodb(uri: str, database: str) -> AsyncMongoClient:
    client = AsyncMongoClient(uri, tz_aware=True)
    await init_beanie(database=client[database], document_models=ALL_DOCUMENT_MODELS)

    return client
