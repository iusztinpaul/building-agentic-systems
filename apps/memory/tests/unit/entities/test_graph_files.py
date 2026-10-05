"""Unit tests for the ADR-014 §4 **Graph file** entity (task 185).

Pins the row's shape, the tz-aware ``created_at``, the exactly-two index
declaration (the TTL read from ``mcp.graph_file_ttl_seconds`` and the compound
``{name, user_id}`` the read uses), the registration that makes the
``graph_files`` collection real, and one live round-trip through Beanie.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta, timezone

import pytest
from beanie import PydanticObjectId
from pydantic import ValidationError

from tree.config.app_config import app_config
from tree.db import ALL_DOCUMENT_MODELS
from tree.entities.graph_files import (
    GRAPH_FILE_TTL_INDEX,
    GRAPH_FILES_COLLECTION,
    GraphFile,
    graph_file_indexes,
)


def _graph_file(**overrides: object) -> GraphFile:
    kwargs: dict[str, object] = {
        "user_id": PydanticObjectId(),
        "name": "abc123.html.gz",
        "html_gz": gzip.compress(b"<html></html>"),
    }
    kwargs.update(overrides)
    return GraphFile(**kwargs)  # type: ignore[arg-type]


def _index_documents(ttl_seconds: int | None = None) -> dict[str, dict]:
    indexes = (
        GraphFile.Settings.indexes
        if ttl_seconds is None
        else graph_file_indexes(ttl_seconds)
    )
    return {im.document["name"]: im.document for im in indexes}


class TestGraphFileShape:
    def test_carries_user_name_and_gzip_bytes(self) -> None:
        user_id = PydanticObjectId()
        blob = gzip.compress(b"<html>graph</html>")

        row = _graph_file(user_id=user_id, name="tok.html.gz", html_gz=blob)

        assert (row.user_id, row.name, row.html_gz) == (user_id, "tok.html.gz", blob)

    @pytest.mark.parametrize("missing", ["user_id", "name", "html_gz"])
    def test_required_field_missing_raises(self, missing: str) -> None:
        kwargs: dict[str, object] = {
            "user_id": PydanticObjectId(),
            "name": "tok.html.gz",
            "html_gz": b"\x1f\x8b",
        }
        del kwargs[missing]

        with pytest.raises(ValidationError, match=missing):
            GraphFile(**kwargs)  # type: ignore[arg-type]

    def test_every_field_documents_itself(self) -> None:
        for name in ("user_id", "name", "html_gz", "created_at"):
            assert GraphFile.model_fields[name].description, name


class TestCreatedAt:
    def test_defaults_to_a_utc_aware_now(self) -> None:
        before = datetime.now(UTC)

        row = _graph_file()

        assert row.created_at.tzinfo is not None
        assert row.created_at.utcoffset() == timedelta(0)
        assert before <= row.created_at <= datetime.now(UTC)

    def test_naive_created_at_raises(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            _graph_file(created_at=datetime(2026, 10, 5, 12, 0))

    def test_non_utc_aware_created_at_is_accepted(self) -> None:
        plus_two = timezone(timedelta(hours=2))

        row = _graph_file(created_at=datetime(2026, 10, 5, 12, 0, tzinfo=plus_two))

        assert row.created_at.utcoffset() == timedelta(hours=2)


class TestGraphFileIndexes:
    def test_collection_constants_are_the_one_spelling(self) -> None:
        assert (GRAPH_FILES_COLLECTION, GRAPH_FILE_TTL_INDEX) == (
            "graph_files",
            "created_at_ttl",
        )
        assert GraphFile.Settings.name == GRAPH_FILES_COLLECTION

    def test_declares_exactly_the_ttl_and_the_compound_unique_index(self) -> None:
        documents = _index_documents()

        assert set(documents) == {GRAPH_FILE_TTL_INDEX, "name_user_id_unique"}

    def test_ttl_index_is_on_created_at_with_the_configured_ttl(self) -> None:
        ttl = _index_documents()[GRAPH_FILE_TTL_INDEX]

        assert list(ttl["key"].items()) == [("created_at", 1)]
        assert ttl["expireAfterSeconds"] == app_config.mcp.graph_file_ttl_seconds

    def test_compound_index_is_unique_on_name_then_user_id(self) -> None:
        compound = _index_documents()["name_user_id_unique"]

        assert list(compound["key"].items()) == [("name", 1), ("user_id", 1)]
        assert compound["unique"] is True

    def test_builder_carries_the_given_ttl(self) -> None:
        ttl = _index_documents(ttl_seconds=120)[GRAPH_FILE_TTL_INDEX]

        assert ttl["expireAfterSeconds"] == 120

    def test_registered_with_beanie(self) -> None:
        assert GraphFile in ALL_DOCUMENT_MODELS

    async def test_inserted_row_lands_in_graph_files(self) -> None:
        row = _graph_file(name="roundtrip.html.gz")

        await row.insert()
        try:
            raw = await GraphFile.get_pymongo_collection().find_one({"_id": row.id})
        finally:
            await row.delete()

        assert raw is not None
        assert raw["html_gz"] == row.html_gz
        assert raw["user_id"] == row.user_id
