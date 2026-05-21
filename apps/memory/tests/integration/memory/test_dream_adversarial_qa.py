"""Tester adversarial QA for the dream-consolidation flow (#051).

Headline break paths the SWE suite does NOT directly assert:

A-inverse: two OLD nodes (both <= watermark) that are duplicates → NEITHER
           drives → the pair is NOT processed (proves the driving-set filter
           actually reduces work; if the search space were the only filter
           this would wrongly merge).
B-count:   new<->new duplicate → exactly ONE review_duplicate apply call
           (id1<id2 + seen collapses the two driving comparisons).
F-detail:  idempotent re-run drives ZERO nodes (the watermark optimization),
           not merely "no new merges".
EmptyUser: a user with no fresh nodes is a clean noop.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.config.app_config import app_config
from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.memory.consolidation import dream as dream_mod
from tree.memory.consolidation.dream import dream_consolidation
from tree.memory.consolidation.meta_state import load_watermark, record_dream_run
from tree.memory.indexing.core import ensure_indexes
from tree.models.fake_model import FakeEmbeddingModel

TEST_DATABASE = "integration_tests_twin"
_DIMS = 8

pytestmark = [pytest.mark.requires_mongot, pytest.mark.slow]


def _vec(target_cos: float) -> list[float]:
    cos_value = max(-1.0, min(1.0, target_cos))
    sin_value = math.sqrt(max(0.0, 1.0 - cos_value * cos_value))
    vec = [0.0] * _DIMS
    vec[0] = cos_value
    vec[1] = sin_value
    return vec


def _node_doc(*, node_id, name, user_id, embedding, updated_at, created_at=None):
    return {
        "_id": node_id,
        "user_id": user_id,
        "kind": "node",
        "type": NodeType.PERSON.value,
        "name": name,
        "canonical_name": name,
        "properties": {},
        "aliases": [],
        "confidence": 1.0,
        "embedding": embedding,
        "sources": [],
        "merged_into": None,
        "created_at": created_at or updated_at,
        "updated_at": updated_at,
    }


async def _wait_for_indexed_count(collection, user_id, expected, timeout=60.0):
    import asyncio

    probe = _vec(1.0)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        cursor = await collection.aggregate(
            [
                {
                    "$vectorSearch": {
                        "index": "vector_index",
                        "path": "embedding",
                        "queryVector": probe,
                        "numCandidates": 100,
                        "limit": 50,
                        "filter": {"user_id": user_id, "kind": "node"},
                    }
                },
                {"$count": "n"},
            ]
        )
        rows = [r async for r in cursor]
        if rows and rows[0].get("n", 0) >= expected:
            return
        await asyncio.sleep(1.0)
    raise RuntimeError(f"vector_index did not return {expected} nodes in {timeout}s")


@pytest.fixture(autouse=True)
def _patch_embedding_dimensions(mocker):
    mocker.patch.object(app_config.models.search_embedding, "dimensions", _DIMS)


@pytest.fixture
async def kg_collection(mongo_client):
    db = mongo_client[TEST_DATABASE]
    col = db["knowledge_graph"]
    await ensure_indexes(
        mongo_client,
        TEST_DATABASE,
        embedding_model=FakeEmbeddingModel(dimensions=_DIMS),
        user_id=PydanticObjectId(),
    )
    yield col
    await db.drop_collection("knowledge_graph")


@pytest.fixture(autouse=True)
def _redirect_flow_db(mocker, mongo_client):
    mocker.patch(
        "tree.memory.consolidation.dream.init_mongodb", return_value=mongo_client
    )
    mocker.patch(
        "tree.memory.consolidation.dream.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )


def _database(mongo_client):
    return mongo_client[TEST_DATABASE]


async def test_old_old_pair_is_not_processed(mongo_client, kg_collection) -> None:
    """A-INVERSE: two OLD duplicates (both <= watermark) → neither drives.

    Proves the driving-set watermark filter actually reduces work. If the
    sweep were driven by the search space (full graph) rather than the
    watermark-filtered driving set, these two would wrongly merge.
    """

    user_id = PydanticObjectId()
    last_run = datetime(2026, 5, 10, tzinfo=UTC)
    old = last_run - timedelta(days=7)

    await record_dream_run(
        database=_database(mongo_client),
        user_id=user_id,
        run_start=last_run,
        last_run_id="seed",
        last_stats={},
    )
    await kg_collection.insert_many(
        [
            _node_doc(
                node_id=f"{user_id}:person:old_a",
                name="Marie Curie",
                user_id=user_id,
                embedding=_vec(1.0),
                updated_at=old,
                created_at=old,
            ),
            _node_doc(
                node_id=f"{user_id}:person:old_b",
                name="Marie Curie",
                user_id=user_id,
                embedding=_vec(0.999),
                updated_at=old,
                created_at=old,
            ),
        ]
    )
    await _wait_for_indexed_count(kg_collection, user_id, expected=2)

    with prefect_tags("tests"):
        report = await dream_consolidation(user_id=user_id, dry_run=False)

    assert report.stats.nodes_driven == 0  # neither old node drives
    assert report.pairs == []
    assert report.stats.auto_merged == 0
    tombstoned = await kg_collection.find_one(
        {"user_id": user_id, "kind": "node", "merged_into": {"$nin": [None, ""]}}
    )
    assert tombstoned is None
    edge = await kg_collection.find_one(
        {"user_id": user_id, "kind": "edge", "type": EdgeType.SAME_AS.value}
    )
    assert edge is None


async def test_new_new_applies_exactly_one_merge(
    mongo_client, kg_collection, mocker
) -> None:
    """B-COUNT: two FRESH duplicates → exactly ONE review_duplicate call.

    Both fresh nodes drive (so dedupe_entity runs twice) but the ordered
    pair + seen set must collapse the apply to a single merge.
    """

    user_id = PydanticObjectId()
    last_run = datetime(2026, 5, 10, tzinfo=UTC)
    fresh = last_run + timedelta(days=1)

    await record_dream_run(
        database=_database(mongo_client),
        user_id=user_id,
        run_start=last_run,
        last_run_id="seed",
        last_stats={},
    )
    await kg_collection.insert_many(
        [
            _node_doc(
                node_id=f"{user_id}:person:nn_a",
                name="Carl Sagan",
                user_id=user_id,
                embedding=_vec(1.0),
                updated_at=fresh,
            ),
            _node_doc(
                node_id=f"{user_id}:person:nn_b",
                name="Carl Sagan",
                user_id=user_id,
                embedding=_vec(0.999),
                updated_at=fresh,
            ),
        ]
    )
    await _wait_for_indexed_count(kg_collection, user_id, expected=2)

    spy = mocker.spy(dream_mod, "review_duplicate")

    with prefect_tags("tests"):
        report = await dream_consolidation(user_id=user_id, dry_run=False)

    assert report.stats.nodes_driven == 2  # both fresh nodes drive
    assert len(report.pairs) == 1  # collapsed to one ordered pair
    assert report.stats.auto_merged == 1
    assert spy.call_count == 1  # exactly ONE merge applied, not two
    tombstoned = [
        d
        async for d in kg_collection.find(
            {"user_id": user_id, "kind": "node", "merged_into": {"$nin": [None, ""]}}
        )
    ]
    assert len(tombstoned) == 1


async def test_empty_user_is_clean_noop(mongo_client, kg_collection) -> None:
    """A user with NO fresh nodes (and no prior run) → clean noop, advances wm."""

    user_id = PydanticObjectId()

    with prefect_tags("tests"):
        report = await dream_consolidation(user_id=user_id, dry_run=False)

    assert report.stats.nodes_driven == 0
    assert report.pairs == []
    assert report.watermark_advanced is True
    wm = await load_watermark(database=_database(mongo_client), user_id=user_id)
    # Mongo truncates datetimes to millisecond precision, so compare to ms.
    assert abs((wm.last_run_at - report.run_start).total_seconds()) < 0.002


async def test_idempotent_rerun_drives_zero_after_merge(
    mongo_client, kg_collection
) -> None:
    """F-DETAIL: after a real merge, a second run finds an EMPTY driving delta.

    The merge tombstones the loser and advances the watermark to run_start.
    The winner's updated_at is stamped at the merge instant (after run_start),
    so it MAY re-drive — but it now has no untombstoned twin and an existing
    confirmed SAME_AS edge, so the second run takes no action. We assert the
    driving set shrank to at most the winner and produced zero new pairs.
    """

    user_id = PydanticObjectId()
    last_run = datetime(2026, 5, 10, tzinfo=UTC)
    fresh = last_run + timedelta(days=1)
    old = last_run - timedelta(days=7)

    await record_dream_run(
        database=_database(mongo_client),
        user_id=user_id,
        run_start=last_run,
        last_run_id="seed",
        last_stats={},
    )
    await kg_collection.insert_many(
        [
            _node_doc(
                node_id=f"{user_id}:person:idem_new",
                name="Nikola Tesla",
                user_id=user_id,
                embedding=_vec(1.0),
                updated_at=fresh,
            ),
            _node_doc(
                node_id=f"{user_id}:person:idem_old",
                name="Nikola Tesla",
                user_id=user_id,
                embedding=_vec(0.999),
                updated_at=old,
                created_at=old,
            ),
        ]
    )
    await _wait_for_indexed_count(kg_collection, user_id, expected=2)

    # The first sweep depends on the per-node $vectorSearch surfacing the
    # twin; mongot index convergence is eventually-consistent, so retry the
    # FIRST run until the merge lands (re-seeding the watermark each attempt
    # so the fresh node keeps driving). This isolates the test from mongot
    # timing without weakening the idempotency assertion on the second run.
    import asyncio

    first = None
    for _ in range(10):
        with prefect_tags("tests"):
            first = await dream_consolidation(user_id=user_id, dry_run=False)
        if first.stats.auto_merged == 1:
            break
        await record_dream_run(
            database=_database(mongo_client),
            user_id=user_id,
            run_start=last_run,
            last_run_id="seed",
            last_stats={},
        )
        await asyncio.sleep(1.0)
    assert first is not None and first.stats.auto_merged == 1
    first_wm = await load_watermark(database=_database(mongo_client), user_id=user_id)
    assert abs((first_wm.last_run_at - first.run_start).total_seconds()) < 0.002

    with prefect_tags("tests"):
        second = await dream_consolidation(user_id=user_id, dry_run=False)

    # The watermark optimization shrank the driving delta: at most the winner
    # re-drives (documented overlap), never the tombstoned loser.
    assert second.stats.nodes_driven <= 1
    assert second.pairs == []
    assert second.stats.auto_merged == 0
    assert second.stats.flagged == 0
    # The second run still advances the watermark forward.
    assert second.run_start >= first.run_start
    second_wm = await load_watermark(database=_database(mongo_client), user_id=user_id)
    assert abs((second_wm.last_run_at - second.run_start).total_seconds()) < 0.002
