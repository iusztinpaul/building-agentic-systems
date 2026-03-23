"""Integration tests for the memory materialization pipeline."""

from datetime import UTC, datetime

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from twin.entities.knowledge_graph import (
    EdgeLogEntry,
    EdgeType,
    NodeLogEntry,
    NodeType,
)
from twin.memory.materialization.pipeline import memory_materialization
from twin.models.fake_model import FakeEmbeddingModel

TEST_DATABASE = "integration_tests_twin"


@pytest.fixture(autouse=True)
def _patch_database_name(mocker):
    """Ensure materialization uses the test database, not the real one."""
    mocker.patch(
        "twin.memory.materialization.pipeline.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )


# Shared fake document ID for log entries.
_FAKE_DOC_ID = PydanticObjectId()
_NOW = datetime.now(tz=UTC)


async def _seed_kg_log() -> None:
    """Insert minimal node and edge log entries for materialization tests."""
    await NodeLogEntry.insert_many(
        [
            NodeLogEntry(
                name="alice",
                type=NodeType.PERSON,
                properties={"aliases": ["alice doe"]},
                source_document_id=_FAKE_DOC_ID,
                chunk_id="chunk-1",
                created_at=_NOW,
            ),
            NodeLogEntry(
                name="https://example.com/doc",
                type=NodeType.DOCUMENT,
                properties={
                    "source_type": "huggingface",
                    "source_uri": "https://example.com/doc",
                },
                source_document_id=_FAKE_DOC_ID,
                chunk_id="chunk-struct",
                created_at=_NOW,
            ),
            NodeLogEntry(
                name="https://example.com/doc#chunk-0",
                type=NodeType.CHUNK,
                properties={
                    "source_type": "huggingface",
                    "source_uri": "https://example.com/doc",
                    "content": "Alice works on ML pipelines.",
                },
                source_document_id=_FAKE_DOC_ID,
                chunk_id="chunk-1",
                created_at=_NOW,
            ),
        ]
    )
    await EdgeLogEntry.insert_many(
        [
            EdgeLogEntry(
                source_node_id="https://example.com/doc#chunk-0",
                source_type=NodeType.CHUNK,
                target_node_id="https://example.com/doc",
                target_type=NodeType.DOCUMENT,
                type=EdgeType.PART_OF,
                source_document_id=_FAKE_DOC_ID,
                chunk_id="chunk-1",
                created_at=_NOW,
            ),
            EdgeLogEntry(
                source_node_id="https://example.com/doc",
                source_type=NodeType.DOCUMENT,
                target_node_id="alice",
                target_type=NodeType.PERSON,
                type=EdgeType.MENTIONS,
                source_document_id=_FAKE_DOC_ID,
                chunk_id="chunk-struct",
                created_at=_NOW,
            ),
        ]
    )


@pytest.fixture(autouse=True)
async def _clean_kg_collection(mongo_client):
    """Drop the materialized knowledge_graph collection after each test."""
    yield
    await mongo_client[TEST_DATABASE].drop_collection("knowledge_graph")


class TestMemoryMaterializationPipeline:
    async def test_materializes_nodes_and_edges(self, mongo_client, mocker) -> None:
        await _seed_kg_log()

        mocker.patch(
            "twin.memory.materialization.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.materialization.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_materialization()

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        node_count = await kg.count_documents({"kind": "node"})
        edge_count = await kg.count_documents({"kind": "edge"})

        assert node_count == 3  # alice, document, chunk
        assert edge_count >= 2  # part_of, mentions

    async def test_nodes_receive_embeddings(self, mongo_client, mocker) -> None:
        await _seed_kg_log()

        mocker.patch(
            "twin.memory.materialization.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.materialization.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_materialization()

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        nodes = await kg.find({"kind": "node"}).to_list()
        for node in nodes:
            assert len(node["embedding"]) == 8

    async def test_reverse_edges_created(self, mongo_client, mocker) -> None:
        await _seed_kg_log()

        mocker.patch(
            "twin.memory.materialization.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.materialization.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_materialization()

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        reverse_edges = await kg.find({"direction": "reverse"}).to_list()
        # MENTIONS (document→person) should have a reverse edge
        assert len(reverse_edges) >= 1

    async def test_text_index_created(self, mongo_client, mocker) -> None:
        await _seed_kg_log()

        mocker.patch(
            "twin.memory.materialization.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.materialization.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_materialization()

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        indexes = await kg.index_information()
        assert "text_index" in indexes

    async def test_rematerialization_is_idempotent(self, mongo_client, mocker) -> None:
        """Running materialization twice produces the same result."""
        await _seed_kg_log()

        mocker.patch(
            "twin.memory.materialization.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.materialization.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_materialization()

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        first_node_count = await kg.count_documents({"kind": "node"})

        # Re-run
        with prefect_tags("tests"):
            await memory_materialization()

        second_node_count = await kg.count_documents({"kind": "node"})
        assert first_node_count == second_node_count
