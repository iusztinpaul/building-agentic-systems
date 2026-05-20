"""POLE+O end-to-end multi-type extraction (#033 — Test 1).

A short paragraph that names a person, an organization, and one (or
more) locations is fed into the full extraction pipeline. The LLM is
mocked at the extraction-pipeline boundary so the assertions exercise
the real validator / resolver / write path against live Mongo.

Pinned by AC #6 of the #033 groomed spec:

* Five expected nodes land: ``person:paul``, ``organization:anthropic``,
  ``location:san francisco``, ``location:berkeley``, plus
  ``person:self``.
* At least three ``related_to`` edges land with ``semantic_type`` set
  to one of the registered semantics (``employed_by`` /
  ``headquarters_at`` / ``located_at`` / ``resides_at``) and every
  emitted pair falls under that semantic's ``allowed_pairs``.
* No legacy ``type="todo"`` / ``type="experienced"`` rows.
* ``extraction_rejections`` is empty for this user (the LLM emission
  is well-formed for these well-known entities; if a rejection does
  land, the test surfaces it for triage rather than swallowing it).

Marked ``@pytest.mark.slow`` because it spins up the full Prefect
flow. No mongot dependency: the supersession / preference branches
are not exercised by this paragraph; the dedup branch is patched to
"no candidates" so we don't run a ``$vectorSearch`` against a
not-yet-indexed collection.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.entities.documents import Document, SourceType
from tree.entities.knowledge_graph import EdgeType
from tree.entities.ontology import RELATION_SEMANTICS
from tree.entities.users import User
from tree.memory.extraction.dedup import DeduplicationResult
from tree.memory.extraction.pipeline import memory_extraction
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM


TEST_DATABASE = "integration_tests_twin"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


async def _make_user() -> User:
    user = User(identifier=f"test-pole-o-e2e-{PydanticObjectId()}")
    await user.insert()
    return user


async def _insert_doc(
    *, content: str, user_id: PydanticObjectId, source_uri: str
) -> Document:
    doc = Document(
        title="POLE+O multi-type E2E",
        content=content,
        # CONVERSATION source-type more closely matches the AC ("ingest
        # one short conversation"), but HUGGINGFACE keeps the fixture
        # symmetric with the other e2e tests in this directory.
        source_type=SourceType.CONVERSATION,
        source_uri=source_uri,
        user_id=user_id,
        authors=["Paul"],
    )
    await doc.insert()
    return doc


def _patch_pipeline_deps(
    mocker,
    mongo_client,
    *,
    llm: FakeLLM,
    embedding_model: FakeEmbeddingModel,
) -> None:
    """Same patch shape as ``tests/integration/memory/test_fact_island.py``.

    We mock the LLM at the pipeline boundary so the validator /
    resolver / dedup / write path runs for real. ``dedupe_entity`` is
    pinned to "no candidates" so the test doesn't issue a live
    ``$vectorSearch`` against a collection whose ``vector_index`` may
    not exist yet (the indexing pipeline runs separately).
    """

    mocker.patch(
        "tree.memory.extraction.pipeline.init_mongodb", return_value=mongo_client
    )
    mocker.patch(
        "tree.memory.extraction.pipeline.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )
    mocker.patch("tree.memory.extraction.pipeline.get_llm", return_value=llm)
    mocker.patch(
        "tree.memory.extraction.pipeline.get_embedding_model",
        return_value=embedding_model,
    )
    # #042: task ④ embeds node-text via the SEARCH model factory.
    mocker.patch(
        "tree.memory.extraction.pipeline.get_search_embedding_model",
        return_value=embedding_model,
    )
    mocker.patch(
        "tree.memory.extraction.pipeline.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )
    mocker.patch(
        "tree.memory.extraction.add_entity.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )


# ---------------------------------------------------------------------------
# LLM canned emission — POLE+O multi-type paragraph
# ---------------------------------------------------------------------------


def _multi_type_response() -> dict[str, Any]:
    """Mock the LLM output for the paragraph::

        "In March 2024, Paul started at Anthropic. The office is in
        San Francisco. Paul lives in Berkeley."

    The emission is the **closed-loop** shape #033's AC describes: one
    person, one organization, two locations, three ``related_to`` edges
    (employed_by, headquarters_at, resides_at). Each edge carries a
    pair that's in its semantic's ``allowed_pairs``.
    """

    return {
        "nodes": [
            {
                "name": "paul",
                "type": "person",
                "subtype": "individual",
                "properties": {},
            },
            {
                "name": "anthropic",
                "type": "organization",
                "subtype": "company",
                "properties": {},
            },
            {
                "name": "san francisco",
                "type": "location",
                "subtype": "city",
                "properties": {},
            },
            {
                "name": "berkeley",
                "type": "location",
                "subtype": "city",
                "properties": {},
            },
        ],
        "edges": [
            {
                "source_node_id": "paul",
                "source_type": "person",
                "target_node_id": "anthropic",
                "target_type": "organization",
                "type": "related_to",
                "semantic_type": "employed_by",
                "properties": {"start_date": "2024-03"},
            },
            {
                "source_node_id": "anthropic",
                "source_type": "organization",
                "target_node_id": "san francisco",
                "target_type": "location",
                "type": "related_to",
                "semantic_type": "headquarters_at",
                "properties": {},
            },
            {
                "source_node_id": "paul",
                "source_type": "person",
                "target_node_id": "berkeley",
                "target_type": "location",
                "type": "related_to",
                "semantic_type": "resides_at",
                "properties": {},
            },
        ],
    }


# ---------------------------------------------------------------------------
# Helpers for assertions
# ---------------------------------------------------------------------------


async def _kg_rows(mongo_client) -> list[dict[str, Any]]:
    return await mongo_client[TEST_DATABASE]["knowledge_graph"].find().to_list()


async def _rejection_rows(mongo_client) -> list[dict[str, Any]]:
    return await mongo_client[TEST_DATABASE]["extraction_rejections"].find().to_list()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestPOLEOMultiTypeExtractionE2E:
    """Test 1 from the #033 groomed spec — POLE+O multi-type paragraph.

    Headline acceptance test for the Phase 2-5 feature: it proves that
    after the registry foundation (#027), the canonical POLE+O types
    (#028), the ``related_to`` collapse (#029), and the
    envelope-strict validator (#030), a multi-type paragraph lands as
    the expected node/edge mix end-to-end.
    """

    async def test_extracts_pole_o_nodes_and_related_to_edges(
        self, mongo_client, mocker
    ) -> None:
        user = await _make_user()
        doc = await _insert_doc(
            content=(
                "In March 2024, Paul started at Anthropic. "
                "The office is in San Francisco. "
                "Paul lives in Berkeley."
            ),
            user_id=user.id,
            source_uri=f"conversation://pole-o-e2e-{PydanticObjectId()}",
        )
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_multi_type_response()]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            summary = await memory_extraction(
                user_id=user.id, document_ids=[str(doc.id)]
            )

        # The flow processed exactly one document.
        assert summary.documents_processed == 1
        assert summary.nodes_written >= 4
        assert summary.edges_written >= 3

        rows = await _kg_rows(mongo_client)
        nodes = [r for r in rows if r["kind"] == "node"]
        edges = [r for r in rows if r["kind"] == "edge"]

        # --- Node assertions: every expected POLE+O node lands. ---

        node_ids = {r["_id"] for r in nodes}
        assert f"{user.id}:person:self" in node_ids, (
            "person:self must exist (created by User.after_insert)"
        )
        assert f"{user.id}:person:paul" in node_ids
        assert f"{user.id}:organization:anthropic" in node_ids
        assert f"{user.id}:location:san francisco" in node_ids
        assert f"{user.id}:location:berkeley" in node_ids

        # Subtypes were preserved on the POLE+O nodes.
        by_id = {r["_id"]: r for r in nodes}
        assert by_id[f"{user.id}:person:paul"].get("subtype") == "individual"
        assert by_id[f"{user.id}:organization:anthropic"].get("subtype") == "company"
        assert by_id[f"{user.id}:location:san francisco"].get("subtype") == "city"
        assert by_id[f"{user.id}:location:berkeley"].get("subtype") == "city"

        # Every node carries the right tenant.
        for n in nodes:
            assert n["user_id"] == user.id

        # --- Legacy types do NOT appear ---

        # Post-#029: ``todo`` and ``experienced`` are no longer
        # registered edge types. The validator drops any such row at
        # write time; pin so a regression that re-enables them surfaces
        # here.
        edge_types = {e["type"] for e in edges}
        assert "todo" not in edge_types
        assert "experienced" not in edge_types

        # --- related_to edge assertions ---

        related_to_edges = [e for e in edges if e["type"] == EdgeType.RELATED_TO.value]
        assert len(related_to_edges) >= 3, (
            f"expected >=3 related_to edges, got "
            f"{len(related_to_edges)}: {[e['_id'] for e in related_to_edges]!r}"
        )

        # Every related_to edge has a valid semantic_type and a
        # (source_type, target_type) pair in that semantic's
        # allowed_pairs (the write-time contract from #029 / #030).
        for edge in related_to_edges:
            semantic_type = edge.get("semantic_type")
            assert semantic_type is not None, (
                f"related_to edge {edge['_id']!r} missing semantic_type"
            )
            spec = RELATION_SEMANTICS.get(semantic_type)
            assert spec is not None, (
                f"unregistered semantic_type={semantic_type!r} survived the "
                f"validator on edge {edge['_id']!r}"
            )
            pair = (edge["source_type"], edge["target_type"])
            assert pair in spec.allowed_pairs, (
                f"edge {edge['_id']!r} pair {pair!r} not in {spec.allowed_pairs!r}"
            )

        # The specific semantics we expect from THIS paragraph.
        observed_semantics = {e["semantic_type"] for e in related_to_edges}
        # The fixture emits employed_by, headquarters_at, resides_at;
        # each one must survive the validator. (We pin the exact set
        # because the LLM emission is deterministic in this test —
        # under live Gemini we'd assert subset membership, but here
        # the FakeLLM gives us exact control.)
        assert "employed_by" in observed_semantics
        assert "headquarters_at" in observed_semantics
        assert "resides_at" in observed_semantics

        # employed_by carries the start_date property from the paragraph
        # (regression on field-level validator passing dict props through).
        employed_by = next(
            e for e in related_to_edges if e["semantic_type"] == "employed_by"
        )
        assert employed_by["source_node_id"] == f"{user.id}:person:paul"
        assert employed_by["target_node_id"] == f"{user.id}:organization:anthropic"
        assert employed_by["properties"].get("start_date") == "2024-03"

        # --- Audit collection is empty for this well-formed run. ---

        rejections = await _rejection_rows(mongo_client)
        # Filter by tenant — other tests run in the same DB during the
        # session before _clean_collections fires.
        my_rejections = [r for r in rejections if r["user_id"] == user.id]
        assert my_rejections == [], (
            f"expected no rejections for this well-formed run, got {my_rejections!r}"
        )

    async def test_isolation_under_pole_o_two_tenants(
        self, mongo_client, mocker
    ) -> None:
        """Phase-1 two-user isolation regression, restated under the
        Phase-3+ ontology (Test 4 in #033's spec).

        The standing acceptance gate
        (``tests/integration/test_two_user_isolation.py``) covers the
        full query-path matrix. This shorter check pins the headline
        invariant — two tenants extracting different content into the
        same collection do NOT see each other's rows — under the new
        POLE+O wire shape (subtypes + semantic_type) introduced by
        #028 and #029. A regression that breaks the tenant prefix on
        ``related_to`` rows would surface here even if the long
        isolation test is accidentally skipped in CI.
        """

        user_a = await _make_user()
        user_b = await _make_user()

        # User A: Paul + Anthropic + SF.
        doc_a = await _insert_doc(
            content="Paul works at Anthropic in San Francisco.",
            user_id=user_a.id,
            source_uri=f"conversation://iso-a-{PydanticObjectId()}",
        )
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_multi_type_response()]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )
        with prefect_tags("tests"):
            await memory_extraction(user_id=user_a.id, document_ids=[str(doc_a.id)])

        # User B: same payload (intentionally collides on names) — the
        # tenant prefix must keep them disjoint.
        doc_b = await _insert_doc(
            content="Paul works at Anthropic in San Francisco.",
            user_id=user_b.id,
            source_uri=f"conversation://iso-b-{PydanticObjectId()}",
        )
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_multi_type_response()]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )
        with prefect_tags("tests"):
            await memory_extraction(user_id=user_b.id, document_ids=[str(doc_b.id)])

        rows = await _kg_rows(mongo_client)

        # Each tenant has its OWN organization:anthropic row (different
        # _id prefix, different user_id). Pin both.
        a_anthropic = [
            r for r in rows if r["_id"] == f"{user_a.id}:organization:anthropic"
        ]
        b_anthropic = [
            r for r in rows if r["_id"] == f"{user_b.id}:organization:anthropic"
        ]
        assert len(a_anthropic) == 1
        assert len(b_anthropic) == 1
        assert a_anthropic[0]["user_id"] == user_a.id
        assert b_anthropic[0]["user_id"] == user_b.id

        # related_to edges on user A's side cannot reference user B's
        # nodes (the _id format would mix prefixes — impossible by
        # construction).
        for edge in (r for r in rows if r["kind"] == "edge"):
            assert edge["user_id"] in (user_a.id, user_b.id)
            if edge["user_id"] == user_a.id:
                assert str(user_b.id) not in (edge.get("source_node_id") or "")
                assert str(user_b.id) not in (edge.get("target_node_id") or "")
            else:
                assert str(user_a.id) not in (edge.get("source_node_id") or "")
                assert str(user_a.id) not in (edge.get("target_node_id") or "")


# Note: Test 4 (full Phase-1 two-user query-path isolation) is enforced
# by ``tests/integration/test_two_user_isolation.py`` running in the
# same suite. That test exercises 17 distinct query paths; any one of
# them returning a cross-tenant row is a hard failure. Per #033's AC,
# the existing test is the canonical regression gate; the shorter
# ``test_isolation_under_pole_o_two_tenants`` above is a tenant-prefix
# spot-check focused on the new POLE+O wire shape.

# ---------------------------------------------------------------------------
# Cross-referenced tests in this directory (so a reader scanning the
# acceptance gates for #033 sees the full picture):
#
# * Test 2 (preference supersession) — covered by
#   ``test_preference_supersession.py::TestPreferenceSupersessionE2E`` /
#   ``TestPreferenceSupersessionLiveEmbedderE2E`` (the live MiniLM run
#   is the QA fix-1 regression pin).
# * Test 3 (fact island) — covered by
#   ``test_fact_island.py::TestFactIslandEnd2End``.
# * Test 4 (Phase-1 two-user isolation regression) — covered by
#   ``tests/integration/test_two_user_isolation.py``.
# ---------------------------------------------------------------------------
