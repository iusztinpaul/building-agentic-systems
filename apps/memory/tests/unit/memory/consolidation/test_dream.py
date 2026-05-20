"""Unit tests for the dream-consolidation pure logic (#051).

These tests exercise the two-set rule, self-match exclusion, pair hygiene
(``id1 < id2`` + skip-existing-SAME_AS), the ``max_pairs`` cap, and the
no-Voyage-on-the-sweep-path invariant WITHOUT touching MongoDB or
``$vectorSearch``. The DB collection is a hand-rolled async fake and
``dedupe_entity`` is patched so the candidate set is fully deterministic.

The Prefect ``@task`` / ``@flow`` wiring is intentionally NOT unit-tested
(per ``CLAUDE.md``) — that's covered end-to-end in the integration suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from beanie import PydanticObjectId

from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.memory.consolidation import dream as dream_mod
from tree.memory.consolidation.dream import (
    _collect_dream_candidates,
    _ordered,
    _partition_node_types,
)
from tree.memory.extraction.dedup import (
    DeduplicationConfig,
    DeduplicationResult,
    decide_from_candidates,
)

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_LAST_RUN = datetime(2026, 5, 10, 0, 0, tzinfo=UTC)
_FRESH = _LAST_RUN + timedelta(days=1)
_OLD = _LAST_RUN - timedelta(days=7)


# ---------------------------------------------------------------------------
# Async fake collection
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def __aiter__(self) -> "_FakeCursor":
        self._it = iter(self._docs)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeCollection:
    """Minimal stand-in for the ``knowledge_graph`` async collection.

    ``find`` returns the seeded nodes that satisfy the watermark/embedding
    predicate the dream sweep issues; ``find_one`` answers the SAME_AS
    existence probe from the seeded edge list.
    """

    def __init__(
        self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]] | None = None
    ) -> None:
        self._nodes = nodes
        self._edges = edges or []

    def find(self, query: dict[str, Any], *args: Any, **kwargs: Any) -> _FakeCursor:
        etype = query["type"]
        gt = query["updated_at"]["$gt"]
        matched = [
            n
            for n in self._nodes
            if n.get("type") == etype
            and n.get("merged_into") in (None, "", False)
            and n.get("embedding")
            and n.get("updated_at") > gt
        ]
        return _FakeCursor(matched)

    async def find_one(
        self, query: dict[str, Any], projection: Any = None
    ) -> dict[str, Any] | None:
        if query.get("type") == EdgeType.SAME_AS.value:
            or_clauses = query["$or"]
            for edge in self._edges:
                for clause in or_clauses:
                    if (
                        edge.get("source_node_id") == clause["source_node_id"]
                        and edge.get("target_node_id") == clause["target_node_id"]
                    ):
                        return edge
            return None
        return None


class _FakeDatabase:
    def __init__(self, collection: _FakeCollection) -> None:
        self._collection = collection

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._collection


def _node(
    *,
    node_id: str,
    name: str,
    node_type: NodeType = NodeType.PERSON,
    updated_at: datetime,
    embedding: list[float] | None = None,
    merged_into: str | None = None,
) -> dict[str, Any]:
    return {
        "_id": node_id,
        "user_id": _USER_ID,
        "kind": "node",
        "type": node_type.value,
        "name": name,
        "embedding": embedding if embedding is not None else [1.0, 0.0],
        "merged_into": merged_into,
        "updated_at": updated_at,
        "created_at": updated_at,
    }


def _same_as_edge(src: str, tgt: str, status: str = "pending") -> dict[str, Any]:
    return {
        "source_node_id": src,
        "target_node_id": tgt,
        "kind": "edge",
        "type": EdgeType.SAME_AS.value,
        "properties": {"status": status},
    }


def _merged_result(matched_id: str, *, self_id: str, score: float = 0.97):
    """A dedupe result whose candidates contain the self node + a twin.

    The patched ``dedupe_entity`` returns ``action="merged"`` with the self
    node ranked top (cos≈1.0) so the sweep's self-exclusion re-decision is
    actually exercised. ``decide_from_candidates`` is the real function, so
    the score the test asserts on flows through the real tier logic.
    """

    self_atlas = (1.0 + 1.0) / 2.0  # cos 1.0 → Atlas (1+cos)/2
    twin_atlas = (1.0 + score) / 2.0
    candidates = [
        {"_id": self_id, "name": "self", "similarity_score": self_atlas},
        {"_id": matched_id, "name": matched_id, "similarity_score": twin_atlas},
    ]
    return DeduplicationResult(
        action="merged",
        matched_node_id=self_id,  # self ranks top before exclusion
        matched_node_name="self",
        similarity_score=1.0,
        match_type="embedding",
        candidates=candidates,
    )


_CONFIG = DeduplicationConfig(use_fuzzy_matching=False)


# ---------------------------------------------------------------------------
# decide_from_candidates — self-exclusion contract
# ---------------------------------------------------------------------------


class TestDecideFromCandidates:
    def test_excludes_self_and_picks_next(self) -> None:
        candidates = [
            {"_id": "person:self", "name": "self", "similarity_score": 1.0},
            {
                "_id": "person:twin",
                "name": "twin",
                "similarity_score": (1.0 + 0.97) / 2,
            },
        ]

        result = decide_from_candidates(
            name="self",
            candidates=candidates,
            config=_CONFIG,
            exclude_ids={"person:self"},
        )

        assert result.matched_node_id == "person:twin"
        assert result.action == "merged"
        assert result.similarity_score == pytest.approx(0.97, abs=1e-6)

    def test_no_exclusion_picks_self(self) -> None:
        candidates = [
            {"_id": "person:self", "name": "self", "similarity_score": 1.0},
        ]

        result = decide_from_candidates(
            name="self", candidates=candidates, config=_CONFIG
        )

        assert result.matched_node_id == "person:self"

    def test_only_self_excluded_yields_none(self) -> None:
        candidates = [
            {"_id": "person:self", "name": "self", "similarity_score": 1.0},
        ]

        result = decide_from_candidates(
            name="self",
            candidates=candidates,
            config=_CONFIG,
            exclude_ids={"person:self"},
        )

        assert result.action == "none"
        assert result.matched_node_id is None


# ---------------------------------------------------------------------------
# Partition helper
# ---------------------------------------------------------------------------


def test_partition_excludes_structural_types() -> None:
    types = {t.value for t in _partition_node_types()}
    assert "document" not in types
    assert "chunk" not in types
    assert "person" in types


def test_ordered() -> None:
    assert _ordered("b", "a") == ("a", "b")
    assert _ordered("a", "b") == ("a", "b")


# ---------------------------------------------------------------------------
# The two-set rule
# ---------------------------------------------------------------------------


class TestTwoSetRule:
    async def test_driving_set_is_watermark_filtered(self, mocker) -> None:
        """Three nodes; only one is watermark-fresh → dedupe_entity called once.

        Proves the DRIVING set is watermark-filtered: the two
        ``updated_at <= last_run_at`` nodes are never driven.
        """

        nodes = [
            _node(node_id="person:fresh", name="paul", updated_at=_FRESH),
            _node(node_id="person:old1", name="paula", updated_at=_OLD),
            _node(node_id="person:old2", name="pawel", updated_at=_OLD),
        ]
        database = _FakeDatabase(_FakeCollection(nodes))

        spy = mocker.patch(
            "tree.memory.consolidation.dream.dedupe_entity",
            return_value=DeduplicationResult(action="none"),
        )

        pairs, stats = await _collect_dream_candidates(
            database=database,
            user_id=_USER_ID,
            last_run_at=_LAST_RUN,
            dedup_config=_CONFIG,
            max_pairs=10000,
        )

        # Exactly one driving node across the person partition.
        person_calls = [
            c
            for c in spy.call_args_list
            if c.kwargs.get("entity_type") == NodeType.PERSON
        ]
        assert len(person_calls) == 1
        assert person_calls[0].kwargs["incoming_node_id"] == "person:fresh"
        assert stats.nodes_driven == 1
        assert pairs == []

    async def test_search_space_is_not_watermark_filtered(self, mocker) -> None:
        """The fresh driving node finds an OLDER twin and the pair is acted on.

        The OLD twin (``updated_at <= last_run_at``) is NOT in the driving
        set, yet it surfaces as ``dedupe_entity``'s candidate — proving the
        search space is the full graph, not watermark-filtered.
        """

        nodes = [
            _node(node_id="person:fresh", name="paul", updated_at=_FRESH),
            _node(node_id="person:oldtwin", name="paul", updated_at=_OLD),
        ]
        database = _FakeDatabase(_FakeCollection(nodes))

        mocker.patch(
            "tree.memory.consolidation.dream.dedupe_entity",
            return_value=_merged_result("person:oldtwin", self_id="person:fresh"),
        )

        pairs, stats = await _collect_dream_candidates(
            database=database,
            user_id=_USER_ID,
            last_run_at=_LAST_RUN,
            dedup_config=_CONFIG,
            max_pairs=10000,
        )

        assert len(pairs) == 1
        pair = pairs[0]
        assert {pair.id1, pair.id2} == {"person:fresh", "person:oldtwin"}
        assert pair.id1 < pair.id2
        assert pair.action == "merged"
        assert pair.driving_id == "person:fresh"
        assert pair.matched_id == "person:oldtwin"
        assert stats.auto_merged == 1

    async def test_self_match_filtered(self, mocker) -> None:
        """A node alone in its partition never merges with itself."""

        nodes = [_node(node_id="person:solo", name="solo", updated_at=_FRESH)]
        database = _FakeDatabase(_FakeCollection(nodes))

        # dedupe_entity returns only the self node as the candidate.
        self_only = DeduplicationResult(
            action="merged",
            matched_node_id="person:solo",
            matched_node_name="solo",
            similarity_score=1.0,
            match_type="embedding",
            candidates=[
                {"_id": "person:solo", "name": "solo", "similarity_score": 1.0}
            ],
        )
        mocker.patch(
            "tree.memory.consolidation.dream.dedupe_entity", return_value=self_only
        )

        pairs, stats = await _collect_dream_candidates(
            database=database,
            user_id=_USER_ID,
            last_run_at=_LAST_RUN,
            dedup_config=_CONFIG,
            max_pairs=10000,
        )

        assert pairs == []
        assert stats.auto_merged == 0
        assert stats.nodes_driven == 1

    async def test_new_new_pair_processed_once(self, mocker) -> None:
        """Two fresh nodes that match each other → a single ordered pair."""

        nodes = [
            _node(node_id="person:newA", name="paul", updated_at=_FRESH),
            _node(node_id="person:newB", name="paul", updated_at=_FRESH),
        ]
        database = _FakeDatabase(_FakeCollection(nodes))

        def _fake_dedupe(**kwargs: Any) -> DeduplicationResult:
            self_id = kwargs["incoming_node_id"]
            other = "person:newB" if self_id == "person:newA" else "person:newA"
            return _merged_result(other, self_id=self_id)

        spy = mocker.patch(
            "tree.memory.consolidation.dream.dedupe_entity",
            side_effect=_fake_dedupe,
        )

        pairs, stats = await _collect_dream_candidates(
            database=database,
            user_id=_USER_ID,
            last_run_at=_LAST_RUN,
            dedup_config=_CONFIG,
            max_pairs=10000,
        )

        # Both nodes drive (so dedupe_entity is called twice), but the
        # ordered pair is collected once.
        assert spy.call_count == 2
        assert stats.nodes_driven == 2
        assert len(pairs) == 1
        assert pairs[0].id1 == "person:newA"
        assert pairs[0].id2 == "person:newB"
        assert stats.auto_merged == 1

    async def test_existing_same_as_pair_skipped(self, mocker) -> None:
        """A pair that already has a SAME_AS edge (any status) is skipped."""

        nodes = [
            _node(node_id="person:fresh", name="paul", updated_at=_FRESH),
            _node(node_id="person:twin", name="paul", updated_at=_OLD),
        ]
        edges = [_same_as_edge("person:fresh", "person:twin", status="rejected")]
        database = _FakeDatabase(_FakeCollection(nodes, edges))

        mocker.patch(
            "tree.memory.consolidation.dream.dedupe_entity",
            return_value=_merged_result("person:twin", self_id="person:fresh"),
        )

        pairs, stats = await _collect_dream_candidates(
            database=database,
            user_id=_USER_ID,
            last_run_at=_LAST_RUN,
            dedup_config=_CONFIG,
            max_pairs=10000,
        )

        assert pairs == []
        assert stats.skipped_existing_same_as == 1
        assert stats.auto_merged == 0

    async def test_existing_same_as_reversed_direction_skipped(self, mocker) -> None:
        """The skip is direction-agnostic (edge stored twin→fresh)."""

        nodes = [
            _node(node_id="person:fresh", name="paul", updated_at=_FRESH),
            _node(node_id="person:twin", name="paul", updated_at=_OLD),
        ]
        edges = [_same_as_edge("person:twin", "person:fresh", status="confirmed")]
        database = _FakeDatabase(_FakeCollection(nodes, edges))

        mocker.patch(
            "tree.memory.consolidation.dream.dedupe_entity",
            return_value=_merged_result("person:twin", self_id="person:fresh"),
        )

        pairs, stats = await _collect_dream_candidates(
            database=database,
            user_id=_USER_ID,
            last_run_at=_LAST_RUN,
            dedup_config=_CONFIG,
            max_pairs=10000,
        )

        assert pairs == []
        assert stats.skipped_existing_same_as == 1

    async def test_max_pairs_cap_honored(self, mocker) -> None:
        """cap=1 with two candidate pairs → only one processed, cap_hit=True."""

        nodes = [
            _node(node_id="person:a", name="aa", updated_at=_FRESH),
            _node(node_id="person:b", name="bb", updated_at=_FRESH),
        ]
        database = _FakeDatabase(_FakeCollection(nodes))

        def _fake_dedupe(**kwargs: Any) -> DeduplicationResult:
            self_id = kwargs["incoming_node_id"]
            # Each fresh node matches a distinct OLD twin, so without the cap
            # we would collect two pairs.
            twin = f"person:twin_of_{self_id.split(':')[1]}"
            return _merged_result(twin, self_id=self_id)

        mocker.patch(
            "tree.memory.consolidation.dream.dedupe_entity",
            side_effect=_fake_dedupe,
        )

        pairs, stats = await _collect_dream_candidates(
            database=database,
            user_id=_USER_ID,
            last_run_at=_LAST_RUN,
            dedup_config=_CONFIG,
            max_pairs=1,
        )

        assert len(pairs) == 1
        assert stats.cap_hit is True

    async def test_flagged_tier_recorded(self, mocker) -> None:
        """A medium-confidence pair is flagged, not merged."""

        nodes = [
            _node(node_id="person:fresh", name="paul", updated_at=_FRESH),
            _node(node_id="person:twin", name="paul", updated_at=_OLD),
        ]
        database = _FakeDatabase(_FakeCollection(nodes))

        flagged = DeduplicationResult(
            action="flagged",
            matched_node_id="person:fresh",
            matched_node_name="self",
            similarity_score=1.0,
            match_type="embedding",
            candidates=[
                {"_id": "person:fresh", "name": "self", "similarity_score": 1.0},
                {
                    "_id": "person:twin",
                    "name": "twin",
                    "similarity_score": (1.0 + 0.88) / 2,
                },
            ],
        )
        mocker.patch(
            "tree.memory.consolidation.dream.dedupe_entity", return_value=flagged
        )

        pairs, stats = await _collect_dream_candidates(
            database=database,
            user_id=_USER_ID,
            last_run_at=_LAST_RUN,
            dedup_config=_CONFIG,
            max_pairs=10000,
        )

        assert len(pairs) == 1
        assert pairs[0].action == "flagged"
        assert pairs[0].similarity_score == pytest.approx(0.88, abs=1e-6)
        assert stats.flagged == 1
        assert stats.auto_merged == 0

    async def test_sweep_makes_zero_embedding_calls(self, mocker) -> None:
        """The sweep reads stored vectors — no embedding client constructed.

        Patches every embedding-model factory to blow up if called, then runs
        a full sweep that drives a node and acts on a pair.
        """

        nodes = [
            _node(node_id="person:fresh", name="paul", updated_at=_FRESH),
            _node(node_id="person:twin", name="paul", updated_at=_OLD),
        ]
        database = _FakeDatabase(_FakeCollection(nodes))

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("embedding model must not be used on the sweep path")

        mocker.patch(
            "tree.models.get_model.get_search_embedding_model", side_effect=_boom
        )
        mocker.patch(
            "tree.models.get_model.get_resolution_embedding_model", side_effect=_boom
        )
        mocker.patch(
            "tree.memory.consolidation.dream.dedupe_entity",
            return_value=_merged_result("person:twin", self_id="person:fresh"),
        )

        pairs, _stats = await _collect_dream_candidates(
            database=database,
            user_id=_USER_ID,
            last_run_at=_LAST_RUN,
            dedup_config=_CONFIG,
            max_pairs=10000,
        )

        assert len(pairs) == 1
        # The driving node's stored embedding was passed straight to
        # dedupe_entity — never recomputed.
        assert dream_mod.dedupe_entity.call_args.kwargs["embedding"] == [1.0, 0.0]
