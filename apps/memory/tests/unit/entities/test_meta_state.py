"""Unit tests for the #050 meta-state entity.

Pure schema checks — no live Mongo. Round-trip the Beanie document,
pin the deterministic ``_id`` builder, the tz-aware validator, and the
index declaration.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from beanie import PydanticObjectId
from pymongo import IndexModel

from tree.entities.meta_state import (
    KnowledgeGraphMetaState,
    build_meta_state_id,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


class TestBuildMetaStateId:
    def test_id_is_user_id_colon_job(self) -> None:
        user_id = PydanticObjectId()
        assert build_meta_state_id(user_id, "dream") == f"{user_id}:dream"

    @pytest.mark.parametrize("job", ["dream", "compaction", "reindex"])
    def test_id_uses_supplied_job(self, job: str) -> None:
        user_id = PydanticObjectId()
        assert build_meta_state_id(user_id, job) == f"{user_id}:{job}"

    def test_two_users_get_distinct_ids(self) -> None:
        a, b = PydanticObjectId(), PydanticObjectId()
        assert build_meta_state_id(a, "dream") != build_meta_state_id(b, "dream")


class TestMetaStateRoundTrip:
    def test_basic_construction(self) -> None:
        user_id = PydanticObjectId()
        ts = _now()
        doc = KnowledgeGraphMetaState(
            id=build_meta_state_id(user_id, "dream"),
            user_id=user_id,
            job="dream",
            last_run_at=ts,
            last_run_id="flow-run-123",
            last_stats={"pairs_examined": 10, "auto_merged": 2},
            updated_at=ts,
        )
        assert doc.id == f"{user_id}:dream"
        assert doc.job == "dream"
        assert doc.last_run_at == ts
        assert doc.last_run_id == "flow-run-123"
        assert doc.last_stats == {"pairs_examined": 10, "auto_merged": 2}

    def test_round_trip_via_model_dump(self) -> None:
        user_id = PydanticObjectId()
        ts = _now()
        original = KnowledgeGraphMetaState(
            id=build_meta_state_id(user_id, "dream"),
            user_id=user_id,
            job="dream",
            last_run_at=ts,
            last_run_id=None,
            last_stats={},
            updated_at=ts,
        )
        rehydrated = KnowledgeGraphMetaState.model_validate(original.model_dump())
        assert rehydrated.user_id == user_id
        assert rehydrated.job == "dream"
        assert rehydrated.last_run_at == ts
        assert rehydrated.last_run_id is None
        assert rehydrated.last_stats == {}

    def test_defaults_for_optional_fields(self) -> None:
        user_id = PydanticObjectId()
        ts = _now()
        doc = KnowledgeGraphMetaState(
            id=build_meta_state_id(user_id, "dream"),
            user_id=user_id,
            job="dream",
            last_run_at=ts,
            updated_at=ts,
        )
        assert doc.last_run_id is None
        assert doc.last_stats == {}


class TestTzAwareEnforcement:
    def test_naive_last_run_at_rejected(self) -> None:
        user_id = PydanticObjectId()
        with pytest.raises(ValueError, match="timezone-aware"):
            KnowledgeGraphMetaState(
                id=build_meta_state_id(user_id, "dream"),
                user_id=user_id,
                job="dream",
                last_run_at=datetime(2026, 1, 1),  # naive
                updated_at=_now(),
            )

    def test_naive_updated_at_rejected(self) -> None:
        user_id = PydanticObjectId()
        with pytest.raises(ValueError, match="timezone-aware"):
            KnowledgeGraphMetaState(
                id=build_meta_state_id(user_id, "dream"),
                user_id=user_id,
                job="dream",
                last_run_at=_now(),
                updated_at=datetime(2026, 1, 1),  # naive
            )

    def test_non_utc_tz_aware_accepted(self) -> None:
        # Any tz-aware datetime is accepted; the project rule is "no naive",
        # not "UTC-only at the type level".
        user_id = PydanticObjectId()
        aware = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=5)
        doc = KnowledgeGraphMetaState(
            id=build_meta_state_id(user_id, "dream"),
            user_id=user_id,
            job="dream",
            last_run_at=aware,
            updated_at=aware,
        )
        assert doc.last_run_at.tzinfo is not None


class TestIndexes:
    def test_user_job_index_declared(self) -> None:
        index_models: list[IndexModel] = list(KnowledgeGraphMetaState.Settings.indexes)
        target_key = [("user_id", 1), ("job", 1)]
        assert any(
            list(im.document.get("key", {}).items()) == target_key
            for im in index_models
        )

    def test_collection_name(self) -> None:
        assert KnowledgeGraphMetaState.Settings.name == "knowledge_graph_meta_state"
