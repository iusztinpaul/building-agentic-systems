"""Unit tests for the ADR-007 §3 **Memory cluster** entity (#115).

Schema-level checks plus one live round-trip through Beanie: pin the
deterministic ``_id`` builder, the "noise is never a row" and tz-aware
validators, the index declaration, and the registration that makes the
``memory_clusters`` collection real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from beanie import PydanticObjectId
from pydantic import BaseModel, ValidationError
from pymongo import IndexModel

import tree.entities as entities
from tree.db import ALL_DOCUMENT_MODELS
from tree.entities.clusters import (
    MEMORY_CLUSTERS_COLLECTION,
    ClusterCentroid,
    MemoryCluster,
    build_cluster_id,
)


# Beanie declares these on every Document; they carry no project-authored
# ``Field(description=...)`` and are not part of this row's contract.
_BEANIE_OWNED_FIELDS = {"id", "revision_id"}


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _cluster_kwargs(**overrides: object) -> dict[str, object]:
    user_id = overrides.pop("user_id", None) or PydanticObjectId()
    cluster_id = overrides.pop("cluster_id", 3)
    kwargs: dict[str, object] = {
        "id": build_cluster_id(user_id, cluster_id),  # type: ignore[arg-type]
        "user_id": user_id,
        "run_id": "flow-run-123",
        "cluster_id": cluster_id,
        "label": "Agent memory design",
        "summary": "Chunks about designing the memory layer of an agent.",
        "keywords": ["memory", "agents", "design"],
        "size": 42,
        "sample_chunk_ids": ["u:chunk:doc#0-0", "u:chunk:doc#0-1"],
        "centroid": {"x": 1.5, "y": -0.5},
        "created_at": _now(),
    }
    kwargs.update(overrides)
    return kwargs


class TestBuildClusterId:
    def test_id_is_user_id_colon_cluster_colon_cluster_id(self) -> None:
        user_id = PydanticObjectId()

        assert build_cluster_id(user_id, 3) == f"{user_id}:cluster:3"

    def test_two_users_get_distinct_ids(self) -> None:
        a, b = PydanticObjectId(), PydanticObjectId()

        assert build_cluster_id(a, 0) != build_cluster_id(b, 0)

    def test_two_clusters_of_one_user_get_distinct_ids(self) -> None:
        user_id = PydanticObjectId()

        assert build_cluster_id(user_id, 0) != build_cluster_id(user_id, 1)


class TestClusterCentroid:
    def test_carries_map_space_coordinates(self) -> None:
        centroid = ClusterCentroid(x=0.25, y=-3.5)

        assert (centroid.x, centroid.y) == (0.25, -3.5)

    def test_both_coordinates_are_required(self) -> None:
        with pytest.raises(ValidationError):
            ClusterCentroid(x=0.25)  # type: ignore[call-arg]


class TestMemoryClusterRoundTrip:
    def test_basic_construction(self) -> None:
        user_id = PydanticObjectId()

        cluster = MemoryCluster(**_cluster_kwargs(user_id=user_id))

        assert cluster.id == f"{user_id}:cluster:3"
        assert cluster.cluster_id == 3
        assert cluster.run_id == "flow-run-123"
        assert cluster.size == 42
        assert cluster.centroid == ClusterCentroid(x=1.5, y=-0.5)

    def test_round_trip_via_model_dump(self) -> None:
        original = MemoryCluster(**_cluster_kwargs())

        rehydrated = MemoryCluster.model_validate(original.model_dump())

        assert rehydrated.user_id == original.user_id
        assert rehydrated.keywords == original.keywords
        assert rehydrated.sample_chunk_ids == original.sample_chunk_ids
        assert rehydrated.centroid == original.centroid

    def test_required_fields_are_the_documented_shape(self) -> None:
        """Story 3: a developer reads the collection shape off the JSON schema."""

        required = set(MemoryCluster.model_json_schema()["required"])

        assert {
            "user_id",
            "run_id",
            "cluster_id",
            "label",
            "summary",
            "keywords",
            "size",
            "sample_chunk_ids",
            "centroid",
            "created_at",
        } <= required

    def test_every_field_documents_itself(self) -> None:
        properties = MemoryCluster.model_json_schema()["properties"]

        for name in MemoryCluster.model_fields:
            if name in _BEANIE_OWNED_FIELDS:
                continue
            description = properties[name].get("description")
            assert description and description.strip(), (
                f"MemoryCluster.{name} is missing Field(description=...)"
            )

    def test_centroid_fields_document_themselves(self) -> None:
        properties = ClusterCentroid.model_json_schema()["properties"]

        for name in ClusterCentroid.model_fields:
            description = properties[name].get("description")
            assert description and description.strip(), (
                f"ClusterCentroid.{name} is missing Field(description=...)"
            )


class TestNoiseIsNeverARow:
    def test_cluster_id_minus_one_is_rejected_naming_noise(self) -> None:
        """ADR-007 §3: noise (``-1``) is stored on the CHUNK and gets no cluster
        row — a ``-1`` row would be a summary of "everything unclustered"."""

        with pytest.raises(ValidationError, match="noise"):
            MemoryCluster(**_cluster_kwargs(cluster_id=-1))

    def test_cluster_id_zero_is_accepted(self) -> None:
        assert MemoryCluster(**_cluster_kwargs(cluster_id=0)).cluster_id == 0

    def test_size_below_one_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MemoryCluster(**_cluster_kwargs(size=0))


class TestTzAwareEnforcement:
    def test_naive_created_at_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            MemoryCluster(**_cluster_kwargs(created_at=datetime(2026, 1, 1)))

    def test_non_utc_tz_aware_accepted(self) -> None:
        aware = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=5)

        cluster = MemoryCluster(**_cluster_kwargs(created_at=aware))

        assert cluster.created_at.tzinfo is not None


class TestCollectionRegistration:
    def test_collection_constant_is_the_one_spelling(self) -> None:
        assert MEMORY_CLUSTERS_COLLECTION == "memory_clusters"

    def test_settings_name_is_the_shared_constant(self) -> None:
        assert MemoryCluster.Settings.name == MEMORY_CLUSTERS_COLLECTION

    def test_registered_with_beanie(self) -> None:
        assert MemoryCluster in ALL_DOCUMENT_MODELS

    def test_user_run_index_declared(self) -> None:
        index_models: list[IndexModel] = list(MemoryCluster.Settings.indexes)
        target_key = [("user_id", 1), ("run_id", 1)]

        assert any(
            list(im.document.get("key", {}).items()) == target_key
            for im in index_models
        )

    async def test_inserted_row_lands_in_the_memory_clusters_collection(self) -> None:
        cluster = MemoryCluster(**_cluster_kwargs())

        await cluster.insert()

        database = MemoryCluster.get_pymongo_collection().database
        assert MEMORY_CLUSTERS_COLLECTION in await database.list_collection_names()

        await cluster.delete()


class TestEntitiesExports:
    @pytest.mark.parametrize(
        "name",
        [
            "MemoryCluster",
            "ClusterCentroid",
            "build_cluster_id",
            "MEMORY_CLUSTERS_COLLECTION",
        ],
    )
    def test_entities_package_exports_the_cluster_surface(self, name: str) -> None:
        """``tree.entities`` is the shared-ODM front door — the clustering
        modules (#116-#118) import from it, not from the private module path."""

        assert name in entities.__all__
        assert hasattr(entities, name)

    def test_cluster_centroid_is_an_embedded_pydantic_model(self) -> None:
        assert issubclass(ClusterCentroid, BaseModel)
