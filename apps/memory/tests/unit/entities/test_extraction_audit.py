"""Unit tests for the #030 audit-collection ODMs.

Round-trip both Documents through ``model_validate`` and pin the
index declarations. No live Mongo — these are pure schema checks.
The integration test (``test_validator_e2e.py``) covers the live
write path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from beanie import PydanticObjectId
from pymongo import IndexModel

from tree.entities.extraction_audit import (
    ExtractionDroppedField,
    ExtractionRejection,
    truncate_raw_row,
    truncate_raw_value,
)
from tree.entities.knowledge_graph import ExtractorInfo


def _now() -> datetime:
    return datetime.now(tz=UTC)


class TestExtractionRejectionRoundTrip:
    def test_basic_construction(self) -> None:
        row = ExtractionRejection(
            user_id=PydanticObjectId(),
            chunk_id="chunk-1",
            timestamp=_now(),
            rejected_at_stage="envelope",
            rejection_reason="unknown_type",
            raw_row={"type": "dragon", "name": "smaug"},
            extractor=ExtractorInfo(name="gemini-2.5-pro", version="tree-memory-0.1.0"),
        )
        assert row.rejection_reason == "unknown_type"
        assert row.raw_row == {"type": "dragon", "name": "smaug"}
        assert row.extractor is not None
        assert row.extractor.name == "gemini-2.5-pro"

    def test_round_trip_via_model_dump(self) -> None:
        user_id = PydanticObjectId()
        ts = _now()
        original = ExtractionRejection(
            user_id=user_id,
            timestamp=ts,
            rejection_reason="missing_subtype",
            raw_row={"type": "person", "name": "alice"},
        )
        dumped = original.model_dump()
        rehydrated = ExtractionRejection.model_validate(dumped)
        assert rehydrated.user_id == user_id
        assert rehydrated.rejection_reason == "missing_subtype"
        assert rehydrated.raw_row == {"type": "person", "name": "alice"}
        assert rehydrated.extractor is None

    def test_default_stage_is_envelope(self) -> None:
        row = ExtractionRejection(
            user_id=PydanticObjectId(),
            timestamp=_now(),
            rejection_reason="unknown_type",
        )
        assert row.rejected_at_stage == "envelope"


class TestExtractionDroppedFieldRoundTrip:
    def test_basic_construction(self) -> None:
        row = ExtractionDroppedField(
            user_id=PydanticObjectId(),
            chunk_id="chunk-7",
            timestamp=_now(),
            row_type="person",
            row_subtype="individual",
            dropped_field="email",
            raw_value=12345,
            reason="email: input should be a valid string",
        )
        assert row.row_type == "person"
        assert row.dropped_field == "email"
        assert row.raw_value == 12345

    def test_round_trip_via_model_dump(self) -> None:
        original = ExtractionDroppedField(
            user_id=PydanticObjectId(),
            timestamp=_now(),
            row_type="related_to",
            semantic_type="employed_by",
            dropped_field="role",
            raw_value=["bad"],
            reason="role: input should be a valid string",
            extractor=ExtractorInfo(name="gemini-2.5-pro", version="tree-memory-0.1.0"),
        )
        rehydrated = ExtractionDroppedField.model_validate(original.model_dump())
        assert rehydrated.row_type == "related_to"
        assert rehydrated.semantic_type == "employed_by"
        assert rehydrated.dropped_field == "role"
        assert rehydrated.raw_value == ["bad"]


# ---------------------------------------------------------------------------
# Index declarations
# ---------------------------------------------------------------------------


class TestIndexes:
    def test_rejection_user_timestamp_index_declared(self) -> None:
        index_models: list[IndexModel] = list(ExtractionRejection.Settings.indexes)
        target_key = [("user_id", 1), ("timestamp", -1)]
        assert any(
            list(im.document.get("key", {}).items()) == target_key
            for im in index_models
        )

    def test_rejection_user_reason_index_declared(self) -> None:
        index_models = list(ExtractionRejection.Settings.indexes)
        target_key = [("user_id", 1), ("rejection_reason", 1)]
        assert any(
            list(im.document.get("key", {}).items()) == target_key
            for im in index_models
        )

    def test_dropped_field_user_type_field_index_declared(self) -> None:
        index_models = list(ExtractionDroppedField.Settings.indexes)
        target_key = [("user_id", 1), ("row_type", 1), ("dropped_field", 1)]
        assert any(
            list(im.document.get("key", {}).items()) == target_key
            for im in index_models
        )

    def test_dropped_field_user_timestamp_index_declared(self) -> None:
        index_models = list(ExtractionDroppedField.Settings.indexes)
        target_key = [("user_id", 1), ("timestamp", -1)]
        assert any(
            list(im.document.get("key", {}).items()) == target_key
            for im in index_models
        )


# ---------------------------------------------------------------------------
# Truncation helpers
# ---------------------------------------------------------------------------


class TestTruncationHelpers:
    def test_small_value_passes_through(self) -> None:
        assert truncate_raw_value("alice") == "alice"
        assert truncate_raw_value(42) == 42

    def test_large_value_truncated(self) -> None:
        huge = "x" * 4096
        out = truncate_raw_value(huge)
        # When the value can't fit, the helper returns a truncation
        # marker string instead of the original value.
        assert isinstance(out, str)
        assert "truncated" in out

    def test_small_raw_row_passes_through(self) -> None:
        row = {"type": "person", "name": "alice"}
        assert truncate_raw_row(row) == row

    @pytest.mark.parametrize("key", ["a", "b", "c"])
    def test_raw_row_preserves_shape_under_truncation(self, key: str) -> None:
        huge = "x" * 100_000
        row = {key: huge, "other": "small"}
        out = truncate_raw_row(row)
        assert set(out) == {key, "other"}
        # ``other`` is small and survives.
        assert out["other"] == "small"
