"""Unit tests for ``dedup.DeduplicationConfig`` and read-only invariants.

These tests cover behavior that does not require MongoDB: config
validation, the ``enabled=False`` short-circuit, and the read-only
invariant on ``dedupe_entity``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from beanie import PydanticObjectId

from tree.entities.knowledge_graph import NodeType
from tree.memory.extraction.dedup import (
    DeduplicationConfig,
    DeduplicationResult,
    MergeStrategy,
    dedupe_entity,
)

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


class TestDeduplicationConfigDefaults:
    def test_defaults_construct(self) -> None:
        # Arrange / Act
        config = DeduplicationConfig()

        # Assert
        assert config.enabled is True
        assert config.auto_merge_threshold == 0.95
        assert config.flag_threshold == 0.85
        assert config.use_fuzzy_matching is True
        assert config.fuzzy_threshold == 0.90
        assert config.max_candidates == 10
        assert config.match_same_type_only is True
        assert config.merge_strategy is MergeStrategy.KEEP_PRIMARY


class TestDeduplicationConfigValidation:
    def test_auto_merge_must_exceed_flag(self) -> None:
        with pytest.raises(ValueError) as exc:
            DeduplicationConfig(auto_merge_threshold=0.5, flag_threshold=0.8)

        message = str(exc.value)
        assert "auto_merge_threshold" in message
        assert "flag_threshold" in message

    def test_max_candidates_must_be_positive(self) -> None:
        with pytest.raises(ValueError) as exc:
            DeduplicationConfig(max_candidates=0)

        assert "max_candidates" in str(exc.value)

    def test_max_candidates_negative_rejected(self) -> None:
        with pytest.raises(ValueError) as exc:
            DeduplicationConfig(max_candidates=-1)

        assert "max_candidates" in str(exc.value)

    def test_auto_merge_above_one_rejected(self) -> None:
        with pytest.raises(ValueError) as exc:
            DeduplicationConfig(auto_merge_threshold=1.5)

        assert "auto_merge_threshold" in str(exc.value)

    def test_auto_merge_below_zero_rejected(self) -> None:
        with pytest.raises(ValueError) as exc:
            DeduplicationConfig(auto_merge_threshold=-0.1, flag_threshold=-0.2)

        # The auto_merge range check fires first.
        assert "auto_merge_threshold" in str(exc.value)

    def test_flag_threshold_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError) as exc:
            DeduplicationConfig(auto_merge_threshold=0.5, flag_threshold=1.2)

        assert "flag_threshold" in str(exc.value)

    def test_fuzzy_threshold_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError) as exc:
            DeduplicationConfig(fuzzy_threshold=2.0)

        assert "fuzzy_threshold" in str(exc.value)

    def test_equal_thresholds_rejected(self) -> None:
        """auto_merge must be *strictly* greater than flag."""

        with pytest.raises(ValueError) as exc:
            DeduplicationConfig(auto_merge_threshold=0.85, flag_threshold=0.85)

        assert "auto_merge_threshold" in str(exc.value)


class TestDedupeEntityShortCircuit:
    async def test_enabled_false_skips_database(self, mocker) -> None:
        """When ``enabled=False``, ``dedupe_entity`` must not touch Mongo."""

        # Arrange — a MagicMock database whose attribute access we will spy on.
        database = MagicMock(name="database")
        config = DeduplicationConfig(enabled=False)

        # Act
        result = await dedupe_entity(
            database=database,
            user_id=_USER_ID,
            name="alice",
            entity_type=NodeType.PERSON,
            embedding=[0.1, 0.2, 0.3],
            config=config,
        )

        # Assert — short-circuited "none" result.
        assert isinstance(result, DeduplicationResult)
        assert result.action == "none"
        assert result.matched_node_id is None
        assert result.matched_node_name is None
        assert result.similarity_score == 0.0
        assert result.match_type is None

        # Assert — no database / collection access happened at all.
        assert database.mock_calls == [], (
            "dedupe_entity must not access the database when enabled=False"
        )


class TestDedupeEntityReadOnlyInvariant:
    """``dedupe_entity`` must never invoke a write method on the collection.

    This isn't about asserting "no Mongo at all" — when ``enabled=True`` the
    function legitimately calls ``aggregate``. The invariant is that none of
    the *write* methods are touched, regardless of branch.
    """

    @pytest.mark.parametrize("enabled", [True, False])
    async def test_no_write_methods_invoked(self, mocker, enabled: bool) -> None:
        # Arrange — a database whose ``[name]`` returns a collection mock
        # with an empty async aggregate cursor.
        async def _empty_async_iter():
            for _ in []:
                yield _

        collection = MagicMock(name="collection")
        collection.aggregate = mocker.AsyncMock(return_value=_empty_async_iter())

        database = MagicMock(name="database")
        database.__getitem__.return_value = collection

        config = DeduplicationConfig(enabled=enabled)

        # Spies on every write method that might be called.
        insert_one_spy = mocker.spy(collection, "insert_one")
        insert_many_spy = mocker.spy(collection, "insert_many")
        update_one_spy = mocker.spy(collection, "update_one")
        update_many_spy = mocker.spy(collection, "update_many")
        bulk_write_spy = mocker.spy(collection, "bulk_write")
        delete_one_spy = mocker.spy(collection, "delete_one")
        delete_many_spy = mocker.spy(collection, "delete_many")
        replace_one_spy = mocker.spy(collection, "replace_one")

        # Act
        await dedupe_entity(
            database=database,
            user_id=_USER_ID,
            name="alice",
            entity_type=NodeType.PERSON,
            embedding=[0.1, 0.2, 0.3],
            config=config,
        )

        # Assert — no write methods called.
        assert insert_one_spy.call_count == 0
        assert insert_many_spy.call_count == 0
        assert update_one_spy.call_count == 0
        assert update_many_spy.call_count == 0
        assert bulk_write_spy.call_count == 0
        assert delete_one_spy.call_count == 0
        assert delete_many_spy.call_count == 0
        assert replace_one_spy.call_count == 0
