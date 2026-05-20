"""Integration tests for the dream-consolidation flow (#051).

These tests drive ``dream_consolidation(...)`` end-to-end against a live
Atlas-local ``$vectorSearch`` (so they are ``requires_mongot`` + ``slow``).
The ``knowledge_graph`` collection is seeded with hand-crafted 8-dimensional
embeddings whose pairwise cosine similarities are known by construction
(mirroring ``test_dedup.py``), so the tiered decisions are deterministic.

The flow's DB plumbing is redirected to the test database by patching
``init_mongodb`` + ``settings.mongo.mongo_initdb_database`` in the dream
module (the same seam ``test_extraction_pipeline.py`` uses). The sweep reads
the nodes' STORED vectors, so no embedding model is needed — this directly
exercises the embedding-READ-only invariant.

Coverage:

* the two-set rule (driving set watermark-filtered; search space full graph),
* dry_run reports without mutating,
* auto-merge tier tombstones the loser + stamps the audit edge,
* flag tier upserts a pending SAME_AS edge,
* idempotency (a second real run is a near-noop),
* tenant isolation (a second user's twin is never touched).
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.config.app_config import app_config
from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.memory.consolidation.dream import dream_consolidation
from tree.memory.consolidation.meta_state import load_watermark, record_dream_run
from tree.memory.indexing.core import ensure_indexes
from tree.models.fake_model import FakeEmbeddingModel

TEST_DATABASE = "integration_tests_twin"
_DIMS = 8

pytestmark = [pytest.mark.requires_mongot, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Vector + seeding helpers (raw cosine, mirrors test_dedup.py)
# ---------------------------------------------------------------------------


def _vector_with_raw_cosine(target_cos: float) -> list[float]:
    """8-dim unit vector whose cosine with ``(1, 0, ...)`` equals ``target_cos``."""

    cos_value = max(-1.0, min(1.0, target_cos))
    sin_value = math.sqrt(max(0.0, 1.0 - cos_value * cos_value))
    vec = [0.0] * _DIMS
    vec[0] = cos_value
    vec[1] = sin_value
    return vec


def _node_doc(
    *,
    node_id: str,
    name: str,
    user_id: PydanticObjectId,
    embedding: list[float],
    updated_at: datetime,
    node_type: NodeType = NodeType.PERSON,
    created_at: datetime | None = None,
    merged_into: str | None = None,
) -> dict:
    return {
        "_id": node_id,
        "user_id": user_id,
        "kind": "node",
        "type": node_type.value,
        "name": name,
        "canonical_name": name,
        "properties": {},
        "aliases": [],
        "confidence": 1.0,
        "embedding": embedding,
        "sources": [],
        "merged_into": merged_into,
        "created_at": created_at or updated_at,
        "updated_at": updated_at,
    }


async def _wait_for_indexed_count(
    collection, user_id: PydanticObjectId, expected: int, timeout: float = 30.0
) -> None:
    """Poll ``$vectorSearch`` until ``expected`` of ``user_id``'s nodes index."""

    probe = _vector_with_raw_cosine(1.0)
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
    raise RuntimeError(
        f"vector_index did not return {expected} nodes within {timeout}s"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_embedding_dimensions(mocker):
    """Force the vector index to 8 dimensions for these tests."""

    mocker.patch.object(app_config.models.search_embedding, "dimensions", _DIMS)


@pytest.fixture
async def kg_collection(mongo_client):
    """Hand back the ``knowledge_graph`` collection with a ready vector index."""

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
    """Point the flow's ``init_mongodb`` + db name at the test database."""

    mocker.patch(
        "tree.memory.consolidation.dream.init_mongodb",
        return_value=mongo_client,
    )
    mocker.patch(
        "tree.memory.consolidation.dream.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )


def _database(mongo_client):
    return mongo_client[TEST_DATABASE]


# ---------------------------------------------------------------------------
# Two-set rule + auto-merge
# ---------------------------------------------------------------------------


async def test_new_node_finds_older_twin_and_auto_merges(
    mongo_client, kg_collection
) -> None:
    """The watermark-fresh node drives; its OLD twin (outside the delta) is
    in the search space and the high-similarity pair auto-merges."""

    user_id = PydanticObjectId()
    last_run = datetime(2026, 5, 10, tzinfo=UTC)
    fresh = last_run + timedelta(days=1)
    old = last_run - timedelta(days=7)

    # Seed a prior watermark so the OLD twin is OUTSIDE the driving set.
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
                node_id=f"{user_id}:person:paul_new",
                name="Paul Iusztin",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(1.0),
                updated_at=fresh,
            ),
            _node_doc(
                node_id=f"{user_id}:person:paul_old",
                name="Paul  Iusztin",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(0.999),
                updated_at=old,
                created_at=old,
            ),
        ]
    )
    await _wait_for_indexed_count(kg_collection, user_id, expected=2)

    with prefect_tags("tests"):
        report = await dream_consolidation(user_id=user_id, dry_run=False)

    assert report.stats.nodes_driven == 1  # only the fresh node drives
    assert len(report.pairs) == 1
    assert report.pairs[0].action == "merged"
    assert report.stats.auto_merged == 1

    # One node ends up tombstoned (merged_into set).
    tombstoned = [
        d
        async for d in kg_collection.find(
            {"user_id": user_id, "kind": "node", "merged_into": {"$nin": [None, ""]}}
        )
    ]
    assert len(tombstoned) == 1

    # A confirmed SAME_AS audit edge stamped reviewed_by="dream".
    audit = await kg_collection.find_one(
        {"user_id": user_id, "kind": "edge", "type": EdgeType.SAME_AS.value}
    )
    assert audit is not None
    assert audit["properties"]["status"] == "confirmed"
    assert audit["properties"]["reviewed_by"] == "dream"

    # Watermark advanced to run_start.
    wm = await load_watermark(database=_database(mongo_client), user_id=user_id)
    assert wm.last_run_at >= last_run
    assert report.watermark_advanced is True


async def test_flag_tier_upserts_pending_same_as(mongo_client, kg_collection) -> None:
    """A medium-confidence pair is flagged (pending SAME_AS), not merged."""

    user_id = PydanticObjectId()
    now = datetime.now(UTC)

    # No prior watermark ⇒ epoch ⇒ both nodes are in the delta. To keep a
    # single driving comparison, make the twin OLD relative to a seeded
    # watermark; the twin scores in the flag band on names that don't fuzzy.
    last_run = now - timedelta(days=2)
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
                node_id=f"{user_id}:person:zoltar",
                name="Zoltar",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(1.0),
                updated_at=now,
            ),
            _node_doc(
                node_id=f"{user_id}:person:xerxes",
                name="Xerxes",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(0.88),
                updated_at=last_run - timedelta(days=5),
                created_at=last_run - timedelta(days=5),
            ),
        ]
    )
    await _wait_for_indexed_count(kg_collection, user_id, expected=2)

    with prefect_tags("tests"):
        report = await dream_consolidation(user_id=user_id, dry_run=False)

    assert len(report.pairs) == 1
    assert report.pairs[0].action == "flagged"
    assert report.stats.flagged == 1

    # A pending SAME_AS edge exists; no node tombstoned.
    pending = await kg_collection.find_one(
        {
            "user_id": user_id,
            "kind": "edge",
            "type": EdgeType.SAME_AS.value,
            "properties.status": "pending",
        }
    )
    assert pending is not None
    tombstoned = await kg_collection.find_one(
        {"user_id": user_id, "kind": "node", "merged_into": {"$nin": [None, ""]}}
    )
    assert tombstoned is None


# ---------------------------------------------------------------------------
# dry_run — report without mutating
# ---------------------------------------------------------------------------


async def test_dry_run_reports_without_mutating(mongo_client, kg_collection) -> None:
    """dry_run=True reports the would-be merge but writes nothing and leaves
    the watermark untouched."""

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
                node_id=f"{user_id}:person:a_new",
                name="Ada Lovelace",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(1.0),
                updated_at=fresh,
            ),
            _node_doc(
                node_id=f"{user_id}:person:a_old",
                name="Ada Lovelace",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(0.999),
                updated_at=old,
                created_at=old,
            ),
        ]
    )
    await _wait_for_indexed_count(kg_collection, user_id, expected=2)

    with prefect_tags("tests"):
        report = await dream_consolidation(user_id=user_id, dry_run=True)

    # Reports the would-be decision...
    assert len(report.pairs) == 1
    assert report.pairs[0].action == "merged"
    assert report.watermark_advanced is False

    # ...but nothing is written: no tombstones, no SAME_AS edges.
    tombstoned = await kg_collection.find_one(
        {"user_id": user_id, "kind": "node", "merged_into": {"$nin": [None, ""]}}
    )
    assert tombstoned is None
    edge = await kg_collection.find_one(
        {"user_id": user_id, "kind": "edge", "type": EdgeType.SAME_AS.value}
    )
    assert edge is None

    # Watermark unchanged — the next real run still sees the same delta.
    wm = await load_watermark(database=_database(mongo_client), user_id=user_id)
    assert wm.last_run_at == last_run


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_second_run_is_near_noop(mongo_client, kg_collection) -> None:
    """A second real run on the same data takes NO new action.

    The merge stamps the winner's ``updated_at`` to the merge instant, which
    is after the freshly-advanced watermark, so the winner may re-drive on the
    next run (the documented slight idempotent overlap). But the loser is
    tombstoned (excluded from the search space) and the SAME_AS edge is now
    ``confirmed``, so the second run finds nothing to act on: zero new merges,
    zero new pairs."""

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
                node_id=f"{user_id}:person:dup_new",
                name="Grace Hopper",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(1.0),
                updated_at=fresh,
            ),
            _node_doc(
                node_id=f"{user_id}:person:dup_old",
                name="Grace Hopper",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(0.999),
                updated_at=old,
                created_at=old,
            ),
        ]
    )
    await _wait_for_indexed_count(kg_collection, user_id, expected=2)

    with prefect_tags("tests"):
        first = await dream_consolidation(user_id=user_id, dry_run=False)
    assert first.stats.auto_merged == 1

    # Second run: the loser is tombstoned and the SAME_AS edge is confirmed,
    # so even if the winner re-drives there is nothing to act on.
    with prefect_tags("tests"):
        second = await dream_consolidation(user_id=user_id, dry_run=False)

    assert second.pairs == []
    assert second.stats.auto_merged == 0
    assert second.stats.flagged == 0

    # Still exactly one tombstone — the second run did not double-merge.
    tombstoned = [
        d
        async for d in kg_collection.find(
            {"user_id": user_id, "kind": "node", "merged_into": {"$nin": [None, ""]}}
        )
    ]
    assert len(tombstoned) == 1


async def test_existing_rejected_pair_is_not_reflagged(
    mongo_client, kg_collection
) -> None:
    """A pair with a prior rejected SAME_AS edge is never re-acted on.

    A ``rejected`` SAME_AS edge is honored at TWO layers: ``dedupe_entity``'s
    reject-pair filter drops the twin from the driving node's candidate set
    (so it never even surfaces as a pair), and — were it to surface — the
    sweep's skip-existing-SAME_AS guard would catch it. Either way the human
    "not a duplicate" decision stands: no new pair, no tombstone, the edge
    stays ``rejected``."""

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

    id_new = f"{user_id}:person:rej_new"
    id_old = f"{user_id}:person:rej_old"
    await kg_collection.insert_many(
        [
            _node_doc(
                node_id=id_new,
                name="Linus",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(1.0),
                updated_at=fresh,
            ),
            _node_doc(
                node_id=id_old,
                name="Linus",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(0.999),
                updated_at=old,
                created_at=old,
            ),
            {
                "_id": f"{id_old}|same_as|{id_new}",
                "user_id": user_id,
                "kind": "edge",
                "type": EdgeType.SAME_AS.value,
                "source_node_id": id_old,
                "target_node_id": id_new,
                "source_type": NodeType.PERSON.value,
                "target_type": NodeType.PERSON.value,
                "properties": {"status": "rejected"},
                "sources": [],
                "created_at": old,
                "updated_at": old,
            },
        ]
    )
    await _wait_for_indexed_count(kg_collection, user_id, expected=2)

    with prefect_tags("tests"):
        report = await dream_consolidation(user_id=user_id, dry_run=False)

    # No actionable pair emitted (rejected pair honored), no merge, no flag.
    assert report.pairs == []
    assert report.stats.auto_merged == 0
    assert report.stats.flagged == 0
    # No node tombstoned; the rejected edge stays rejected.
    tombstoned = await kg_collection.find_one(
        {"user_id": user_id, "kind": "node", "merged_into": {"$nin": [None, ""]}}
    )
    assert tombstoned is None
    edge = await kg_collection.find_one({"_id": f"{id_old}|same_as|{id_new}"})
    assert edge["properties"]["status"] == "rejected"


async def test_existing_pending_pair_is_skipped(mongo_client, kg_collection) -> None:
    """A pair already carrying a PENDING SAME_AS edge is skipped by the sweep's
    skip-existing-SAME_AS guard.

    Unlike ``rejected``, a ``pending`` edge does NOT filter the candidate out
    of ``dedupe_entity`` (only ``rejected`` does), so the twin DOES surface —
    proving the sweep's own ``_same_as_edge_exists`` skip is what suppresses
    it. The pending edge must stay pending (not re-merged, not duplicated)."""

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

    id_new = f"{user_id}:person:pend_new"
    id_old = f"{user_id}:person:pend_old"
    # The pending edge is keyed on the ORDERED pair (id1|same_as|id2), the
    # same shape _upsert_pending_same_as_edge writes.
    id1, id2 = (id_new, id_old) if id_new < id_old else (id_old, id_new)
    await kg_collection.insert_many(
        [
            _node_doc(
                node_id=id_new,
                name="Alan Turing",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(1.0),
                updated_at=fresh,
            ),
            _node_doc(
                node_id=id_old,
                name="Alan Turing",
                user_id=user_id,
                embedding=_vector_with_raw_cosine(0.999),
                updated_at=old,
                created_at=old,
            ),
            {
                "_id": f"{id1}|same_as|{id2}",
                "user_id": user_id,
                "kind": "edge",
                "type": EdgeType.SAME_AS.value,
                "source_node_id": id1,
                "target_node_id": id2,
                "source_type": NodeType.PERSON.value,
                "target_type": NodeType.PERSON.value,
                "properties": {"status": "pending"},
                "sources": [],
                "created_at": old,
                "updated_at": old,
            },
        ]
    )
    await _wait_for_indexed_count(kg_collection, user_id, expected=2)

    with prefect_tags("tests"):
        report = await dream_consolidation(user_id=user_id, dry_run=False)

    # The twin surfaced but the existing pending edge made the sweep skip it.
    assert report.pairs == []
    assert report.stats.skipped_existing_same_as == 1
    assert report.stats.auto_merged == 0
    # No tombstone; the edge stays pending.
    tombstoned = await kg_collection.find_one(
        {"user_id": user_id, "kind": "node", "merged_into": {"$nin": [None, ""]}}
    )
    assert tombstoned is None
    edge = await kg_collection.find_one({"_id": f"{id1}|same_as|{id2}"})
    assert edge["properties"]["status"] == "pending"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_tenant_isolation(mongo_client, kg_collection) -> None:
    """User A's sweep never touches User B's near-duplicate twin."""

    user_a = PydanticObjectId()
    user_b = PydanticObjectId()
    last_run = datetime(2026, 5, 10, tzinfo=UTC)
    fresh = last_run + timedelta(days=1)
    old = last_run - timedelta(days=7)

    for uid in (user_a, user_b):
        await record_dream_run(
            database=_database(mongo_client),
            user_id=uid,
            run_start=last_run,
            last_run_id="seed",
            last_stats={},
        )

    await kg_collection.insert_many(
        [
            _node_doc(
                node_id=f"{user_a}:person:a_new",
                name="Same Name",
                user_id=user_a,
                embedding=_vector_with_raw_cosine(1.0),
                updated_at=fresh,
            ),
            _node_doc(
                node_id=f"{user_a}:person:a_old",
                name="Same Name",
                user_id=user_a,
                embedding=_vector_with_raw_cosine(0.999),
                updated_at=old,
                created_at=old,
            ),
            # User B: an identical near-duplicate pair that MUST stay intact.
            _node_doc(
                node_id=f"{user_b}:person:b_new",
                name="Same Name",
                user_id=user_b,
                embedding=_vector_with_raw_cosine(1.0),
                updated_at=fresh,
            ),
            _node_doc(
                node_id=f"{user_b}:person:b_old",
                name="Same Name",
                user_id=user_b,
                embedding=_vector_with_raw_cosine(0.999),
                updated_at=old,
                created_at=old,
            ),
        ]
    )
    await _wait_for_indexed_count(kg_collection, user_a, expected=2)
    await _wait_for_indexed_count(kg_collection, user_b, expected=2)

    with prefect_tags("tests"):
        report = await dream_consolidation(user_id=user_a, dry_run=False)

    assert report.stats.auto_merged == 1

    # User B's nodes are untouched: neither tombstoned.
    b_tombstoned = await kg_collection.find_one(
        {"user_id": user_b, "kind": "node", "merged_into": {"$nin": [None, ""]}}
    )
    assert b_tombstoned is None
    # User B's watermark is unchanged (still the seed).
    wm_b = await load_watermark(database=_database(mongo_client), user_id=user_b)
    assert wm_b.last_run_at == last_run
