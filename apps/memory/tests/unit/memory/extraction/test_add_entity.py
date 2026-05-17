"""Unit tests for ``add_entity`` with a mocked Motor collection.

The integration suite verifies the actual aggregation semantics against
Atlas-local. These unit tests focus on the **dispatch contract**:

* ``_id`` derivation by ``dedup.action``.
* The 3x3 (strategy x action) matrix of expected ``update_one`` calls.
* SAME_AS edge writes happen iff ``action == "flagged"``.
* ``applied_strategy`` is stamped iff ``action == "merged"``.
* Short-circuit when ``resolve=False, deduplicate=False``.
* Input validation (empty name, out-of-range confidence).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.entities.knowledge_graph import NodeType
from tree.memory.extraction.add_entity import add_entity
from tree.memory.extraction.dedup import (
    DeduplicationConfig,
    DeduplicationResult,
    MergeStrategy,
)
from tree.memory.resolution.types import ResolvedEntity


# A stable user_id used across the suite. Real ``PydanticObjectId`` so
# the prospective_id includes a realistic 24-hex-char prefix.
_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_PH = str(_USER_ID)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_database(mocker) -> tuple[Any, Any]:
    """Build a mocked database whose ``[name]`` returns a mocked collection.

    The collection has ``update_one`` and ``aggregate`` as ``AsyncMock``s.
    """

    collection = MagicMock(name="kg_collection")
    collection.update_one = AsyncMock(return_value=MagicMock())

    async def _empty_async_iter():
        for _ in []:
            yield _

    collection.aggregate = AsyncMock(return_value=_empty_async_iter())

    database = MagicMock(name="database")
    database.__getitem__.return_value = collection
    return database, collection


def _make_resolver(canonical_name: str = "alice", match_type: str = "exact") -> Any:
    """Build a resolver mock whose ``resolve`` returns a canned ResolvedEntity."""

    resolver = MagicMock(name="resolver")
    resolver.resolve = AsyncMock(
        return_value=ResolvedEntity(
            original_name="raw",
            canonical_name=canonical_name,
            entity_type=NodeType.PERSON,
            confidence=0.9,
            match_type=match_type,  # type: ignore[arg-type]
        )
    )
    return resolver


def _make_embedding_model() -> Any:
    """Embedding model that always returns one 8-dim zero vector."""
    model = MagicMock(name="embedding_model")
    model.embed = AsyncMock(return_value=[[0.0] * 8])
    return model


def _patch_dedupe_entity(mocker, dedup_result: DeduplicationResult) -> Any:
    """Patch ``tree.memory.extraction.add_entity.dedupe_entity`` to return ``dedup_result``."""
    return mocker.patch(
        "tree.memory.extraction.add_entity.dedupe_entity",
        new=AsyncMock(return_value=dedup_result),
    )


def _count_same_as_calls(collection: Any) -> int:
    """Count ``update_one`` calls whose target ``_id`` matches a SAME_AS edge pattern."""

    count = 0
    for call in collection.update_one.call_args_list:
        args, kwargs = call
        filter_arg = args[0] if args else kwargs.get("filter")
        candidate_id = filter_arg.get("_id") if isinstance(filter_arg, dict) else None
        if isinstance(candidate_id, str) and "|same_as|" in candidate_id:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestAddEntityInputValidation:
    async def test_empty_name_rejected(self, mocker) -> None:
        database, _ = _make_database(mocker)
        with pytest.raises(ValueError, match="non-empty"):
            await add_entity(
                database=database,
                embedding_model=_make_embedding_model(),
                resolver=_make_resolver(),
                user_id=_USER_ID,
                name="   ",
                entity_type=NodeType.PERSON,
                properties={},
                source_id="src1",
                dedup_config=DeduplicationConfig(),
            )

    async def test_confidence_above_one_rejected(self, mocker) -> None:
        database, _ = _make_database(mocker)
        with pytest.raises(ValueError, match="confidence"):
            await add_entity(
                database=database,
                embedding_model=_make_embedding_model(),
                resolver=_make_resolver(),
                user_id=_USER_ID,
                name="alice",
                entity_type=NodeType.PERSON,
                properties={"confidence": 1.5},
                source_id="src1",
                dedup_config=DeduplicationConfig(),
            )

    async def test_confidence_below_zero_rejected(self, mocker) -> None:
        database, _ = _make_database(mocker)
        with pytest.raises(ValueError, match="confidence"):
            await add_entity(
                database=database,
                embedding_model=_make_embedding_model(),
                resolver=_make_resolver(),
                user_id=_USER_ID,
                name="alice",
                entity_type=NodeType.PERSON,
                properties={"confidence": -0.1},
                source_id="src1",
                dedup_config=DeduplicationConfig(),
            )

    async def test_missing_user_id_raises_type_error(self, mocker) -> None:
        """``add_entity`` refuses to run without ``user_id``."""

        database, _ = _make_database(mocker)
        with pytest.raises(TypeError, match="user_id"):
            await add_entity(  # type: ignore[call-arg]
                database=database,
                embedding_model=_make_embedding_model(),
                resolver=_make_resolver(),
                name="alice",
                entity_type=NodeType.PERSON,
                properties={},
                source_id="src1",
                dedup_config=DeduplicationConfig(),
            )


# ---------------------------------------------------------------------------
# Short-circuit
# ---------------------------------------------------------------------------


class TestAddEntityShortCircuit:
    async def test_resolve_false_dedup_false_single_upsert(self, mocker) -> None:
        # Arrange
        database, collection = _make_database(mocker)
        resolver = _make_resolver()
        embedding_model = _make_embedding_model()
        dedupe_spy = _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))

        # Act
        target_id, resolved, dedup_result = await add_entity(
            database=database,
            embedding_model=embedding_model,
            resolver=resolver,
            user_id=_USER_ID,
            name="Alice",
            entity_type=NodeType.PERSON,
            properties={"email": "a@x.com"},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
            resolve=False,
            deduplicate=False,
        )

        # Assert — single upsert at the canonical _id, prefixed with user_id.
        assert target_id == f"{_PH}:person:alice"
        assert resolved.match_type == "none"
        assert dedup_result.action == "none"
        assert collection.update_one.call_count == 1
        assert _count_same_as_calls(collection) == 0
        # The upserted document carries the bound user_id.
        node_call = collection.update_one.call_args_list[0]
        set_stage = node_call.args[1][0]["$set"]
        assert set_stage["user_id"] == _USER_ID
        # Resolver and dedup must not have been called.
        resolver.resolve.assert_not_called()
        dedupe_spy.assert_not_called()
        embedding_model.embed.assert_not_called()


# ---------------------------------------------------------------------------
# 3x3 strategy x action matrix
# ---------------------------------------------------------------------------


_ALL_STRATEGIES = [
    MergeStrategy.KEEP_PRIMARY,
    MergeStrategy.MERGE_PROPERTIES,
    MergeStrategy.KEEP_ALIASES,
]


class TestAddEntityMergedAction:
    """``action == "merged"`` → one update on the canonical, no SAME_AS edge.

    ``applied_strategy`` is set on the returned ``DeduplicationResult``.
    """

    @pytest.mark.parametrize("strategy", _ALL_STRATEGIES)
    async def test_merged_emits_single_update_at_canonical(
        self, mocker, strategy: MergeStrategy
    ) -> None:
        # Arrange
        database, collection = _make_database(mocker)
        dedup_result = DeduplicationResult(
            action="merged",
            matched_node_id="person:alice_canonical",
            matched_node_name="alice canonical",
            similarity_score=0.97,
            match_type="embedding",
        )
        _patch_dedupe_entity(mocker, dedup_result)
        config = DeduplicationConfig(merge_strategy=strategy)

        # Act
        target_id, _resolved, returned = await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=_USER_ID,
            name="alice corp",
            entity_type=NodeType.PERSON,
            properties={"description": "a longer description"},
            source_id="src1",
            dedup_config=config,
        )

        # Assert
        assert target_id == "person:alice_canonical"
        assert returned.applied_strategy is strategy
        # Exactly one update_one (the merge), and it targets the canonical.
        assert collection.update_one.call_count == 1
        merge_args = collection.update_one.call_args_list[0]
        assert merge_args.args[0] == {"_id": "person:alice_canonical"}
        # No SAME_AS edge written.
        assert _count_same_as_calls(collection) == 0


class TestAddEntityFlaggedAction:
    """``action == "flagged"`` → upsert the new node + one SAME_AS edge."""

    @pytest.mark.parametrize("strategy", _ALL_STRATEGIES)
    async def test_flagged_emits_node_and_same_as_edge(
        self, mocker, strategy: MergeStrategy
    ) -> None:
        # Arrange
        database, collection = _make_database(mocker)
        dedup_result = DeduplicationResult(
            action="flagged",
            matched_node_id="person:alice_smith",
            matched_node_name="alice smith",
            similarity_score=0.88,
            match_type="embedding",
        )
        _patch_dedupe_entity(mocker, dedup_result)
        config = DeduplicationConfig(merge_strategy=strategy)

        # Act
        target_id, _resolved, returned = await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=_USER_ID,
            name="alyce smyth",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=config,
        )

        # Assert
        assert target_id == f"{_PH}:person:alyce smyth"
        assert returned.applied_strategy is None  # only set on merged
        # Two upserts: the new node + the SAME_AS edge.
        assert collection.update_one.call_count == 2
        # First call upserts at the new node id.
        node_args = collection.update_one.call_args_list[0]
        assert node_args.args[0] == {"_id": f"{_PH}:person:alyce smyth"}
        # Second call upserts the SAME_AS edge.
        edge_args = collection.update_one.call_args_list[1]
        assert (
            edge_args.args[0]["_id"]
            == f"{_PH}:person:alyce smyth|same_as|person:alice_smith"
        )
        # Edge payload carries pending status + confidence + match_type and user_id.
        edge_update = edge_args.args[1]
        assert edge_update["$setOnInsert"]["properties.status"] == "pending"
        assert edge_update["$setOnInsert"]["user_id"] == _USER_ID
        assert edge_update["$set"]["properties.confidence"] == 0.88
        assert edge_update["$set"]["properties.match_type"] == "embedding"
        assert edge_args.kwargs.get("upsert") is True
        # Exactly one SAME_AS edge.
        assert _count_same_as_calls(collection) == 1


class TestAddEntityNoneAction:
    """``action == "none"`` → upsert the new node, no SAME_AS edge."""

    @pytest.mark.parametrize("strategy", _ALL_STRATEGIES)
    async def test_none_emits_node_only(self, mocker, strategy: MergeStrategy) -> None:
        # Arrange
        database, collection = _make_database(mocker)
        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))
        config = DeduplicationConfig(merge_strategy=strategy)

        # Act
        target_id, _resolved, returned = await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(canonical_name="apple inc"),
            user_id=_USER_ID,
            name="apple",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=config,
        )

        # Assert
        assert target_id == f"{_PH}:person:apple"
        assert returned.applied_strategy is None
        # Exactly one update_one (the new node), no SAME_AS edge.
        assert collection.update_one.call_count == 1
        node_args = collection.update_one.call_args_list[0]
        assert node_args.args[0] == {"_id": f"{_PH}:person:apple"}
        assert _count_same_as_calls(collection) == 0


# ---------------------------------------------------------------------------
# canonical_name written on the new node
# ---------------------------------------------------------------------------


class TestAddEntityCanonicalNameWritten:
    async def test_canonical_name_from_resolver_on_none(self, mocker) -> None:
        """On ``action="none"``, ``canonical_name`` comes from the resolver."""

        # Arrange
        database, collection = _make_database(mocker)
        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))

        # Act
        await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(canonical_name="apple inc", match_type="exact"),
            user_id=_USER_ID,
            name="apple",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert — the $set stage carries canonical_name="apple inc".
        node_call = collection.update_one.call_args_list[0]
        pipeline = node_call.args[1]
        set_stage = pipeline[0]["$set"]
        assert set_stage["canonical_name"] == "apple inc"
        # user_id is stamped on the upsert.
        assert set_stage["user_id"] == _USER_ID


# ---------------------------------------------------------------------------
# Self-match exclusion
# ---------------------------------------------------------------------------


class TestAddEntitySelfMatchExclusion:
    """When dedup returns a self-match (top candidate == prospective_id),
    ``add_entity`` must NOT merge into itself.
    """

    async def test_self_match_is_filtered(self, mocker) -> None:
        # Arrange — dedupe says "merged" with matched_node_id == prospective_id.
        database, collection = _make_database(mocker)
        dedup_result = DeduplicationResult(
            action="merged",
            matched_node_id=f"{_PH}:person:alice",
            matched_node_name="alice",
            similarity_score=1.0,
            match_type="embedding",
        )
        _patch_dedupe_entity(mocker, dedup_result)

        # Act — prospective_id is also the user_id-prefixed person:alice.
        target_id, _resolved, returned = await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=_USER_ID,
            name="Alice",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert — fall back to the new-node path (action="none").
        assert target_id == f"{_PH}:person:alice"
        assert returned.action == "none"
        # No merge into the same node, no SAME_AS edge.
        assert collection.update_one.call_count == 1
        assert _count_same_as_calls(collection) == 0


# ---------------------------------------------------------------------------
# disabled-config short-circuit
# ---------------------------------------------------------------------------


class TestAddEntityDedupDisabled:
    async def test_dedup_config_disabled_skips_dedupe_entity(self, mocker) -> None:
        # Arrange
        database, collection = _make_database(mocker)
        dedupe_spy = _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))
        config = DeduplicationConfig(enabled=False)

        # Act
        target_id, _resolved, dedup_result = await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=_USER_ID,
            name="alice",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=config,
        )

        # Assert — dedupe_entity not called; behaves like action="none".
        assert target_id == f"{_PH}:person:alice"
        assert dedup_result.action == "none"
        assert collection.update_one.call_count == 1
        dedupe_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Two-user isolation
# ---------------------------------------------------------------------------


class TestAddEntityTwoUserIsolation:
    """Two users writing the same name produce two distinct rows."""

    async def test_two_users_distinct_ids(self, mocker) -> None:
        user_a = PydanticObjectId("507f1f77bcf86cd799439011")
        user_b = PydanticObjectId("507f1f77bcf86cd799439022")

        database_a, collection_a = _make_database(mocker)
        database_b, collection_b = _make_database(mocker)

        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))

        target_a, _, _ = await add_entity(
            database=database_a,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=user_a,
            name="alice",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )
        target_b, _, _ = await add_entity(
            database=database_b,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=user_b,
            name="alice",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        assert target_a != target_b
        assert target_a.startswith(f"{user_a}:")
        assert target_b.startswith(f"{user_b}:")
