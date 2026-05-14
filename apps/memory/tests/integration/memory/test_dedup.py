"""Integration tests for ``dedupe_entity`` against Atlas-local.

These tests exercise the real ``$vectorSearch`` aggregation pipeline. They
seed the ``knowledge_graph`` collection with hand-crafted 8-dimensional
embeddings whose pairwise cosine similarities are known by construction, so
the tiered decisions are deterministic.

Vectors are 8-dim unit vectors whose first two components encode the angle
``theta`` with respect to the query vector ``(1, 0, 0, 0, 0, 0, 0, 0)``:

    candidate(theta) = (cos(theta), sin(theta), 0, 0, 0, 0, 0, 0)
    cos(theta) = cosine_similarity(query, candidate(theta))

So ``theta = acos(0.97)`` produces a candidate at raw cosine ~0.97, etc.

``DeduplicationConfig`` thresholds (``auto_merge_threshold``,
``flag_threshold``, ``fuzzy_threshold``) all speak raw cosine, matching
``resolution.semantic._cosine_similarity`` and the published API
contract on ``DeduplicationResult.similarity_score``. ``dedupe_entity``
normalizes Atlas' ``(1 + cos) / 2`` score back to raw cosine internally,
so these fixtures seed at raw cosine directly via
``_vector_with_raw_cosine``.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime

import pytest

from tree.config.app_config import app_config
from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.memory.extraction.dedup import (
    DeduplicationConfig,
    dedupe_entity,
)
from tree.memory.indexing.core import ensure_indexes
from tree.models.fake_model import FakeEmbeddingModel

TEST_DATABASE = "integration_tests_twin"
_DIMS = 8
_NOW = datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------


def _query_vector() -> list[float]:
    """The canonical query vector: ``(1, 0, ..., 0)``."""
    vec = [0.0] * _DIMS
    vec[0] = 1.0
    return vec


def _vector_with_raw_cosine(target_cos: float) -> list[float]:
    """Return an 8-dim unit vector whose cosine similarity with the canonical
    query vector equals ``target_cos`` exactly.

    Since ``dedupe_entity`` normalizes Atlas' ``(1 + cos) / 2`` score back
    to raw cosine before comparing against the configured thresholds (which
    speak raw cosine), seeding by raw cosine matches the spec's AC text
    directly (e.g. cos≈0.97 → merged, cos≈0.88 → flagged, cos≈0.70 → none).
    """

    # Clamp to avoid floating-point drift outside [-1, 1].
    cos_value = max(-1.0, min(1.0, target_cos))
    sin_value = math.sqrt(max(0.0, 1.0 - cos_value * cos_value))
    vec = [0.0] * _DIMS
    vec[0] = cos_value
    vec[1] = sin_value
    return vec


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _node_doc(
    *,
    node_id: str,
    name: str,
    node_type: NodeType,
    embedding: list[float],
    merged_into: str | None = None,
    aliases: list[str] | None = None,
) -> dict:
    return {
        "_id": node_id,
        "kind": "node",
        "type": node_type.value,
        "name": name,
        "properties": {"aliases": aliases or []},
        "embedding": embedding,
        "sources": [],
        "merged_into": merged_into,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _edge_doc(
    *,
    edge_id: str,
    source_node_id: str,
    source_type: NodeType,
    target_node_id: str,
    target_type: NodeType,
    edge_type: EdgeType,
    properties: dict | None = None,
) -> dict:
    return {
        "_id": edge_id,
        "kind": "edge",
        "type": edge_type.value,
        "source_node_id": source_node_id,
        "source_type": source_type.value,
        "target_node_id": target_node_id,
        "target_type": target_type.value,
        "properties": properties or {},
        "sources": [],
        "created_at": _NOW,
        "updated_at": _NOW,
    }


async def _wait_for_indexed_count(
    collection, expected: int, timeout: float = 30.0
) -> None:
    """Poll ``$vectorSearch`` until at least ``expected`` nodes are returned.

    Mongot indexes asynchronously; without this, the first ``$vectorSearch``
    call after seeding may return an empty cursor.
    """

    probe_vector = _query_vector()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        cursor = await collection.aggregate(
            [
                {
                    "$vectorSearch": {
                        "index": "vector_index",
                        "path": "embedding",
                        "queryVector": probe_vector,
                        "numCandidates": 100,
                        "limit": 50,
                        "filter": {"kind": "node"},
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
    """Force the vector index to be created with 8 dimensions for these tests."""

    mocker.patch.object(app_config.models.embedding, "dimensions", _DIMS)


@pytest.fixture
async def _kg_collection(mongo_client):
    """Hand back the ``knowledge_graph`` collection with a ready vector index.

    Drops the collection after each test (in addition to the autouse cleanup
    in ``tests/integration/conftest.py``) so the search index is rebuilt
    fresh per test — this avoids cross-test cosine-score contamination.
    """

    db = mongo_client[TEST_DATABASE]
    col = db["knowledge_graph"]
    await ensure_indexes(
        mongo_client,
        TEST_DATABASE,
        embedding_model=FakeEmbeddingModel(dimensions=_DIMS),
    )
    yield col
    await db.drop_collection("knowledge_graph")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_skip_without_mongot")
class TestDedupeEntityTiers:
    async def test_three_tier_decision_merged(
        self, mongo_client, _kg_collection
    ) -> None:
        """Top candidate at raw cos ~0.97 → ``action="merged"``."""

        await _kg_collection.insert_many(
            [
                _node_doc(
                    node_id="person:high",
                    name="alice smith",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.97),
                ),
                _node_doc(
                    node_id="person:mid",
                    name="alyce smyth",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.88),
                ),
                _node_doc(
                    node_id="person:low",
                    name="bob jones",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.70),
                ),
            ]
        )
        await _wait_for_indexed_count(_kg_collection, expected=3)

        config = DeduplicationConfig(use_fuzzy_matching=False)
        result = await dedupe_entity(
            database=mongo_client[TEST_DATABASE],
            name="alice smith query",
            entity_type=NodeType.PERSON,
            embedding=_query_vector(),
            config=config,
        )

        assert result.action == "merged"
        assert result.matched_node_id == "person:high"
        assert result.similarity_score >= config.auto_merge_threshold

    async def test_three_tier_decision_flagged(
        self, mongo_client, _kg_collection
    ) -> None:
        """Top candidate at raw cos ~0.88 → ``action="flagged"``."""

        await _kg_collection.insert_many(
            [
                _node_doc(
                    node_id="person:mid",
                    name="alyce smyth",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.88),
                ),
                _node_doc(
                    node_id="person:low",
                    name="bob jones",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.70),
                ),
            ]
        )
        await _wait_for_indexed_count(_kg_collection, expected=2)

        config = DeduplicationConfig(use_fuzzy_matching=False)
        result = await dedupe_entity(
            database=mongo_client[TEST_DATABASE],
            name="alyce smyth query",
            entity_type=NodeType.PERSON,
            embedding=_query_vector(),
            config=config,
        )

        assert result.action == "flagged"
        assert result.matched_node_id == "person:mid"
        assert (
            config.flag_threshold
            <= result.similarity_score
            < config.auto_merge_threshold
        )

    async def test_three_tier_decision_none(self, mongo_client, _kg_collection) -> None:
        """Top candidate at raw cos ~0.70 → ``action="none"``."""

        await _kg_collection.insert_one(
            _node_doc(
                node_id="person:low",
                name="bob jones",
                node_type=NodeType.PERSON,
                embedding=_vector_with_raw_cosine(0.70),
            )
        )
        await _wait_for_indexed_count(_kg_collection, expected=1)

        config = DeduplicationConfig(use_fuzzy_matching=False)
        result = await dedupe_entity(
            database=mongo_client[TEST_DATABASE],
            name="bob jones query",
            entity_type=NodeType.PERSON,
            embedding=_query_vector(),
            config=config,
        )

        assert result.action == "none"
        assert result.matched_node_id is None

    async def test_tombstoned_candidate_excluded(
        self, mongo_client, _kg_collection
    ) -> None:
        """Nodes with ``merged_into`` set must not surface, even at cos ~0.99."""

        await _kg_collection.insert_many(
            [
                _node_doc(
                    node_id="person:tombstoned",
                    name="alice old",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.99),
                    merged_into="person:winner",
                ),
                # A live but low-similarity node, so we know $vectorSearch is reachable.
                _node_doc(
                    node_id="person:live_low",
                    name="bob",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.30),
                ),
            ]
        )
        await _wait_for_indexed_count(_kg_collection, expected=2)

        config = DeduplicationConfig(use_fuzzy_matching=False)
        result = await dedupe_entity(
            database=mongo_client[TEST_DATABASE],
            name="alice new",
            entity_type=NodeType.PERSON,
            embedding=_query_vector(),
            config=config,
        )

        # Tombstone is filtered out; live_low is well below flag_threshold.
        assert result.action == "none"
        assert result.matched_node_id is None

    async def test_match_same_type_only_filters_other_types(
        self, mongo_client, _kg_collection
    ) -> None:
        """With ``match_same_type_only=True``, a TASK never matches a PERSON query."""

        await _kg_collection.insert_one(
            _node_doc(
                node_id="task:write_report",
                name="write the report",
                node_type=NodeType.TASK,
                embedding=_vector_with_raw_cosine(0.99),
            )
        )
        await _wait_for_indexed_count(_kg_collection, expected=1)

        config = DeduplicationConfig(
            use_fuzzy_matching=False, match_same_type_only=True
        )
        result = await dedupe_entity(
            database=mongo_client[TEST_DATABASE],
            name="write report",
            entity_type=NodeType.PERSON,
            embedding=_query_vector(),
            config=config,
        )

        assert result.action == "none"

    async def test_reject_pair_filter_drops_candidate(
        self, mongo_client, _kg_collection
    ) -> None:
        """A SAME_AS{status:"rejected"} edge between the prospective ID
        and a candidate suppresses that candidate from the result.

        Seed only the rejected candidate (no live alternative). With the
        rejected node filtered out and a low-similarity bystander present,
        the result must be ``action="none"`` — directly verifying that the
        rejected candidate was genuinely dropped from the pipeline rather
        than merely deranked behind a self-match.
        """

        await _kg_collection.insert_many(
            [
                _node_doc(
                    node_id="person:b",
                    name="alyce",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.92),
                ),
                # Low-similarity bystander so we know $vectorSearch is reachable.
                _node_doc(
                    node_id="person:bystander",
                    name="zach",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(-0.40),
                ),
                _edge_doc(
                    edge_id="person:a|same_as|person:b",
                    source_node_id="person:a",
                    source_type=NodeType.PERSON,
                    target_node_id="person:b",
                    target_type=NodeType.PERSON,
                    edge_type=EdgeType.SAME_AS,
                    properties={"status": "rejected"},
                ),
            ]
        )
        await _wait_for_indexed_count(_kg_collection, expected=2)

        config = DeduplicationConfig(use_fuzzy_matching=False)
        # Query as if we are about to insert person:a (which is not in the
        # graph). person:b is the only otherwise-eligible candidate, but it
        # is filtered out by the rejected SAME_AS edge.
        result = await dedupe_entity(
            database=mongo_client[TEST_DATABASE],
            name="alice",
            entity_type=NodeType.PERSON,
            embedding=_query_vector(),
            config=config,
            incoming_node_id="person:a",
        )

        assert result.action == "none"
        assert result.matched_node_id is None

    async def test_reject_pair_filter_reversed_edge_direction(
        self, mongo_client, _kg_collection
    ) -> None:
        """The reject-pair filter is direction-agnostic."""

        await _kg_collection.insert_many(
            [
                _node_doc(
                    node_id="person:c",
                    name="charlie",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.92),
                ),
                _node_doc(
                    node_id="person:d",
                    name="charley",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.92),
                ),
                # Edge points b → a (source=d, target=c). Filter must still match.
                _edge_doc(
                    edge_id="person:d|same_as|person:c",
                    source_node_id="person:d",
                    source_type=NodeType.PERSON,
                    target_node_id="person:c",
                    target_type=NodeType.PERSON,
                    edge_type=EdgeType.SAME_AS,
                    properties={"status": "rejected"},
                ),
            ]
        )
        await _wait_for_indexed_count(_kg_collection, expected=2)

        config = DeduplicationConfig(use_fuzzy_matching=False)
        result = await dedupe_entity(
            database=mongo_client[TEST_DATABASE],
            name="charlie",
            entity_type=NodeType.PERSON,
            embedding=_query_vector(),
            config=config,
            incoming_node_id="person:c",
        )

        assert result.matched_node_id != "person:d"

    async def test_pending_same_as_edge_does_not_filter(
        self, mongo_client, _kg_collection
    ) -> None:
        """Only ``status="rejected"`` edges filter candidates. Pending stays."""

        await _kg_collection.insert_many(
            [
                _node_doc(
                    node_id="person:e",
                    name="eve",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.92),
                ),
                _node_doc(
                    node_id="person:f",
                    name="evie",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.92),
                ),
                _edge_doc(
                    edge_id="person:e|same_as|person:f",
                    source_node_id="person:e",
                    source_type=NodeType.PERSON,
                    target_node_id="person:f",
                    target_type=NodeType.PERSON,
                    edge_type=EdgeType.SAME_AS,
                    properties={"status": "pending"},
                ),
            ]
        )
        await _wait_for_indexed_count(_kg_collection, expected=2)

        config = DeduplicationConfig(use_fuzzy_matching=False)
        result = await dedupe_entity(
            database=mongo_client[TEST_DATABASE],
            name="brand new",
            entity_type=NodeType.PERSON,
            embedding=_query_vector(),
            config=config,
            incoming_node_id="person:e",
        )

        # Pending edge does not filter — person:f remains in candidates and
        # since e==f score, one of them must be the match (flagged tier).
        assert result.action == "flagged"
        assert result.matched_node_id in {"person:e", "person:f"}

    async def test_fuzzy_boost_produces_both_match_type(
        self, mongo_client, _kg_collection
    ) -> None:
        """RapidFuzz boost on a near-exact name turns ``embedding`` into ``both``.

        Seed a PERSON whose name matches the query string exactly but whose
        embedding only scores ~0.86 (flagged tier on semantics alone). With
        the fuzzy boost, ``match_type`` becomes ``"both"`` and the score is
        the mean of semantic and fuzzy.
        """

        await _kg_collection.insert_one(
            _node_doc(
                node_id="person:alice_smith",
                name="alice smith",
                node_type=NodeType.PERSON,
                embedding=_vector_with_raw_cosine(0.86),
            )
        )
        await _wait_for_indexed_count(_kg_collection, expected=1)

        config = DeduplicationConfig(
            use_fuzzy_matching=True,
            fuzzy_threshold=0.90,
        )
        result = await dedupe_entity(
            database=mongo_client[TEST_DATABASE],
            name="alice smith",  # identical to candidate name → fuzz==1.0
            entity_type=NodeType.PERSON,
            embedding=_query_vector(),
            config=config,
        )

        assert result.matched_node_id == "person:alice_smith"
        assert result.match_type == "both"
        # Score should be the average of semantic (~0.86) and fuzzy (1.0) = ~0.93.
        assert result.similarity_score == pytest.approx(0.93, abs=0.02)

    async def test_incoming_node_id_omitted_does_not_filter(
        self, mongo_client, _kg_collection
    ) -> None:
        """Reject-pair filter is a no-op when ``incoming_node_id`` is None.

        Seed a rejected SAME_AS edge against an arbitrary ``_id`` that we
        never pass in. The candidate must still surface.
        """

        await _kg_collection.insert_many(
            [
                _node_doc(
                    node_id="person:g",
                    name="grace",
                    node_type=NodeType.PERSON,
                    embedding=_vector_with_raw_cosine(0.97),
                ),
                _edge_doc(
                    edge_id="person:unrelated|same_as|person:g",
                    source_node_id="person:unrelated",
                    source_type=NodeType.PERSON,
                    target_node_id="person:g",
                    target_type=NodeType.PERSON,
                    edge_type=EdgeType.SAME_AS,
                    properties={"status": "rejected"},
                ),
            ]
        )
        await _wait_for_indexed_count(_kg_collection, expected=1)

        config = DeduplicationConfig(use_fuzzy_matching=False)
        result = await dedupe_entity(
            database=mongo_client[TEST_DATABASE],
            name="grace",
            entity_type=NodeType.PERSON,
            embedding=_query_vector(),
            config=config,
            # incoming_node_id intentionally omitted
        )

        assert result.action == "merged"
        assert result.matched_node_id == "person:g"
