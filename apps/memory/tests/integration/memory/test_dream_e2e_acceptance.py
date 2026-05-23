"""Headline end-to-end acceptance for the dream-consolidation feature (#053).

This is the *feature-level* regression that ties the whole chain delivered by
#048-#052 together. Where ``test_dream_consolidation.py`` (#051) and
``test_dream_supersession_and_fanout.py`` (#052) each prove an individual
mechanism with synthetic users, this module proves the operator narrative end
to end on the **Paul Iusztin** user (per ``CLAUDE.md``: "use the Paul Iusztin
user when testing"):

THE HEADLINE SCENARIO — collapse-then-noop watermark proof
----------------------------------------------------------
1. Seed the Paul Iusztin user (a real ``User`` row, whose ``after_insert`` hook
   materializes the active ``person:self`` node the fan-out enumerates) with
   TWO near-duplicate nodes of the same type — the parallel-ingest scenario
   where two writers each created a node and inline write-time dedup missed the
   twin.
2. Run ``dream_consolidation(user_id=<paul>, dry_run=False)`` → the pair
   collapses (auto-merge: loser tombstoned + SAME_AS ``confirmed`` /
   ``reviewed_by="dream"``, OR a pending flag for the flag-band variant) AND the
   watermark advances to ``run_start``.
3. Run ``dream_consolidation`` AGAIN immediately on unchanged data → a near-noop:
   the driving delta is empty, zero NEW merges/flags, no already-decided pair is
   re-touched. **This is the core proof the incremental watermark works.**

Plus the adversarial coverage the groomed spec lists: incremental catch-up of a
node ingested AFTER the first dream, a dry-run rehearsal that writes nothing,
respect for a human's earlier rejection, the ``max_pairs`` cap, the zero-Voyage
(embedding-READ-only) and zero-LLM (default path) cost invariants, the #048
search-model routing, and the fan-out parent flow over the genuine active Paul
user.

Embeddings are deterministic 8-dim cosine vectors (mirroring ``test_dedup.py``
and the #051 suite): the dream sweep is embedding-READ-only over stored vectors,
so a fake embedder keeps the scores stable AND makes ZERO Voyage calls — exactly
the invariant we assert. No live-Voyage smoke is used, so the live-RPM ``[HUMAN]``
AC is ``NOT RUN — used deterministic embedder``.

The flow's DB plumbing is redirected to the test database by patching
``init_mongodb`` + ``settings.mongo.mongo_initdb_database`` in the dream module
(the seam the #051/#052 suites use). The vector index is forced to 8 dimensions
so the hand-crafted vectors index against the live mongot ``vector_index`` —
hence ``requires_mongot`` + ``slow``.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.config.app_config import app_config, load_app_config
from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.entities.users import User
from tree.memory.consolidation.dream import (
    dream_consolidation,
    dream_consolidation_all_users,
)
from tree.memory.consolidation.meta_state import load_watermark, record_dream_run
from tree.memory.indexing.core import ensure_indexes
from tree.models.fake_model import FakeEmbeddingModel
from tree.models.get_model import get_search_embedding_model
from tree.models.voyage_embedding import VoyageTextEmbeddingModel

TEST_DATABASE = "integration_tests_twin"
_DIMS = 8

# Per CLAUDE.md, the canonical test tenant is the Paul Iusztin user.
PAUL_IDENTIFIER = "p.b.iusztin@gmail.com"

pytestmark = [pytest.mark.requires_mongot, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Vector + seeding helpers (raw cosine against (1, 0, ...); mirrors test_dedup)
# ---------------------------------------------------------------------------


def _vec(target_cos: float) -> list[float]:
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
        "merged_into": None,
        "created_at": created_at or updated_at,
        "updated_at": updated_at,
    }


async def _wait_for_indexed_count(
    collection, user_id: PydanticObjectId, expected: int, timeout: float = 60.0
) -> None:
    """Poll ``$vectorSearch`` until ``expected`` of ``user_id``'s nodes index."""

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
    raise RuntimeError(
        f"vector_index did not return {expected} nodes within {timeout}s"
    )


async def _seed_parallel_duplicate(
    *,
    collection,
    user_id: PydanticObjectId,
    base_id: str,
    name_a: str,
    name_b: str,
    cos: float,
    fresh: datetime,
    old: datetime,
) -> tuple[str, str]:
    """Seed the parallel-ingest scenario: two near-identical nodes of one type.

    Simulates two writers each creating a node for the same real-world entity
    that inline write-time dedup missed (the twin was committed concurrently).
    Node ``a`` is watermark-fresh (``updated_at = fresh``) so it DRIVES the
    sweep; node ``b`` is OLD (``updated_at = old``) so it sits in the search
    space only — exactly the new->old collapse the dream is for. Their stored
    embeddings score ``cos`` apart, deterministically placing the pair in the
    auto-merge or flag band.
    """

    id_a = f"{user_id}:person:{base_id}_a"
    id_b = f"{user_id}:person:{base_id}_b"
    await collection.insert_many(
        [
            _node_doc(
                node_id=id_a,
                name=name_a,
                user_id=user_id,
                embedding=_vec(1.0),
                updated_at=fresh,
            ),
            _node_doc(
                node_id=id_b,
                name=name_b,
                user_id=user_id,
                embedding=_vec(cos),
                updated_at=old,
                created_at=old,
            ),
        ]
    )
    await _wait_for_indexed_count(collection, user_id, expected=2)
    return id_a, id_b


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_embedding_dimensions(mocker):
    """Force the vector index to 8 dimensions so hand-crafted vectors index."""

    mocker.patch.object(app_config.models.search_embedding, "dimensions", _DIMS)


@pytest.fixture
async def kg_collection(mongo_client):
    """The ``knowledge_graph`` collection with a ready 8-dim vector index."""

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


@pytest.fixture
def dream_cfg(mocker):
    """Override the dream config block the flow reads via ``_live_app_config``.

    The flow calls ``_live_app_config()`` (a fresh ``load_app_config()``) for
    its gates, so monkeypatching the module-level singleton has no effect. This
    installer patches ``_live_app_config`` to return a config whose ``dream``
    block carries the given overrides — the precise seam the flow consults.
    """

    def _install(**overrides):
        cfg = load_app_config()
        cfg.dream = cfg.dream.model_copy(update=overrides)
        mocker.patch(
            "tree.memory.consolidation.dream._live_app_config",
            return_value=cfg,
        )
        return cfg

    return _install


@pytest.fixture
async def paul(mongo_client) -> User:
    """The Paul Iusztin user — a real ``User`` row in the test DB.

    Inserting the row fires the ``after_insert`` hook, which materializes the
    active ``person:self`` node carrying ``properties.is_active_user=True`` —
    the single source of truth the fan-out parent flow enumerates. Using a real
    user (not a bare ``PydanticObjectId``) is what makes the fan-out e2e
    exercise the genuine active-user selection path.
    """

    user = User(identifier=PAUL_IDENTIFIER, attributes={"name": "Paul Iusztin"})
    await user.insert()
    return user


@pytest.fixture
def _no_cost_guard(mocker):
    """Trip-wire: fail loudly if the default path constructs an LLM/embedder.

    The dream sweep is embedding-READ-only (it reuses stored vectors) and the
    default path runs NO LLM (``enable_supersession_judge=false``). These spies
    turn either a Voyage embedding call or a Gemini LLM call into a hard test
    failure, proving the zero-cost invariants directly rather than by trust.
    """

    llm_spy = mocker.patch(
        "tree.memory.consolidation.dream.get_llm",
        side_effect=AssertionError(
            "get_llm must not be called on the default dream path (zero LLM)"
        ),
    )
    embed_spy = mocker.patch(
        "tree.memory.consolidation.dream.get_search_embedding_model",
        side_effect=AssertionError(
            "embedding model must not be built by the dream sweep "
            "(embedding-READ-only; zero Voyage calls)"
        ),
    )
    return llm_spy, embed_spy


def _database(mongo_client):
    return mongo_client[TEST_DATABASE]


async def _confirmed_audit_edge(collection, user_id: PydanticObjectId) -> dict | None:
    return await collection.find_one(
        {
            "user_id": user_id,
            "kind": "edge",
            "type": EdgeType.SAME_AS.value,
            "properties.status": "confirmed",
        }
    )


async def _tombstones(collection, user_id: PydanticObjectId) -> list[dict]:
    return [
        d
        async for d in collection.find(
            {"user_id": user_id, "kind": "node", "merged_into": {"$nin": [None, ""]}}
        )
    ]


# ===========================================================================
# HEADLINE: collapse-then-noop watermark proof (auto-merge + flag bands)
# ===========================================================================


@pytest.mark.parametrize(
    ("band", "cos", "action"),
    [
        ("auto_merge", 0.999, "merged"),
        ("flag", 0.88, "flagged"),
    ],
)
async def test_collapse_then_noop_watermark_proof(
    mongo_client, kg_collection, paul, dream_cfg, _no_cost_guard, band, cos, action
) -> None:
    """The headline acceptance: parallel duplicates collapse, then run 2 noops.

    Covers BOTH threshold bands (parametrized): the auto-merge tier tombstones
    the loser and confirms the SAME_AS audit edge; the flag tier upserts a
    pending SAME_AS for human review. In either band the watermark advances on
    run 1 and run 2 (no new ingestion) is a near-noop — proving the incremental
    watermark optimization holds.
    """

    dream_cfg(enabled=True, enable_supersession_judge=False)
    user_id = paul.id
    last_run = datetime(2026, 5, 10, tzinfo=UTC)
    fresh = last_run + timedelta(days=1)
    old = last_run - timedelta(days=7)

    # A prior watermark so only the parallel-fresh node drives (the OLD twin is
    # in the search space but outside the driving delta).
    await record_dream_run(
        database=_database(mongo_client),
        user_id=user_id,
        run_start=last_run,
        last_run_id="seed",
        last_stats={},
    )
    # The flag band needs names that DON'T fuzzy-match (else fuzzy promotes the
    # pair to a merge); the auto-merge band uses near-identical names.
    if band == "auto_merge":
        name_a, name_b = "Paul Iusztin", "Paul  Iusztin"
    else:
        name_a, name_b = "Zoltar", "Xerxes"

    id_a, id_b = await _seed_parallel_duplicate(
        collection=kg_collection,
        user_id=user_id,
        base_id=band,
        name_a=name_a,
        name_b=name_b,
        cos=cos,
        fresh=fresh,
        old=old,
    )

    # --- RUN 1: collapse -----------------------------------------------------
    with prefect_tags("tests"):
        run1 = await dream_consolidation(user_id=user_id, dry_run=False)

    assert run1.stats.nodes_driven == 1  # only the fresh parallel twin drives
    assert len(run1.pairs) == 1
    assert run1.pairs[0].action == action
    assert run1.watermark_advanced is True

    wm_after_1 = await load_watermark(database=_database(mongo_client), user_id=user_id)
    # Watermark advanced to run_start (ms precision in Mongo).
    assert abs((wm_after_1.last_run_at - run1.run_start).total_seconds()) < 0.002
    assert wm_after_1.last_run_at > last_run

    if action == "merged":
        # Auto-merge: exactly one tombstone + a confirmed/reviewed_by=dream edge.
        tombstoned = await _tombstones(kg_collection, user_id)
        assert len(tombstoned) == 1
        audit = await _confirmed_audit_edge(kg_collection, user_id)
        assert audit is not None
        assert audit["properties"]["reviewed_by"] == "dream"
    else:
        # Flag: a pending SAME_AS exists, no node tombstoned.
        pending = await kg_collection.find_one(
            {
                "user_id": user_id,
                "kind": "edge",
                "type": EdgeType.SAME_AS.value,
                "properties.status": "pending",
            }
        )
        assert pending is not None
        assert await _tombstones(kg_collection, user_id) == []

    # --- RUN 2: near-noop (THE incremental watermark proof) ------------------
    with prefect_tags("tests"):
        run2 = await dream_consolidation(user_id=user_id, dry_run=False)

    # The decided pair is never re-touched: zero new merges/flags. The driving
    # delta is empty for the flag band (nothing's updated_at moved) and at most
    # the merge-winner for the auto-merge band (its updated_at was stamped at
    # the merge instant, the documented slight overlap) — but it has no
    # actionable twin, so still zero new pairs.
    assert run2.pairs == []
    assert run2.stats.auto_merged == 0
    assert run2.stats.flagged == 0
    if action == "flagged":
        assert run2.stats.nodes_driven == 0  # truly empty delta
    else:
        assert run2.stats.nodes_driven <= 1  # at most the winner re-drives

    # Run 2 advanced the watermark further forward, and did not double-act.
    assert run2.run_start >= run1.run_start
    if action == "merged":
        assert len(await _tombstones(kg_collection, user_id)) == 1


# ===========================================================================
# Incremental catch-up: a node ingested AFTER dream 1 is caught by dream 2
# ===========================================================================


async def test_node_ingested_after_first_dream_is_caught_by_second(
    mongo_client, kg_collection, paul, dream_cfg, _no_cost_guard
) -> None:
    """A twin written AFTER run 1 (updated_at > watermark) collapses on run 2.

    This is the no-gap guarantee: run 1 advances the watermark to its
    ``run_start``; a node ingested afterwards has ``updated_at`` past that
    watermark, so it lands in run 2's driving delta and finds its older twin in
    the full-graph search space. Proves incremental catch-up across runs.
    """

    dream_cfg(enabled=True, enable_supersession_judge=False)
    user_id = paul.id
    last_run = datetime(2026, 5, 10, tzinfo=UTC)
    old = last_run - timedelta(days=7)

    await record_dream_run(
        database=_database(mongo_client),
        user_id=user_id,
        run_start=last_run,
        last_run_id="seed",
        last_stats={},
    )

    # An older "anchor" node, present before run 1 and outside its delta.
    anchor_id = f"{user_id}:person:catchup_anchor"
    await kg_collection.insert_one(
        _node_doc(
            node_id=anchor_id,
            name="Ada Lovelace",
            user_id=user_id,
            embedding=_vec(0.999),
            updated_at=old,
            created_at=old,
        )
    )
    await _wait_for_indexed_count(kg_collection, user_id, expected=1)

    # RUN 1: nothing fresh to drive → clean noop, watermark advances.
    with prefect_tags("tests"):
        run1 = await dream_consolidation(user_id=user_id, dry_run=False)
    assert run1.pairs == []
    assert run1.stats.nodes_driven == 0
    assert run1.watermark_advanced is True
    wm1 = await load_watermark(database=_database(mongo_client), user_id=user_id)

    # NOW ingest the parallel twin AFTER run 1 — its updated_at is past wm1.
    late_id = f"{user_id}:person:catchup_late"
    await kg_collection.insert_one(
        _node_doc(
            node_id=late_id,
            name="Ada Lovelace",
            user_id=user_id,
            embedding=_vec(1.0),
            updated_at=datetime.now(UTC) + timedelta(seconds=1),
        )
    )
    await _wait_for_indexed_count(kg_collection, user_id, expected=2)

    # RUN 2: the late node is in the delta and collapses with the anchor.
    with prefect_tags("tests"):
        run2 = await dream_consolidation(user_id=user_id, dry_run=False)

    assert run2.stats.nodes_driven == 1  # only the late twin drives
    assert run2.stats.auto_merged == 1
    assert len(await _tombstones(kg_collection, user_id)) == 1
    wm2 = await load_watermark(database=_database(mongo_client), user_id=user_id)
    assert wm2.last_run_at > wm1.last_run_at


# ===========================================================================
# dry_run rehearsal before the real run
# ===========================================================================


async def test_dry_run_writes_nothing_then_real_run_collapses(
    mongo_client, kg_collection, paul, dream_cfg, _no_cost_guard
) -> None:
    """A dry-run rehearsal reports the pair, writes nothing, holds the watermark;
    the subsequent real run still collapses it."""

    dream_cfg(enabled=True, enable_supersession_judge=False)
    user_id = paul.id
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
    await _seed_parallel_duplicate(
        collection=kg_collection,
        user_id=user_id,
        base_id="dry",
        name_a="Grace Hopper",
        name_b="Grace Hopper",
        cos=0.999,
        fresh=fresh,
        old=old,
    )

    # --- dry-run rehearsal: reports the would-be merge, writes nothing -------
    with prefect_tags("tests"):
        dry = await dream_consolidation(user_id=user_id, dry_run=True)

    assert len(dry.pairs) == 1
    assert dry.pairs[0].action == "merged"
    assert dry.watermark_advanced is False
    assert await _tombstones(kg_collection, user_id) == []
    assert (
        await kg_collection.find_one(
            {"user_id": user_id, "kind": "edge", "type": EdgeType.SAME_AS.value}
        )
        is None
    )
    wm_dry = await load_watermark(database=_database(mongo_client), user_id=user_id)
    assert wm_dry.last_run_at == last_run  # unchanged

    # --- real run: now the pair collapses ------------------------------------
    with prefect_tags("tests"):
        real = await dream_consolidation(user_id=user_id, dry_run=False)

    assert real.stats.auto_merged == 1
    assert len(await _tombstones(kg_collection, user_id)) == 1
    assert await _confirmed_audit_edge(kg_collection, user_id) is not None
    wm_real = await load_watermark(database=_database(mongo_client), user_id=user_id)
    assert wm_real.last_run_at > last_run


# ===========================================================================
# Respect a human's earlier rejection
# ===========================================================================


async def test_rejected_pair_is_never_remerged_or_reflagged(
    mongo_client, kg_collection, paul, dream_cfg, _no_cost_guard
) -> None:
    """A pre-seeded ``rejected`` SAME_AS pair is left untouched by the dream."""

    dream_cfg(enabled=True, enable_supersession_judge=False)
    user_id = paul.id
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
                embedding=_vec(1.0),
                updated_at=fresh,
            ),
            _node_doc(
                node_id=id_old,
                name="Linus",
                user_id=user_id,
                embedding=_vec(0.999),
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

    assert report.pairs == []
    assert report.stats.auto_merged == 0
    assert report.stats.flagged == 0
    assert await _tombstones(kg_collection, user_id) == []
    edge = await kg_collection.find_one({"_id": f"{id_old}|same_as|{id_new}"})
    assert edge["properties"]["status"] == "rejected"


# ===========================================================================
# max_pairs cap stops the run cleanly
# ===========================================================================


async def test_max_pairs_cap_stops_run_and_records_cap_hit(
    mongo_client, kg_collection, paul, dream_cfg, _no_cost_guard
) -> None:
    """With ``max_pairs=1`` and TWO independent duplicate pairs, the run stops at
    the cap and records ``cap_hit=True`` without crashing."""

    dream_cfg(enabled=True, enable_supersession_judge=False, max_pairs=1)
    user_id = paul.id
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

    # Two disjoint near-duplicate pairs (4 nodes). Both fresh nodes would drive,
    # but max_pairs=1 must stop after the first actionable pair.
    docs = []
    for i, cos in ((1, 1.0), (2, 1.0)):
        docs.append(
            _node_doc(
                node_id=f"{user_id}:person:cap{i}_new",
                name=f"Cap Person {i}",
                user_id=user_id,
                embedding=_vec(cos),
                updated_at=fresh,
            )
        )
        docs.append(
            _node_doc(
                node_id=f"{user_id}:person:cap{i}_old",
                name=f"Cap Person {i}",
                user_id=user_id,
                embedding=_vec(0.999),
                updated_at=old,
                created_at=old,
            )
        )
    await kg_collection.insert_many(docs)
    await _wait_for_indexed_count(kg_collection, user_id, expected=4)

    with prefect_tags("tests"):
        report = await dream_consolidation(user_id=user_id, dry_run=False)

    assert report.stats.cap_hit is True
    assert len(report.pairs) == 1  # stopped at the cap
    # The watermark still advances (a real run completed without crashing).
    assert report.watermark_advanced is True


# ===========================================================================
# #048 routing (cheap, no mongot needed but kept in-suite so the headline
# acceptance file is self-contained)
# ===========================================================================


def test_search_embedding_model_routes_through_voyage_text_client() -> None:
    """The #048 default flip routes the persisted search embedding through the
    text client (``/v1/embeddings``), not the multimodal endpoint."""

    model = get_search_embedding_model()
    assert isinstance(model, VoyageTextEmbeddingModel)


# ===========================================================================
# Fan-out parent flow over the genuine active Paul user (end-to-end)
# ===========================================================================


async def test_fan_out_collapses_paul_duplicates_end_to_end(
    mongo_client, kg_collection, paul, dream_cfg, _no_cost_guard
) -> None:
    """The scheduled parent flow enumerates the active Paul user and its per-user
    dream collapses his parallel duplicates — full chain, no fakes on the dream.

    Unlike the #052 fan-out tests (which spy on a faked per-user dream), this
    runs the REAL ``dream_consolidation`` per user, so the fan-out -> per-user
    -> sweep -> merge -> watermark chain executes end to end on Paul. ``paul``'s
    insert hook created the active ``person:self`` node the parent flow selects.
    """

    dream_cfg(enabled=True, dry_run=False, enable_supersession_judge=False)
    user_id = paul.id
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
    await _seed_parallel_duplicate(
        collection=kg_collection,
        user_id=user_id,
        base_id="fanout",
        name_a="Paul Iusztin",
        name_b="Paul  Iusztin",
        cos=0.999,
        fresh=fresh,
        old=old,
    )

    with prefect_tags("tests"):
        stats = await dream_consolidation_all_users()

    # The parent flow found exactly the one active user (Paul) and ran it.
    assert stats.enabled is True
    assert stats.users_total == 1
    assert stats.succeeded == 1
    assert stats.failed == 0

    # The per-user dream actually collapsed Paul's parallel duplicates.
    assert len(await _tombstones(kg_collection, user_id)) == 1
    assert await _confirmed_audit_edge(kg_collection, user_id) is not None
    wm = await load_watermark(database=_database(mongo_client), user_id=user_id)
    assert wm.last_run_at > last_run
