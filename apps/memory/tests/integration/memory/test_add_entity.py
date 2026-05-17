"""Integration tests for ``add_entity`` against MongoDB.

These tests run against the local MongoDB instance (no Atlas search needed
for most of them — we patch ``dedupe_entity`` to return canned results so
we can isolate the write-side behavior of the orchestrator). The flagged
path test does run live to confirm the SAME_AS edge layout.

The aliases-cap and sources-cap tests seed pre-existing arrays beyond the
cap to confirm the ``$slice`` truncation works as designed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from beanie import PydanticObjectId

from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.memory.extraction.add_entity import add_entity
from tree.memory.extraction.dedup import (
    DeduplicationConfig,
    DeduplicationResult,
    MergeStrategy,
)
from tree.memory.resolution.composite import CompositeResolver
from tree.models.fake_model import FakeEmbeddingModel

TEST_DATABASE = "integration_tests_twin"
_NOW = datetime.now(tz=UTC)
# Real PydanticObjectId used everywhere in this suite so prospective_ids carry
# a realistic 24-hex-char tenant prefix.
_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_PH = str(_USER_ID)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def kg_collection(mongo_client):
    """Return the ``knowledge_graph`` collection; drop after each test."""

    db = mongo_client[TEST_DATABASE]
    col = db["knowledge_graph"]
    yield col
    await db.drop_collection("knowledge_graph")


@pytest.fixture
def embedding_model() -> FakeEmbeddingModel:
    return FakeEmbeddingModel(dimensions=8)


@pytest.fixture
def resolver(embedding_model: FakeEmbeddingModel) -> CompositeResolver:
    return CompositeResolver(embedding_model=embedding_model)


def _seed_person(
    node_id: str,
    *,
    name: str,
    canonical_name: str | None = None,
    aliases: list[str] | None = None,
    properties: dict[str, Any] | None = None,
    sources: list[Any] | None = None,
    confidence: float = 1.0,
    user_id: PydanticObjectId = _USER_ID,
) -> dict[str, Any]:
    return {
        "_id": node_id,
        "user_id": user_id,
        "kind": "node",
        "type": NodeType.PERSON.value,
        "name": name,
        "canonical_name": canonical_name or name,
        "aliases": aliases or [],
        "properties": properties or {},
        "sources": sources or [],
        "confidence": confidence,
        "embedding": [],
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _patch_dedupe(mocker, result: DeduplicationResult) -> None:
    mocker.patch(
        "tree.memory.extraction.add_entity.dedupe_entity",
        new=AsyncMock(return_value=result),
    )


# ---------------------------------------------------------------------------
# Soft-join: action="none" creates a new node without touching the existing one.
# ---------------------------------------------------------------------------


class TestSoftJoinPreservation:
    async def test_new_node_shares_canonical_name(
        self,
        mongo_client,
        kg_collection,
        embedding_model,
        resolver,
        mocker,
    ) -> None:
        # Arrange — pre-existing canonical seeded under the placeholder
        # user prefix so it shares the tenant of the row add_entity writes.
        await kg_collection.insert_one(
            _seed_person(
                f"{_PH}:person:apple inc",
                name="apple inc",
                canonical_name="apple inc",
            )
        )
        _patch_dedupe(mocker, DeduplicationResult(action="none"))

        # Act
        target_id, _resolved, dedup_result = await add_entity(
            database=mongo_client[TEST_DATABASE],
            embedding_model=embedding_model,
            resolver=resolver,
            user_id=_USER_ID,
            name="apple",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="doc1",
            dedup_config=DeduplicationConfig(),
            # Force the resolver to return canonical_name="apple inc" via alias.
            candidate_names=["apple inc"],
            candidate_aliases={"apple inc": ["apple"]},
        )

        # Assert
        assert dedup_result.action == "none"
        assert target_id == f"{_PH}:person:apple"

        new_doc = await kg_collection.find_one({"_id": f"{_PH}:person:apple"})
        assert new_doc is not None
        assert new_doc["canonical_name"] == "apple inc"

        old_doc = await kg_collection.find_one({"_id": f"{_PH}:person:apple inc"})
        assert old_doc is not None
        assert old_doc["canonical_name"] == "apple inc"

        # Both rows surface under the same canonical_name (soft-join).
        same_canonical = await kg_collection.find(
            {"canonical_name": "apple inc"}
        ).to_list(length=None)
        ids = {d["_id"] for d in same_canonical}
        assert {f"{_PH}:person:apple", f"{_PH}:person:apple inc"} <= ids


# ---------------------------------------------------------------------------
# Auto-merge strategies: KEEP_PRIMARY, MERGE_PROPERTIES, KEEP_ALIASES.
# ---------------------------------------------------------------------------


class TestAutoMergeKeepPrimary:
    async def test_alias_appended_properties_unchanged(
        self,
        mongo_client,
        kg_collection,
        embedding_model,
        resolver,
        mocker,
    ) -> None:
        # Arrange
        await kg_collection.insert_one(
            _seed_person(
                "person:apple inc",
                name="apple inc",
                properties={"description": "short"},
            )
        )
        _patch_dedupe(
            mocker,
            DeduplicationResult(
                action="merged",
                matched_node_id="person:apple inc",
                matched_node_name="apple inc",
                similarity_score=0.97,
                match_type="embedding",
            ),
        )

        # Act
        target_id, _resolved, dedup_result = await add_entity(
            database=mongo_client[TEST_DATABASE],
            embedding_model=embedding_model,
            resolver=resolver,
            user_id=_USER_ID,
            name="apple corp",
            entity_type=NodeType.PERSON,
            properties={"description": "a much longer description"},
            source_id="doc1",
            dedup_config=DeduplicationConfig(merge_strategy=MergeStrategy.KEEP_PRIMARY),
        )

        # Assert
        assert target_id == "person:apple inc"
        assert dedup_result.applied_strategy is MergeStrategy.KEEP_PRIMARY

        doc = await kg_collection.find_one({"_id": "person:apple inc"})
        assert doc is not None
        assert "apple corp" in doc["aliases"]
        # KEEP_PRIMARY drops incoming properties.
        assert doc["properties"]["description"] == "short"
        # No new node created.
        assert await kg_collection.count_documents({"_id": "person:apple corp"}) == 0


class TestAutoMergeMergeProperties:
    async def test_longer_string_wins(
        self,
        mongo_client,
        kg_collection,
        embedding_model,
        resolver,
        mocker,
    ) -> None:
        # Arrange
        await kg_collection.insert_one(
            _seed_person(
                "person:apple inc",
                name="apple inc",
                properties={"description": "short"},
            )
        )
        _patch_dedupe(
            mocker,
            DeduplicationResult(
                action="merged",
                matched_node_id="person:apple inc",
                matched_node_name="apple inc",
                similarity_score=0.97,
                match_type="embedding",
            ),
        )

        # Act
        await add_entity(
            database=mongo_client[TEST_DATABASE],
            embedding_model=embedding_model,
            resolver=resolver,
            user_id=_USER_ID,
            name="apple corp",
            entity_type=NodeType.PERSON,
            properties={"description": "a much longer description"},
            source_id="doc1",
            dedup_config=DeduplicationConfig(
                merge_strategy=MergeStrategy.MERGE_PROPERTIES
            ),
        )

        # Assert
        doc = await kg_collection.find_one({"_id": "person:apple inc"})
        assert doc is not None
        assert doc["properties"]["description"] == "a much longer description"
        assert "apple corp" in doc["aliases"]

    async def test_missing_key_taken_from_incoming(
        self,
        mongo_client,
        kg_collection,
        embedding_model,
        resolver,
        mocker,
    ) -> None:
        # Arrange — canonical has no email; incoming has one.
        await kg_collection.insert_one(
            _seed_person(
                "person:alice",
                name="alice",
                properties={"description": "primary"},
            )
        )
        _patch_dedupe(
            mocker,
            DeduplicationResult(
                action="merged",
                matched_node_id="person:alice",
                matched_node_name="alice",
                similarity_score=0.97,
                match_type="embedding",
            ),
        )

        # Act
        await add_entity(
            database=mongo_client[TEST_DATABASE],
            embedding_model=embedding_model,
            resolver=resolver,
            user_id=_USER_ID,
            name="alice s",
            entity_type=NodeType.PERSON,
            properties={"email": "alice@example.com"},
            source_id="doc1",
            dedup_config=DeduplicationConfig(
                merge_strategy=MergeStrategy.MERGE_PROPERTIES
            ),
        )

        # Assert
        doc = await kg_collection.find_one({"_id": "person:alice"})
        assert doc is not None
        assert doc["properties"]["email"] == "alice@example.com"
        # Existing key preserved.
        assert doc["properties"]["description"] == "primary"

    async def test_list_set_union(
        self,
        mongo_client,
        kg_collection,
        embedding_model,
        resolver,
        mocker,
    ) -> None:
        # Arrange — both sides have a list at "tags".
        await kg_collection.insert_one(
            _seed_person(
                "person:bob",
                name="bob",
                properties={"tags": ["a", "b"]},
            )
        )
        _patch_dedupe(
            mocker,
            DeduplicationResult(
                action="merged",
                matched_node_id="person:bob",
                matched_node_name="bob",
                similarity_score=0.97,
                match_type="embedding",
            ),
        )

        # Act
        await add_entity(
            database=mongo_client[TEST_DATABASE],
            embedding_model=embedding_model,
            resolver=resolver,
            user_id=_USER_ID,
            name="bob j",
            entity_type=NodeType.PERSON,
            properties={"tags": ["b", "c"]},
            source_id="doc1",
            dedup_config=DeduplicationConfig(
                merge_strategy=MergeStrategy.MERGE_PROPERTIES
            ),
        )

        # Assert
        doc = await kg_collection.find_one({"_id": "person:bob"})
        assert doc is not None
        assert set(doc["properties"]["tags"]) == {"a", "b", "c"}


class TestAutoMergeKeepAliases:
    async def test_alias_appended_properties_untouched(
        self,
        mongo_client,
        kg_collection,
        embedding_model,
        resolver,
        mocker,
    ) -> None:
        # Arrange
        await kg_collection.insert_one(
            _seed_person(
                "person:apple inc",
                name="apple inc",
                properties={"description": "short"},
            )
        )
        _patch_dedupe(
            mocker,
            DeduplicationResult(
                action="merged",
                matched_node_id="person:apple inc",
                matched_node_name="apple inc",
                similarity_score=0.97,
                match_type="embedding",
            ),
        )

        # Act
        await add_entity(
            database=mongo_client[TEST_DATABASE],
            embedding_model=embedding_model,
            resolver=resolver,
            user_id=_USER_ID,
            name="apple corp",
            entity_type=NodeType.PERSON,
            properties={"description": "a much longer description"},
            source_id="doc1",
            dedup_config=DeduplicationConfig(merge_strategy=MergeStrategy.KEEP_ALIASES),
        )

        # Assert
        doc = await kg_collection.find_one({"_id": "person:apple inc"})
        assert doc is not None
        assert "apple corp" in doc["aliases"]
        assert doc["properties"]["description"] == "short"


# ---------------------------------------------------------------------------
# Flagged path: new node + pending SAME_AS edge.
# ---------------------------------------------------------------------------


class TestFlaggedPath:
    async def test_new_node_and_pending_edge_emitted(
        self,
        mongo_client,
        kg_collection,
        embedding_model,
        resolver,
        mocker,
    ) -> None:
        # Arrange — pre-existing canonical seeded under the placeholder
        # tenant prefix so dedupe's matched_node_id lines up with reality.
        await kg_collection.insert_one(
            _seed_person(f"{_PH}:person:alice smith", name="alice smith")
        )
        _patch_dedupe(
            mocker,
            DeduplicationResult(
                action="flagged",
                matched_node_id=f"{_PH}:person:alice smith",
                matched_node_name="alice smith",
                similarity_score=0.88,
                match_type="embedding",
            ),
        )

        # Act
        target_id, _resolved, _dedup = await add_entity(
            database=mongo_client[TEST_DATABASE],
            embedding_model=embedding_model,
            resolver=resolver,
            user_id=_USER_ID,
            name="alyce smyth",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="doc1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert
        assert target_id == f"{_PH}:person:alyce smyth"

        # New node exists.
        new_node = await kg_collection.find_one({"_id": f"{_PH}:person:alyce smyth"})
        assert new_node is not None
        assert new_node["kind"] == "node"

        # SAME_AS edge with status="pending".
        edge_id = f"{_PH}:person:alyce smyth|same_as|{_PH}:person:alice smith"
        edge = await kg_collection.find_one({"_id": edge_id})
        assert edge is not None
        assert edge["kind"] == "edge"
        assert edge["type"] == EdgeType.SAME_AS.value
        assert edge["properties"]["status"] == "pending"
        assert edge["properties"]["confidence"] == pytest.approx(0.88)
        assert edge["properties"]["match_type"] == "embedding"


# ---------------------------------------------------------------------------
# Caps: aliases at 50, sources at 500.
# ---------------------------------------------------------------------------


class TestAliasesCap:
    async def test_existing_60_aliases_truncated_to_50(
        self,
        mongo_client,
        kg_collection,
        embedding_model,
        resolver,
        mocker,
    ) -> None:
        # Arrange — seed 60 unique aliases.
        seed_aliases = [f"alias_{i:02d}" for i in range(60)]
        await kg_collection.insert_one(
            _seed_person(
                "person:apple inc",
                name="apple inc",
                aliases=seed_aliases,
            )
        )
        _patch_dedupe(
            mocker,
            DeduplicationResult(
                action="merged",
                matched_node_id="person:apple inc",
                matched_node_name="apple inc",
                similarity_score=0.97,
                match_type="embedding",
            ),
        )

        # Act
        await add_entity(
            database=mongo_client[TEST_DATABASE],
            embedding_model=embedding_model,
            resolver=resolver,
            user_id=_USER_ID,
            name="brand new alias",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="doc1",
            dedup_config=DeduplicationConfig(merge_strategy=MergeStrategy.KEEP_PRIMARY),
        )

        # Assert
        doc = await kg_collection.find_one({"_id": "person:apple inc"})
        assert doc is not None
        assert len(doc["aliases"]) == 50


class TestSourcesCap:
    async def test_existing_600_sources_truncated_to_500(
        self,
        mongo_client,
        kg_collection,
        embedding_model,
        resolver,
        mocker,
    ) -> None:
        # Arrange — seed 600 unique source ids.
        seed_sources = [f"src_{i:04d}" for i in range(600)]
        await kg_collection.insert_one(
            _seed_person(
                "person:apple inc",
                name="apple inc",
                sources=seed_sources,
            )
        )
        _patch_dedupe(
            mocker,
            DeduplicationResult(
                action="merged",
                matched_node_id="person:apple inc",
                matched_node_name="apple inc",
                similarity_score=0.97,
                match_type="embedding",
            ),
        )

        # Act
        await add_entity(
            database=mongo_client[TEST_DATABASE],
            embedding_model=embedding_model,
            resolver=resolver,
            user_id=_USER_ID,
            name="ignored",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="brand_new_src",
            dedup_config=DeduplicationConfig(merge_strategy=MergeStrategy.KEEP_PRIMARY),
        )

        # Assert
        doc = await kg_collection.find_one({"_id": "person:apple inc"})
        assert doc is not None
        assert len(doc["sources"]) == 500


# ---------------------------------------------------------------------------
# Per-merge atomicity + idempotency.
# ---------------------------------------------------------------------------


class TestConcurrentMerges:
    async def test_two_concurrent_merges_both_aliases_present(
        self,
        mongo_client,
        kg_collection,
        embedding_model,
        resolver,
        mocker,
    ) -> None:
        # Arrange
        await kg_collection.insert_one(
            _seed_person("person:apple inc", name="apple inc")
        )
        _patch_dedupe(
            mocker,
            DeduplicationResult(
                action="merged",
                matched_node_id="person:apple inc",
                matched_node_name="apple inc",
                similarity_score=0.97,
                match_type="embedding",
            ),
        )
        config = DeduplicationConfig(merge_strategy=MergeStrategy.KEEP_PRIMARY)

        # Act
        await asyncio.gather(
            add_entity(
                database=mongo_client[TEST_DATABASE],
                embedding_model=embedding_model,
                resolver=resolver,
                user_id=_USER_ID,
                name="apple corp",
                entity_type=NodeType.PERSON,
                properties={},
                source_id="doc1",
                dedup_config=config,
            ),
            add_entity(
                database=mongo_client[TEST_DATABASE],
                embedding_model=embedding_model,
                resolver=resolver,
                user_id=_USER_ID,
                name="apple llc",
                entity_type=NodeType.PERSON,
                properties={},
                source_id="doc2",
                dedup_config=config,
            ),
        )

        # Assert — both aliases land in the final document.
        doc = await kg_collection.find_one({"_id": "person:apple inc"})
        assert doc is not None
        assert {"apple corp", "apple llc"} <= set(doc["aliases"])


class TestIdempotency:
    async def test_double_call_no_double_append(
        self,
        mongo_client,
        kg_collection,
        embedding_model,
        resolver,
        mocker,
    ) -> None:
        # Arrange
        await kg_collection.insert_one(
            _seed_person("person:apple inc", name="apple inc")
        )
        _patch_dedupe(
            mocker,
            DeduplicationResult(
                action="merged",
                matched_node_id="person:apple inc",
                matched_node_name="apple inc",
                similarity_score=0.97,
                match_type="embedding",
            ),
        )
        config = DeduplicationConfig(merge_strategy=MergeStrategy.KEEP_PRIMARY)

        # Act — call twice with identical inputs.
        for _ in range(2):
            await add_entity(
                database=mongo_client[TEST_DATABASE],
                embedding_model=embedding_model,
                resolver=resolver,
                user_id=_USER_ID,
                name="apple corp",
                entity_type=NodeType.PERSON,
                properties={},
                source_id="doc1",
                dedup_config=config,
            )

        # Assert — single alias, single source (set-union semantics).
        doc = await kg_collection.find_one({"_id": "person:apple inc"})
        assert doc is not None
        assert doc["aliases"].count("apple corp") == 1
        assert doc["sources"].count("doc1") == 1
