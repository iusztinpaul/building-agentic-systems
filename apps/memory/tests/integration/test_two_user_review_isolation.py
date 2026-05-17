"""Phase-1 acceptance gate (review surface): two-user isolation for SAME_AS review.

Seeds two tenants (User A and User B), each with a pair of duplicate
``person`` nodes joined by a PENDING ``SAME_AS`` edge. Then exercises
every review-surface function under User A's scope and asserts that
**no row owned by User B leaks through**.

Covered review-surface paths (all three exported by
:mod:`tree.memory.review.core`):

1. ``find_pending_duplicates(database, user_id=A)`` — must return ONLY
   A's pending pair. B's edge id and B's node names must be invisible.
2. ``review_duplicate(database, user_id=A, source=P_b1, target=P_b2,
   decision=CONFIRM, ...)`` — must raise :class:`ValueError`
   ``"no SAME_AS edge between ..."`` because the SAME_AS audit edge
   belongs to User B and is invisible under A's tenant scope.
3. ``get_same_as_cluster(database, P_b1, user_id=A)`` — must return
   ``{P_b1}`` only (no B-tenant neighbors), since the single-hop
   traversal is filtered by ``user_id``.

Why this test exists
--------------------

The unit-pinning tests in ``tests/unit/mcp/test_tools_user_id_pinning.py``
only assert the MCP tool layer forwards ``user_id`` as a keyword arg —
they mock the underlying business functions, so they never exercise the
DB filter. The integration tests in ``tests/integration/memory/test_review.py``
seed a single tenant, so a missing ``user_id`` predicate on the
``$match`` / ``$lookup`` stages is silently invisible.

This file closes that gap. It is the **only** test in the suite that
exercises the cross-tenant invariant on the review surface end-to-end
against MongoDB. Removing any of the three ``user_id`` filters from
``find_pending_duplicates`` (top-level ``$match`` or either
``$lookup``-pipeline ``$match``) makes this test fail with a clear
cross-tenant assertion message. See "Planted-leak procedure" below.

Planted-leak procedure (DOCUMENTED; NOT run by pytest)
------------------------------------------------------

Per the task spec the SWE must demonstrate that this test is
**exercising the contract**, not passing vacuously. Procedure:

1. In ``apps/memory/src/tree/memory/review/core.py``, inside
   ``find_pending_duplicates``, remove the ``"user_id": user_id`` key
   from the top-level ``$match`` stage AND remove the ``"user_id":
   user_id`` key from one of the two ``$lookup`` ``pipeline`` /
   ``$match`` stages (e.g. the ``_source_node`` lookup).
2. Re-run::

       uv run pytest tests/integration/test_two_user_review_isolation.py -v

   The test method
   ``test_find_pending_duplicates_returns_only_user_a_pair``
   must FAIL with an assertion message of the form
   ``LEAK — User-B pair surfaced in User-A find_pending_duplicates``.
3. Revert the change. Re-run. The test must PASS.

The actual FAIL / PASS outputs from this procedure are captured in the
SWE log entry of #023 ("Fix-up after Tester FAIL #1").
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from beanie import PydanticObjectId

from tree.entities.knowledge_graph import (
    EdgeType,
    NodeType,
    build_edge_id,
    build_node_id,
)
from tree.memory.review import (
    MergeStrategy,
    ReviewDecision,
    find_pending_duplicates,
    get_same_as_cluster,
    review_duplicate,
)

from tests.integration.conftest import TEST_DATABASE


# Two stable tenant ids. Real ``User`` rows are not required for the
# review surface — the contract is "rows are scoped by ``user_id``", so
# we just need two distinct ``PydanticObjectId`` values to scope writes
# / reads. Using stable hex strings makes failure messages legible.
_USER_A_ID = PydanticObjectId("0000000000000000000000aa")
_USER_B_ID = PydanticObjectId("0000000000000000000000bb")

_NOW = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Per-tenant seed helpers
# ---------------------------------------------------------------------------


def _person_node(
    *,
    user_id: PydanticObjectId,
    name: str,
    created_at: datetime = _NOW,
) -> dict[str, Any]:
    """Build a minimal ``person`` node row for ``user_id`` named ``name``.

    The ``_id`` carries the canonical ``"{user_id}:{type}:{name}"``
    prefix (per Phase-1 decision #1), so cross-tenant id collisions are
    impossible by construction.
    """

    node_id = build_node_id(user_id, NodeType.PERSON, name)
    return {
        "_id": node_id,
        "user_id": user_id,
        "kind": "node",
        "type": NodeType.PERSON.value,
        "name": name,
        "canonical_name": name,
        "aliases": [],
        "properties": {},
        "sources": [],
        "confidence": 1.0,
        "embedding": [],
        "merged_into": None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def _pending_same_as_edge(
    *,
    user_id: PydanticObjectId,
    source_id: str,
    target_id: str,
    confidence: float = 0.92,
) -> dict[str, Any]:
    """Build a PENDING ``SAME_AS`` audit edge between two nodes."""

    edge_id = build_edge_id(source_id, EdgeType.SAME_AS, target_id)
    return {
        "_id": edge_id,
        "user_id": user_id,
        "kind": "edge",
        "type": EdgeType.SAME_AS.value,
        "source_node_id": source_id,
        "source_type": NodeType.PERSON.value,
        "target_node_id": target_id,
        "target_type": NodeType.PERSON.value,
        "sources": [],
        "properties": {
            "status": "pending",
            "confidence": confidence,
            "match_type": "embedding",
            "created_at": _NOW,
        },
        "created_at": _NOW,
        "updated_at": _NOW,
    }


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestTwoUserReviewIsolation:
    """Cross-tenant isolation for the human-review SAME_AS surface."""

    @pytest.fixture(autouse=True)
    async def _seed(self, mongo_client) -> None:
        """Seed two users, each with a pending SAME_AS duplicate pair.

        Per-tenant layout::

            User A: person:Paul  <-- SAME_AS pending -->  person:P. Iusztin
            User B: person:Paul  <-- SAME_AS pending -->  person:P. Iusztin

        The names are deliberately identical across tenants so the
        ``_id`` prefix is the only thing that distinguishes them. A
        missing ``user_id`` predicate would surface BOTH pairs.
        """

        db = mongo_client[TEST_DATABASE]
        self.database = db
        self.collection = db["knowledge_graph"]

        # Cache ids so tests can use them directly.
        self.a_p1 = build_node_id(_USER_A_ID, NodeType.PERSON, "Paul")
        self.a_p2 = build_node_id(_USER_A_ID, NodeType.PERSON, "P. Iusztin")
        self.b_p1 = build_node_id(_USER_B_ID, NodeType.PERSON, "Paul")
        self.b_p2 = build_node_id(_USER_B_ID, NodeType.PERSON, "P. Iusztin")

        self.a_edge_id = build_edge_id(self.a_p1, EdgeType.SAME_AS, self.a_p2)
        self.b_edge_id = build_edge_id(self.b_p1, EdgeType.SAME_AS, self.b_p2)

        await self.collection.insert_many(
            [
                # --- User A ---
                _person_node(user_id=_USER_A_ID, name="Paul"),
                _person_node(user_id=_USER_A_ID, name="P. Iusztin"),
                _pending_same_as_edge(
                    user_id=_USER_A_ID,
                    source_id=self.a_p1,
                    target_id=self.a_p2,
                ),
                # --- User B ---
                _person_node(user_id=_USER_B_ID, name="Paul"),
                _person_node(user_id=_USER_B_ID, name="P. Iusztin"),
                _pending_same_as_edge(
                    user_id=_USER_B_ID,
                    source_id=self.b_p1,
                    target_id=self.b_p2,
                ),
            ]
        )

        yield

        # Drop the whole collection — the autouse cleanup fixture in
        # ``conftest.py`` only walks the Beanie models, so the raw
        # ``knowledge_graph`` rows we inserted need explicit teardown.
        await db.drop_collection("knowledge_graph")

    # ------------------------------------------------------------------
    # Surface 1 — find_pending_duplicates
    # ------------------------------------------------------------------

    async def test_find_pending_duplicates_returns_only_user_a_pair(self) -> None:
        """A's ``find_pending_duplicates`` must surface ONLY A's pair.

        With the contract intact, the result is exactly one
        :class:`PendingDuplicate` whose ``edge_id`` equals A's
        ``SAME_AS`` audit edge id. B's edge id and B's node ids must be
        absent.

        Removing the ``user_id`` predicate from the top-level ``$match``
        (or either ``$lookup`` pipeline) makes the cross-tenant pair
        leak in here.
        """

        # Act
        pairs = await find_pending_duplicates(
            self.database,
            user_id=_USER_A_ID,
            limit=50,
        )

        # Assert — exactly one pair, and it's A's.
        assert len(pairs) == 1, (
            f"LEAK — User-A find_pending_duplicates returned {len(pairs)} pairs; "
            f"expected exactly 1. Pairs: {pairs}"
        )

        only = pairs[0]
        assert only.edge_id == self.a_edge_id, (
            f"LEAK — User-B pair surfaced in User-A find_pending_duplicates: "
            f"got edge_id={only.edge_id!r}, expected A's edge_id={self.a_edge_id!r}"
        )

        # Defensive: B's tenant-scoped ids must not appear anywhere.
        b_ids = {self.b_p1, self.b_p2, self.b_edge_id}
        surfaced_ids = {
            only.edge_id,
            only.source_node_id,
            only.target_node_id,
        }
        leaked = surfaced_ids & b_ids
        assert not leaked, (
            f"LEAK — User-B ids surfaced in User-A find_pending_duplicates: {leaked}"
        )

        # And the ``user_id`` prefix on every returned id must be A's.
        a_prefix = f"{_USER_A_ID}:"
        for surfaced in (only.source_node_id, only.target_node_id):
            assert surfaced.startswith(a_prefix), (
                f"LEAK — surfaced node id {surfaced!r} is not under User A's "
                f"tenant prefix {a_prefix!r}"
            )

    # ------------------------------------------------------------------
    # Surface 2 — review_duplicate (CONFIRM against B's pair under A)
    # ------------------------------------------------------------------

    async def test_review_duplicate_cannot_confirm_other_tenants_pair(self) -> None:
        """A invoking CONFIRM on B's pair must raise ``ValueError``.

        The SAME_AS audit edge between ``b_p1`` and ``b_p2`` belongs to
        User B; under User A's tenant scope it is invisible. The
        ``find_one`` inside ``review_duplicate`` returns ``None`` and
        the function raises with ``"no SAME_AS edge between ..."``.

        A missing ``user_id`` predicate on the lookup would let A mutate
        B's edge — a hard cross-tenant leak.
        """

        with pytest.raises(ValueError) as exc:
            await review_duplicate(
                self.database,
                user_id=_USER_A_ID,
                source_node_id=self.b_p1,
                target_node_id=self.b_p2,
                decision=ReviewDecision.CONFIRM,
                reviewed_by="user-a-attacker",
                merge_strategy=MergeStrategy.KEEP_PRIMARY,
            )

        msg = str(exc.value)
        assert "no SAME_AS edge" in msg, (
            f"Expected ValueError with 'no SAME_AS edge between ...'; got: {msg!r}"
        )

        # And — critically — B's SAME_AS edge must still be PENDING.
        # If A leaked through and mutated B's edge, status would now
        # be 'confirmed'. This is the data-side proof of isolation.
        b_edge = await self.collection.find_one(
            {"_id": self.b_edge_id, "user_id": _USER_B_ID}
        )
        assert b_edge is not None, (
            "B's SAME_AS audit edge vanished — seed or teardown is broken"
        )
        assert b_edge["properties"]["status"] == "pending", (
            f"LEAK — User A's CONFIRM mutated User B's SAME_AS edge: "
            f"status={b_edge['properties']['status']!r}, expected 'pending'"
        )

        # And neither of B's person nodes should have been tombstoned.
        for b_node_id in (self.b_p1, self.b_p2):
            b_node = await self.collection.find_one(
                {"_id": b_node_id, "user_id": _USER_B_ID}
            )
            assert b_node is not None, f"User B node {b_node_id!r} vanished"
            assert b_node.get("merged_into") is None, (
                f"LEAK — User B node {b_node_id!r} was tombstoned by User A: "
                f"merged_into={b_node['merged_into']!r}"
            )

    # ------------------------------------------------------------------
    # Surface 3 — get_same_as_cluster
    # ------------------------------------------------------------------

    async def test_get_same_as_cluster_does_not_traverse_other_tenant(self) -> None:
        """``get_same_as_cluster(P_b1, user_id=A)`` must return ``{P_b1}`` only.

        Under A's tenant there is no SAME_AS edge incident to
        ``b_p1`` — that edge belongs to B. The cluster must contain only
        the seed (callers rely on the seed always being in the returned
        set, even when no edges are visible).

        A missing ``user_id`` predicate on the ``find(...)`` would let
        A's traversal see B's SAME_AS edge and surface ``b_p2`` as a
        neighbor — a hard cross-tenant leak.
        """

        # Act
        cluster = await get_same_as_cluster(
            self.database,
            self.b_p1,
            user_id=_USER_A_ID,
        )

        # Assert — only the seed (no B neighbors visible under A).
        assert cluster == {self.b_p1}, (
            f"LEAK — User-A cluster traversal of {self.b_p1!r} surfaced "
            f"User-B neighbors: {cluster - {self.b_p1}}"
        )

        # Sanity: under B's own scope, the cluster DOES include the
        # neighbor (``b_p2``). This proves the seed data is valid and
        # the empty result under A is isolation, not a missing edge.
        cluster_under_b = await get_same_as_cluster(
            self.database,
            self.b_p1,
            user_id=_USER_B_ID,
        )
        assert cluster_under_b == {self.b_p1, self.b_p2}, (
            f"Seed sanity broken — User-B cluster of {self.b_p1!r} should be "
            f"{{b_p1, b_p2}}, got {cluster_under_b}"
        )
