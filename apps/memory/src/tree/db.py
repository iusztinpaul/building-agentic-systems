from beanie import init_beanie
from pymongo import AsyncMongoClient

from tree.entities.documents import Document
from tree.entities.knowledge_graph import KnowledgeGraphEntry

ALL_DOCUMENT_MODELS = [Document, KnowledgeGraphEntry]


async def init_mongodb(uri: str, database: str) -> AsyncMongoClient:
    client = AsyncMongoClient(uri, tz_aware=True)
    await init_beanie(database=client[database], document_models=ALL_DOCUMENT_MODELS)

    return client
