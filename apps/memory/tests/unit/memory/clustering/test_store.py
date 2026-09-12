"""Every Mongo touch of the clustering feature, against the real test database.

ADR-007 §3. These are deliberately NOT mock tests: the claims are about what
lands in (and disappears from) Mongo — "the previous run's rows are gone", "a
re-run changes nothing", "a stale chunk is counted, not drawn" — and a
``bulk_write`` double cannot answer any of them. The suite writes to
``unit_tests_twin`` through Beanie ODMs (so the fixtures obey the same
validators production rows do) and reads back through the raw-pymongo store
functions under test.

Each test owns a FRESH ``user_id``, so the tenant scoping is exercised for free:
a leak between tests would show up as an off-by-N count here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from beanie import PydanticObjectId

from tests.unit.conftest import TEST_DATABASE
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.clusters import (
    MEMORY_CLUSTERS_COLLECTION,
    ClusterCentroid,
    MemoryCluster,
)
from tree.entities.memory import MEMORY_COLLECTION, ChunkViz, MemoryEntry, NodeType
from tree.memory.clustering.store import (
    ChildEmbeddingRow,
    load_child_embeddings,
    load_embedding_map,
    latest_run_id,
    write_clustering_run,
)
from tree.memory.clustering.types import MemoryClusterInfo

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
async def mongo_client():
    """A client on the unit-test database; both collections cleared afterwards."""

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(), TEST_DATABASE
    )
    yield client
    database = client[TEST_DATABASE]
    await database[MEMORY_COLLECTION].delete_many({})
    await database[MEMORY_CLUSTERS_COLLECTION].delete_many({})


@pytest.fixture
def user_id() -> PydanticObjectId:
    """A fresh tenant per test — every query in the store is user-scoped."""

    return PydanticObjectId()


async def _insert_child(
    user_id: PydanticObjectId,
    chunk_id: str,
    *,
    embedding: list[float] | None = None,
    content: str = "child content",
    title: str | None = "Memory for AI Agents",
    heading_path: list[str] | None = None,
    cluster_id: int | None = None,
    viz: ChunkViz | None = None,
) -> MemoryEntry:
    """Insert one **Child chunk** row through the ODM."""

    entry = MemoryEntry(
        id=chunk_id,
        user_id=user_id,
        kind="node",
        type=NodeType.CHUNK,
        subtype="child",
        name=chunk_id,
        properties={
            "content": content,
            "title": title,
            "heading_path": heading_path or ["Retrieval"],
        },
        embedding=[0.1, 0.2, 0.3, 0.4] if embedding is None else embedding,
        cluster_id=cluster_id,
        viz=viz,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await entry.insert()
    return entry


async def _insert_parent(user_id: PydanticObjectId, chunk_id: str) -> None:
    """A **Parent chunk**: deliberately vector-less, never clustered."""

    await MemoryEntry(
        id=chunk_id,
        user_id=user_id,
        kind="node",
        type=NodeType.CHUNK,
        subtype="parent",
        name=chunk_id,
        properties={"content": "parent content"},
        embedding=[],
        created_at=_NOW,
        updated_at=_NOW,
    ).insert()


async def _insert_document(user_id: PydanticObjectId, chunk_id: str) -> None:
    await MemoryEntry(
        id=chunk_id,
        user_id=user_id,
        kind="node",
        type=NodeType.DOCUMENT,
        name=chunk_id,
        properties={"title": "Memory for AI Agents"},
        embedding=[],
        created_at=_NOW,
        updated_at=_NOW,
    ).insert()


async def _insert_cluster(
    user_id: PydanticObjectId,
    cluster_id: int,
    *,
    run_id: str,
    size: int = 3,
    label: str | None = None,
) -> None:
    await MemoryCluster(
        id=f"{user_id}:cluster:{cluster_id}",
        user_id=user_id,
        run_id=run_id,
        cluster_id=cluster_id,
        label=label or f"Old label {cluster_id}",
        summary="An older run's summary.",
        keywords=["memory", "agents", "rag"],
        size=size,
        sample_chunk_ids=[f"c{cluster_id}"],
        centroid=ClusterCentroid(x=0.0, y=0.0),
        created_at=_NOW,
    ).insert()


def _info(
    cluster_id: int, *, size: int = 2, label: str | None = None
) -> MemoryClusterInfo:
    return MemoryClusterInfo(
        cluster_id=cluster_id,
        label=label or f"Agent memory {cluster_id}",
        summary="What these chunks share.",
        keywords=["memory", "agents", "rag"],
        size=size,
        sample_chunk_ids=[f"c{cluster_id}"],
        centroid_x=float(cluster_id),
        centroid_y=float(cluster_id) + 0.5,
    )


async def _rows(client: Any, collection: str, **query: Any) -> list[dict[str, Any]]:
    return await client[TEST_DATABASE][collection].find(query).to_list()


class _RecordingCollection:
    """Delegates to the real collection and records the ``find`` arguments.

    The filter and the projection ARE the contract here, so they are asserted
    literally — while the call still hits Mongo, so the same test proves the
    query is also VALID.
    """

    def __init__(self, collection: Any) -> None:
        self._collection = collection
        self.find_calls: list[tuple[Any, ...]] = []

    def find(self, *args: Any, **kwargs: Any) -> Any:
        self.find_calls.append(args)
        return self._collection.find(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._collection, name)


class _RecordingDatabase:
    """Every ``[collection]`` is the ONE recorded stand-in."""

    def __init__(self, collection: _RecordingCollection) -> None:
        self.collection = collection

    def __getitem__(self, _name: str) -> _RecordingCollection:
        return self.collection


class _RecordingClient:
    """A client whose every ``[database][collection]`` is ONE recorded stand-in."""

    def __init__(self, collection: _RecordingCollection) -> None:
        self.collection = collection
        self._database = _RecordingDatabase(collection)

    def __getitem__(self, _name: str) -> _RecordingDatabase:
        return self._database


@pytest.fixture
def recording_client(mongo_client):
    """``client[db][coll]`` -> a :class:`_RecordingCollection` over ``memory``."""

    recording = _RecordingCollection(mongo_client[TEST_DATABASE][MEMORY_COLLECTION])
    return _RecordingClient(recording)


class TestLoadChildEmbeddings:
    async def test_loads_only_embedded_child_chunks(
        self, mongo_client, user_id
    ) -> None:
        await _insert_document(user_id, "doc1")
        await _insert_parent(user_id, "p1")
        await _insert_child(user_id, "c1")
        await _insert_child(user_id, "c2", embedding=[])

        rows = await load_child_embeddings(mongo_client, TEST_DATABASE, user_id)

        # Parents and documents are vector-less by design; an unembedded child
        # has nothing to project. All three are excluded BY QUERY.
        assert [row.chunk_id for row in rows] == ["c1"]

    async def test_issues_the_exact_child_filter(
        self, recording_client, user_id
    ) -> None:
        """The filter IS the definition of what a run clusters — pin it.

        A silent widening (dropping ``subtype``) would cluster parent chunks and
        draw a map of rows the retrieval layer never returns.
        """

        await load_child_embeddings(recording_client, TEST_DATABASE, user_id)

        assert recording_client.collection.find_calls[0][0] == {
            "user_id": user_id,
            "kind": "node",
            "type": "chunk",
            "subtype": "child",
            "embedding.0": {"$exists": True},
        }

    async def test_projects_only_the_fields_the_run_reads(
        self, recording_client, user_id
    ) -> None:
        await load_child_embeddings(recording_client, TEST_DATABASE, user_id)

        # The whole corpus is loaded at once — a wide projection would pull
        # every row's full properties block into memory for nothing.
        assert recording_client.collection.find_calls[0][1] == {
            "_id": 1,
            "embedding": 1,
            "properties.title": 1,
            "properties.heading_path": 1,
            "properties.content": 1,
        }

    async def test_returns_rows_sorted_by_id(self, mongo_client, user_id) -> None:
        for chunk_id in ("c3", "c1", "c2"):
            await _insert_child(user_id, chunk_id)

        rows = await load_child_embeddings(mongo_client, TEST_DATABASE, user_id)

        # Determinism of the whole recipe rests on this order: same corpus +
        # same seed => same labels and the same map.
        assert [row.chunk_id for row in rows] == ["c1", "c2", "c3"]

    async def test_carries_the_display_fields_the_summariser_needs(
        self, mongo_client, user_id
    ) -> None:
        await _insert_child(
            user_id,
            "c1",
            content="Agents remember what they read.",
            heading_path=["Memory", "Retrieval"],
        )

        rows = await load_child_embeddings(mongo_client, TEST_DATABASE, user_id)

        assert rows[0] == ChildEmbeddingRow(
            chunk_id="c1",
            embedding=[0.1, 0.2, 0.3, 0.4],
            title="Memory for AI Agents",
            heading_path=["Memory", "Retrieval"],
            content="Agents remember what they read.",
        )

    async def test_another_tenants_children_are_invisible(
        self, mongo_client, user_id
    ) -> None:
        await _insert_child(user_id, "c1")
        await _insert_child(PydanticObjectId(), "other-c1")

        rows = await load_child_embeddings(mongo_client, TEST_DATABASE, user_id)

        assert [row.chunk_id for row in rows] == ["c1"]


class TestWriteClusteringRun:
    @pytest.fixture
    async def previous_run(self, mongo_client, user_id) -> None:
        """A user clustered yesterday into three clusters (run ``r0``)."""

        for cluster_id in (0, 1, 2):
            await _insert_cluster(user_id, cluster_id, run_id="r0")

    async def _write_r1(self, client, user_id, *, run_id: str = "r1") -> Any:
        return await write_clustering_run(
            client,
            TEST_DATABASE,
            user_id,
            run_id=run_id,
            chunk_ids=["c1", "c2", "c3"],
            labels=[0, 1, -1],
            coords=[(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)],
            clusters=[_info(0), _info(1)],
            now=_NOW,
        )

    @pytest.fixture
    async def clustered_chunks(self, mongo_client, user_id) -> None:
        for chunk_id in ("c1", "c2", "c3"):
            await _insert_child(user_id, chunk_id)

    async def test_replaces_the_previous_runs_cluster_rows(
        self, mongo_client, user_id, previous_run, clustered_chunks
    ) -> None:
        await self._write_r1(mongo_client, user_id)

        rows = await _rows(mongo_client, MEMORY_CLUSTERS_COLLECTION, user_id=user_id)

        # Seven clusters becoming five is the case that matters: cluster 2 has
        # no counterpart in r1, and nothing but the delete would remove it.
        assert len(rows) == 2
        assert {row["_id"] for row in rows} == {
            f"{user_id}:cluster:0",
            f"{user_id}:cluster:1",
        }
        assert {row["run_id"] for row in rows} == {"r1"}

    async def test_overwrites_a_surviving_cluster_id_with_the_new_label(
        self, mongo_client, user_id, previous_run, clustered_chunks
    ) -> None:
        await self._write_r1(mongo_client, user_id)

        row = await mongo_client[TEST_DATABASE][MEMORY_CLUSTERS_COLLECTION].find_one(
            {"_id": f"{user_id}:cluster:0"}
        )

        # Deterministic ``_id``s mean the delete never sees cluster 0 — the
        # upsert must be what stops yesterday's label from surviving.
        assert row["label"] == "Agent memory 0"
        assert row["size"] == 2
        assert row["centroid"] == {"x": 0.0, "y": 0.5}
        assert row["keywords"] == ["memory", "agents", "rag"]

    async def test_stamps_every_chunk_with_its_cluster_and_coordinates(
        self, mongo_client, user_id, clustered_chunks
    ) -> None:
        await self._write_r1(mongo_client, user_id)

        rows = {
            row["_id"]: row
            for row in await _rows(mongo_client, MEMORY_COLLECTION, user_id=user_id)
        }

        assert rows["c1"]["cluster_id"] == 0
        assert rows["c1"]["viz"] == {"x": 1.0, "y": 2.0, "run_id": "r1"}
        assert rows["c2"]["cluster_id"] == 1

    async def test_noise_gets_a_label_and_coordinates_but_no_cluster_row(
        self, mongo_client, user_id, clustered_chunks
    ) -> None:
        await self._write_r1(mongo_client, user_id)

        noise = await mongo_client[TEST_DATABASE][MEMORY_COLLECTION].find_one(
            {"_id": "c3"}
        )

        # Noise is drawn (grey) but never summarised: coordinates yes, row no.
        assert noise["cluster_id"] == -1
        assert noise["viz"] == {"x": 5.0, "y": 6.0, "run_id": "r1"}
        rows = await _rows(mongo_client, MEMORY_CLUSTERS_COLLECTION, user_id=user_id)
        assert all(row["cluster_id"] >= 0 for row in rows)

    async def test_created_at_is_timezone_aware(
        self, mongo_client, user_id, clustered_chunks
    ) -> None:
        await self._write_r1(mongo_client, user_id)

        row = await mongo_client[TEST_DATABASE][MEMORY_CLUSTERS_COLLECTION].find_one(
            {"_id": f"{user_id}:cluster:0"}
        )

        assert row["created_at"].tzinfo is not None
        assert row["created_at"] == _NOW

    async def test_re_running_the_same_run_changes_nothing(
        self, mongo_client, user_id, previous_run, clustered_chunks
    ) -> None:
        first = await self._write_r1(mongo_client, user_id)

        second = await self._write_r1(mongo_client, user_id)

        # Idempotent for a given run_id: the write task retries 3 times, so a
        # partial write followed by a retry must converge, not duplicate.
        assert second.clusters_written == first.clusters_written == 2
        assert second.chunks_updated == first.chunks_updated == 3
        assert second.clusters_deleted == 0
        rows = await _rows(mongo_client, MEMORY_CLUSTERS_COLLECTION, user_id=user_id)
        assert len(rows) == 2

    async def test_reports_what_it_wrote_and_deleted(
        self, mongo_client, user_id, previous_run, clustered_chunks
    ) -> None:
        counts = await self._write_r1(mongo_client, user_id)

        assert counts.clusters_written == 2
        assert counts.chunks_updated == 3
        assert counts.clusters_deleted == 3

    async def test_another_tenants_clusters_are_untouched(
        self, mongo_client, user_id, clustered_chunks
    ) -> None:
        other = PydanticObjectId()
        await _insert_cluster(other, 0, run_id="r0")

        await self._write_r1(mongo_client, user_id)

        assert (
            len(await _rows(mongo_client, MEMORY_CLUSTERS_COLLECTION, user_id=other))
            == 1
        )

    async def test_an_all_noise_run_writes_coordinates_and_no_cluster_rows(
        self, mongo_client, user_id, previous_run, clustered_chunks
    ) -> None:
        counts = await write_clustering_run(
            mongo_client,
            TEST_DATABASE,
            user_id,
            run_id="r1",
            chunk_ids=["c1", "c2", "c3"],
            labels=[-1, -1, -1],
            coords=[(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)],
            clusters=[],
            now=_NOW,
        )

        assert counts.clusters_written == 0
        assert counts.chunks_updated == 3
        assert (
            await _rows(mongo_client, MEMORY_CLUSTERS_COLLECTION, user_id=user_id) == []
        )

    async def test_mismatched_input_lengths_are_rejected(
        self, mongo_client, user_id
    ) -> None:
        with pytest.raises(ValueError, match="must describe the same chunks"):
            await write_clustering_run(
                mongo_client,
                TEST_DATABASE,
                user_id,
                run_id="r1",
                chunk_ids=["c1", "c2"],
                labels=[0],
                coords=[(1.0, 2.0)],
                clusters=[],
                now=_NOW,
            )

    async def test_a_naive_timestamp_is_rejected(self, mongo_client, user_id) -> None:
        """CLAUDE.md: no naive datetimes. ``latest_run_id`` sorts on this field."""

        with pytest.raises(ValueError, match="timezone-aware"):
            await write_clustering_run(
                mongo_client,
                TEST_DATABASE,
                user_id,
                run_id="r1",
                chunk_ids=[],
                labels=[],
                coords=[],
                clusters=[],
                now=datetime(2026, 9, 6, 12, 0),  # noqa: DTZ001 — the point
            )


class TestLatestRunId:
    async def test_returns_none_for_a_fresh_user(self, mongo_client, user_id) -> None:
        assert await latest_run_id(mongo_client, TEST_DATABASE, user_id) is None

    async def test_returns_the_newest_runs_id(self, mongo_client, user_id) -> None:
        await _insert_cluster(user_id, 0, run_id="r0")
        await write_clustering_run(
            mongo_client,
            TEST_DATABASE,
            user_id,
            run_id="r1",
            chunk_ids=[],
            labels=[],
            coords=[],
            clusters=[_info(0)],
            now=_NOW,
        )

        assert await latest_run_id(mongo_client, TEST_DATABASE, user_id) == "r1"

    async def test_falls_back_to_a_childs_viz_run_id_on_an_all_noise_run(
        self, mongo_client, user_id
    ) -> None:
        """An all-noise run writes no cluster row, but it DID run.

        Without the fallback the surfaces would report "never clustered" and
        tell the operator to run a phase that just finished.
        """

        await _insert_child(
            user_id, "c1", cluster_id=-1, viz=ChunkViz(x=1.0, y=2.0, run_id="r1")
        )

        assert await latest_run_id(mongo_client, TEST_DATABASE, user_id) == "r1"

    async def test_ignores_another_tenants_runs(self, mongo_client, user_id) -> None:
        await _insert_cluster(PydanticObjectId(), 0, run_id="r1")

        assert await latest_run_id(mongo_client, TEST_DATABASE, user_id) is None


class TestLoadEmbeddingMap:
    @pytest.fixture
    async def mixed_corpus(self, mongo_client, user_id) -> None:
        """5 embedded children: 3 drawn by ``r1``, 1 stale, 1 never clustered."""

        for index in (1, 2, 3):
            await _insert_child(
                user_id,
                f"c{index}",
                content="x" * 400,
                cluster_id=0 if index < 3 else 1,
                viz=ChunkViz(x=float(index), y=float(index), run_id="r1"),
            )
        await _insert_child(
            user_id, "c4", cluster_id=0, viz=ChunkViz(x=9.0, y=9.0, run_id="r0")
        )
        await _insert_child(user_id, "c5")
        await _insert_cluster(user_id, 0, run_id="r1", size=2, label="Agent memory")
        await _insert_cluster(user_id, 1, run_id="r1", size=1, label="Retrieval")

    async def test_returns_none_when_the_user_was_never_clustered(
        self, mongo_client, user_id
    ) -> None:
        await _insert_child(user_id, "c1")

        # A caller shows an explanatory message, never an empty picture.
        assert await load_embedding_map(mongo_client, TEST_DATABASE, user_id) is None

    async def test_draws_only_this_runs_points(
        self, mongo_client, user_id, mixed_corpus
    ) -> None:
        embedding_map = await load_embedding_map(mongo_client, TEST_DATABASE, user_id)

        assert embedding_map.run_id == "r1"
        assert {point.chunk_id for point in embedding_map.points} == {"c1", "c2", "c3"}

    async def test_counts_stale_and_unassigned_chunks_as_unclustered(
        self, mongo_client, user_id, mixed_corpus
    ) -> None:
        embedding_map = await load_embedding_map(mongo_client, TEST_DATABASE, user_id)

        # The warning contract (ADR-007 §8): "2 of 5 chunks have no cluster
        # assignment (or a stale one)". Stale coordinates come from an older
        # UMAP fit — a space nothing else on the map shares.
        assert embedding_map.total_children == 5
        assert len(embedding_map.points) == 3
        assert embedding_map.unclustered == 2

    async def test_clusters_are_sorted_by_size_descending(
        self, mongo_client, user_id, mixed_corpus
    ) -> None:
        embedding_map = await load_embedding_map(mongo_client, TEST_DATABASE, user_id)

        # The legend reads biggest-topic-first, and the palette is assigned by
        # cluster RANK, so the order is part of the picture.
        assert [cluster.size for cluster in embedding_map.clusters] == [2, 1]
        assert [cluster.label for cluster in embedding_map.clusters] == [
            "Agent memory",
            "Retrieval",
        ]

    async def test_a_point_carries_its_tooltip_fields(
        self, mongo_client, user_id, mixed_corpus
    ) -> None:
        embedding_map = await load_embedding_map(mongo_client, TEST_DATABASE, user_id)

        point = next(p for p in embedding_map.points if p.chunk_id == "c1")
        assert (point.x, point.y) == (1.0, 1.0)
        assert point.cluster_id == 0
        assert point.title == "Memory for AI Agents"
        assert point.heading_path == ["Retrieval"]
        assert len(point.snippet) == 160

    async def test_flattens_the_stored_centroid(
        self, mongo_client, user_id, mixed_corpus
    ) -> None:
        embedding_map = await load_embedding_map(mongo_client, TEST_DATABASE, user_id)

        assert embedding_map.clusters[0].centroid_x == 0.0
        assert embedding_map.clusters[0].centroid_y == 0.0

    async def test_an_all_noise_run_is_a_map_with_no_legend(
        self, mongo_client, user_id
    ) -> None:
        await _insert_child(
            user_id, "c1", cluster_id=-1, viz=ChunkViz(x=1.0, y=2.0, run_id="r1")
        )

        embedding_map = await load_embedding_map(mongo_client, TEST_DATABASE, user_id)

        assert embedding_map.clusters == []
        assert [point.cluster_id for point in embedding_map.points] == [-1]
        assert embedding_map.unclustered == 0
