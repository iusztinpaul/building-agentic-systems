"""Integration tests for the memory indexing pipeline."""

from datetime import UTC, datetime

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from twin.memory.indexing.pipeline import memory_indexing
from twin.models.fake_model import FakeEmbeddingModel

TEST_DATABASE = "integration_tests_twin"


@pytest.fixture(autouse=True)
def _patch_database_name(mocker):
    """Ensure indexing uses the test database, not the real one."""
    mocker.patch(
        "twin.memory.indexing.pipeline.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )


# Shared fake document ID for graph entries.
_FAKE_DOC_ID = PydanticObjectId()
_NOW = datetime.now(tz=UTC)


async def _seed_knowledge_graph(mongo_client) -> None:
    """Insert node and edge documents directly into knowledge_graph for tests."""
    col = mongo_client[TEST_DATABASE]["knowledge_graph"]
    await col.insert_many(
        [
            # Nodes
            {
                "_id": "person:alice",
                "kind": "node",
                "type": "person",
                "name": "alice",
                "properties": {"aliases": ["alice doe"]},
                "embedding": [],
                "sources": [_FAKE_DOC_ID],
                "created_at": _NOW,
                "updated_at": _NOW,
            },
            {
                "_id": "document:https://example.com/doc",
                "kind": "node",
                "type": "document",
                "name": "https://example.com/doc",
                "properties": {
                    "source_type": "huggingface",
                    "source_uri": "https://example.com/doc",
                },
                "embedding": [],
                "sources": [_FAKE_DOC_ID],
                "created_at": _NOW,
                "updated_at": _NOW,
            },
            {
                "_id": "chunk:https://example.com/doc#chunk-0",
                "kind": "node",
                "type": "chunk",
                "name": "https://example.com/doc#chunk-0",
                "properties": {
                    "source_type": "huggingface",
                    "source_uri": "https://example.com/doc",
                    "content": "Alice works on ML pipelines.",
                },
                "embedding": [],
                "sources": [_FAKE_DOC_ID],
                "created_at": _NOW,
                "updated_at": _NOW,
            },
            # Edges
            {
                "_id": "chunk:https://example.com/doc#chunk-0|part_of|document:https://example.com/doc",
                "kind": "edge",
                "type": "part_of",
                "source_node_id": "chunk:https://example.com/doc#chunk-0",
                "source_type": "chunk",
                "target_node_id": "document:https://example.com/doc",
                "target_type": "document",
                "properties": {},
                "sources": [_FAKE_DOC_ID],
                "created_at": _NOW,
                "updated_at": _NOW,
            },
            {
                "_id": "document:https://example.com/doc|mentions|person:alice",
                "kind": "edge",
                "type": "mentions",
                "source_node_id": "document:https://example.com/doc",
                "source_type": "document",
                "target_node_id": "person:alice",
                "target_type": "person",
                "properties": {},
                "sources": [_FAKE_DOC_ID],
                "created_at": _NOW,
                "updated_at": _NOW,
            },
        ]
    )


@pytest.fixture(autouse=True)
async def _clean_kg_collection(mongo_client):
    """Drop the knowledge_graph collection after each test."""
    yield
    await mongo_client[TEST_DATABASE].drop_collection("knowledge_graph")


@pytest.mark.usefixtures("_skip_without_mongot")
class TestMemoryIndexingPipeline:
    async def test_embeds_nodes(self, mongo_client, mocker) -> None:
        await _seed_knowledge_graph(mongo_client)

        mocker.patch(
            "twin.memory.indexing.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.indexing.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_indexing()

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        nodes = await kg.find({"kind": "node"}).to_list()
        for node in nodes:
            assert len(node["embedding"]) == 8

    async def test_reverse_edges_created(self, mongo_client, mocker) -> None:
        await _seed_knowledge_graph(mongo_client)

        mocker.patch(
            "twin.memory.indexing.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.indexing.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_indexing()

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        reverse_edges = await kg.find({"direction": "reverse"}).to_list()
        # MENTIONS (document→person) should have a reverse edge
        assert len(reverse_edges) >= 1
        # Verify reverse edge has string _id
        for edge in reverse_edges:
            assert isinstance(edge["_id"], str)
            assert "|" in edge["_id"]

    async def test_text_index_created(self, mongo_client, mocker) -> None:
        await _seed_knowledge_graph(mongo_client)

        mocker.patch(
            "twin.memory.indexing.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.indexing.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_indexing()

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        indexes = await kg.index_information()
        assert "text_index" in indexes

    async def test_idempotent_indexing(self, mongo_client, mocker) -> None:
        """Running indexing twice produces the same result."""
        await _seed_knowledge_graph(mongo_client)

        mocker.patch(
            "twin.memory.indexing.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.indexing.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_indexing()

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        first_count = await kg.count_documents({})

        # Re-run
        with prefect_tags("tests"):
            await memory_indexing()

        second_count = await kg.count_documents({})
        assert first_count == second_count
