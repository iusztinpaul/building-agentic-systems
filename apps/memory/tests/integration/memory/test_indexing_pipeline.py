"""Integration tests for the memory indexing pipeline."""

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.memory.indexing.core import (
    _CANONICAL_NAME_INDEX,
    _VECTOR_INDEX_NAME,
    ensure_indexes,
)
from tree.memory.indexing.pipeline import memory_indexing
from tree.models.fake_model import FakeEmbeddingModel

TEST_DATABASE = "integration_tests_twin"


@pytest.fixture(autouse=True)
def _patch_database_name(mocker):
    """Ensure indexing uses the test database, not the real one."""
    mocker.patch(
        "tree.memory.indexing.pipeline.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )


# Shared fake document ID for graph entries.
_FAKE_DOC_ID = PydanticObjectId()
_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_NOW = datetime.now(tz=UTC)


async def _seed_knowledge_graph(
    mongo_client, user_id: PydanticObjectId = _USER_ID
) -> None:
    """Insert node and edge documents directly into knowledge_graph for tests."""
    col = mongo_client[TEST_DATABASE]["knowledge_graph"]
    await col.insert_many(
        [
            # Nodes
            {
                "_id": f"{user_id}:person:alice",
                "user_id": user_id,
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
                "_id": f"{user_id}:document:https://example.com/doc",
                "user_id": user_id,
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
                "_id": f"{user_id}:chunk:https://example.com/doc#chunk-0",
                "user_id": user_id,
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
                "_id": f"{user_id}:chunk:https://example.com/doc#chunk-0|part_of|{user_id}:document:https://example.com/doc",
                "user_id": user_id,
                "kind": "edge",
                "type": "part_of",
                "source_node_id": f"{user_id}:chunk:https://example.com/doc#chunk-0",
                "source_type": "chunk",
                "target_node_id": f"{user_id}:document:https://example.com/doc",
                "target_type": "document",
                "properties": {},
                "sources": [_FAKE_DOC_ID],
                "created_at": _NOW,
                "updated_at": _NOW,
            },
            {
                "_id": f"{user_id}:document:https://example.com/doc|mentions|{user_id}:person:alice",
                "user_id": user_id,
                "kind": "edge",
                "type": "mentions",
                "source_node_id": f"{user_id}:document:https://example.com/doc",
                "source_type": "document",
                "target_node_id": f"{user_id}:person:alice",
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


@pytest.fixture()
def _stub_dim_check(mocker):
    """Stub the #016 dim assertion: these tests build an index with the
    fake model's small dimension (8), not ``app_config.models.search_embedding.dimensions``
    (1024 in prod). Without this stub the new #020 boot-time assertion
    in ``memory_indexing`` would hard-fail on the deliberate mismatch.
    """

    from unittest.mock import AsyncMock

    mocker.patch(
        "tree.memory.indexing.pipeline.assert_settings_match_live_vector_index",
        new_callable=AsyncMock,
    )


@pytest.mark.requires_mongot
@pytest.mark.usefixtures("_skip_without_mongot", "_stub_dim_check")
class TestMemoryIndexingPipeline:
    async def test_embeds_nodes(self, mongo_client, mocker) -> None:
        await _seed_knowledge_graph(mongo_client)

        mocker.patch(
            "tree.memory.indexing.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "tree.memory.indexing.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_indexing(user_id=_USER_ID)

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        nodes = await kg.find({"kind": "node"}).to_list()
        for node in nodes:
            assert len(node["embedding"]) == 8

    async def test_text_index_created(self, mongo_client, mocker) -> None:
        await _seed_knowledge_graph(mongo_client)

        mocker.patch(
            "tree.memory.indexing.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "tree.memory.indexing.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_indexing(user_id=_USER_ID)

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        indexes = await kg.index_information()
        assert "text_index" in indexes

    @pytest.mark.slow
    async def test_idempotent_indexing(self, mongo_client, mocker) -> None:
        """Running indexing twice produces the same result."""
        await _seed_knowledge_graph(mongo_client)

        mocker.patch(
            "tree.memory.indexing.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "tree.memory.indexing.pipeline.get_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_indexing(user_id=_USER_ID)

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        first_count = await kg.count_documents({})

        # Re-run
        with prefect_tags("tests"):
            await memory_indexing(user_id=_USER_ID)

        second_count = await kg.count_documents({})
        assert first_count == second_count


async def _wait_for_search_index_definition(
    collection,
    *,
    expected_dimensions: int,
    timeout: float = 30.0,
) -> dict:
    """Block until ``vector_index`` reports the expected ``numDimensions``.

    mongot is eventually-consistent for index DDL; polling avoids flakes
    on the drop+recreate path.
    """

    deadline = asyncio.get_event_loop().time() + timeout
    last_seen: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        cursor = await collection.list_search_indexes(_VECTOR_INDEX_NAME)
        indexes = await cursor.to_list()
        if indexes:
            last_seen = indexes[0]
            fields = (
                last_seen.get("latestDefinition", {}).get("fields")
                or last_seen.get("definition", {}).get("fields")
                or []
            )
            for field in fields:
                if (
                    field.get("type") == "vector"
                    and field.get("numDimensions") == expected_dimensions
                ):
                    return last_seen
        await asyncio.sleep(1.0)
    raise AssertionError(
        f"vector_index did not converge to numDimensions={expected_dimensions} "
        f"within {timeout}s. Last seen: {last_seen}"
    )


@pytest.mark.requires_mongot
@pytest.mark.usefixtures("_skip_without_mongot")
class TestEnsureIndexesReconcile:
    @pytest.mark.slow
    async def test_dimension_mismatch_drops_and_recreates_with_warning(
        self, mongo_client, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Start with a 16-dim vector index, then reconcile against an
        8-dim model — expect a WARNING + the live index reports 8 dims."""

        # Arrange: seed the 16-dim index.
        await ensure_indexes(
            mongo_client,
            TEST_DATABASE,
            embedding_model=FakeEmbeddingModel(dimensions=16),
            user_id=_USER_ID,
        )
        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        await _wait_for_search_index_definition(kg, expected_dimensions=16)

        # Act: reconcile against 8 dims.
        with caplog.at_level(logging.WARNING, logger="tree.memory.indexing.core"):
            await ensure_indexes(
                mongo_client,
                TEST_DATABASE,
                embedding_model=FakeEmbeddingModel(dimensions=8),
                user_id=_USER_ID,
            )

        # Assert: warning mentions both numbers; live index reports 8 dims.
        warning_text = " ".join(
            r.getMessage() for r in caplog.records if r.levelname == "WARNING"
        )
        assert "16" in warning_text
        assert "8" in warning_text

        live = await _wait_for_search_index_definition(kg, expected_dimensions=8)
        fields = (
            live.get("latestDefinition", {}).get("fields")
            or live.get("definition", {}).get("fields")
            or []
        )
        filter_paths = {f["path"] for f in fields if f.get("type") == "filter"}
        assert "merged_into" in filter_paths

        # Drop the collection so the cleanup fixture sees a clean slate.
        await mongo_client[TEST_DATABASE].drop_collection("knowledge_graph")

    async def test_canonical_name_and_alias_text_index_created(
        self, mongo_client
    ) -> None:
        """``ensure_indexes`` must create the canonical_name index and
        ensure the text index covers the top-level ``aliases`` field."""

        await ensure_indexes(
            mongo_client,
            TEST_DATABASE,
            embedding_model=FakeEmbeddingModel(dimensions=8),
            user_id=_USER_ID,
        )

        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        indexes = await kg.index_information()

        # canonical_name index — non-unique, sparse, with user_id as
        # the leading key (post-#019).
        assert _CANONICAL_NAME_INDEX in indexes
        canonical = indexes[_CANONICAL_NAME_INDEX]
        assert canonical.get("sparse") is True
        assert canonical.get("unique") is not True
        # Key shape: [("user_id", 1), ("canonical_name", 1)].
        assert canonical["key"][0][0] == "user_id"
        assert canonical["key"][1][0] == "canonical_name"

        # Text index — must include ``aliases`` (top-level) in its weights.
        assert "text_index" in indexes
        text_index = indexes["text_index"]
        # MongoDB exposes covered text-index fields under ``weights``.
        weights = text_index.get("weights", {})
        assert "aliases" in weights

        await mongo_client[TEST_DATABASE].drop_collection("knowledge_graph")

    async def test_idempotent_reconcile_no_warning_on_second_call(
        self, mongo_client, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two back-to-back calls with the same model produce zero
        WARNINGs the second time around (live state already matches)."""

        await ensure_indexes(
            mongo_client,
            TEST_DATABASE,
            embedding_model=FakeEmbeddingModel(dimensions=8),
            user_id=_USER_ID,
        )
        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        await _wait_for_search_index_definition(kg, expected_dimensions=8)

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="tree.memory.indexing.core"):
            await ensure_indexes(
                mongo_client,
                TEST_DATABASE,
                embedding_model=FakeEmbeddingModel(dimensions=8),
                user_id=_USER_ID,
            )

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == []

        await mongo_client[TEST_DATABASE].drop_collection("knowledge_graph")
