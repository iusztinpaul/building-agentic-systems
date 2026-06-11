"""Phase-1 acceptance gate: two-user isolation across every query path.

Seeds two users (A, B), ingests distinct content for each, runs the full
extraction + indexing pipeline for each, then exercises **every** query
path documented in ``plan.md`` Phase 1 against User A's tenant and
asserts zero rows belonging to User B leak through.

Covered query paths:

1. ``KGQuery.find_nodes(type=...)`` — find by type.
2. ``KGQuery.find_nodes(name=...)`` — find by name.
3. ``KGQuery.find_node_by_id(...)`` — find by id (rejects cross-tenant ids).
4. ``KGQuery.find_self_person()`` — self-person lookup.
5. ``KGQuery.find_edges(...)`` — edge reads.
6. ``KGQuery.find_neighbors(...)`` — neighbor expansion.
7. ``tree.memory.query.core.search_nodes`` — text + vector hybrid (RRF).
8. ``tree.memory.query.core._text_search`` — text-only path.
9. ``tree.memory.query.core._vector_search`` — vector-only path.
10. ``tree.memory.query.core.expand_graph`` — graph traversal.
11. ``tree.memory.query.core.query_memory`` — end-to-end orchestrator.
12. ``tree.memory.query.nl_query.execute_nl_query`` — MCP NL-to-pipeline.
13. MCP ``query_memory`` tool — user-facing surface.
14. MCP ``search_memory`` tool — user-facing surface.
15. ``tree.memory.query.nl_query.execute_nl_query`` with an LLM-emitted
    ``$lookup`` stage — must be rejected by ``validate_pipeline`` (the
    stage is not in ``_ALLOWED_STAGES``) so no joined cross-tenant payload
    can surface. See ``test_nl_query_lookup_stage_is_rejected_no_b_leak``.
16. ``tree.memory.query.nl_query.execute_nl_query`` with an LLM-emitted
    ``$facet`` stage wrapping a ``$lookup`` (the adversarial shape that
    bypassed the cycle-1 fix) — must be rejected by ``validate_pipeline``
    because ``$facet`` carries sub-pipelines that ``_inject_user_id`` does
    not walk. Same logic for ``$unionWith``. See
    ``test_nl_query_facet_wrapped_lookup_is_rejected_no_b_leak``.
17. ``tree.memory.query.nl_query.execute_nl_query`` with an LLM-emitted
    pipeline whose FIRST stage is not ``$match`` or ``$vectorSearch``
    (e.g. ``$group``, ``$sample``, ``$sort``, ``$project``, ``$unwind``,
    ``$bucket``, ``$count``). The Tester demonstrated in cycle 2 that
    such pipelines would run their first stage against the unfiltered
    collection because the old ``_inject_user_id`` only modified
    existing stages. ``validate_pipeline`` now prepends a tenant
    ``{"$match": {"user_id": ...}}`` as the leading stage in those
    cases. See ``test_nl_query_first_stage_without_match_does_not_leak``.

Adding a new query path obligates the author to extend this test.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.entities.knowledge_graph import EdgeType, KnowledgeGraphEntry, NodeType
from tree.memory.extraction.dedup import DeduplicationResult
from tree.memory.extraction.pipeline import memory_extract_etl_worker
from tree.memory.indexing.pipeline import memory_indexing
from tree.memory.query.core import (
    _text_search,
    _vector_search,
    expand_graph,
    query_memory as structured_query_memory,
    search_nodes,
)
from tree.memory.query.kgquery import KGQuery
from tree.memory.query.nl_query import execute_nl_query
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM

from tests.integration.conftest import TEST_DATABASE, TwoUserContent


# ---------------------------------------------------------------------------
# Constants — LLM responses + embedding helpers
# ---------------------------------------------------------------------------


_DIMS = 8


def _user_a_unit_vector() -> list[float]:
    """A's canonical query vector — first axis."""
    vec = [0.0] * _DIMS
    vec[0] = 1.0
    return vec


def _user_b_unit_vector() -> list[float]:
    """B's canonical query vector — second axis (orthogonal to A)."""
    vec = [0.0] * _DIMS
    vec[1] = 1.0
    return vec


class _DirectedEmbeddingModel(FakeEmbeddingModel):
    """Deterministic embedding model that returns a per-text vector.

    Picks one of two orthogonal unit vectors depending on whether the
    text contains a User-A token or a User-B token. This gives the
    ``$vectorSearch`` aggregation real cosine-similarity discrimination
    so a cross-tenant filter omission would surface as a fake "hit"
    (B's rows would rank highly under A's query vector if filtering is
    bypassed).
    """

    _A_TOKENS = ("antelope", "amber", "alice")
    _B_TOKENS = ("badger", "bramble", "bob")

    def __init__(self, *, dimensions: int = _DIMS) -> None:
        super().__init__(dimensions=dimensions)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            if any(tok in lowered for tok in self._A_TOKENS):
                out.append(_user_a_unit_vector())
            elif any(tok in lowered for tok in self._B_TOKENS):
                out.append(_user_b_unit_vector())
            else:
                # Neutral vector — third axis. The tests don't rely on
                # the absolute score, just on the relative ordering.
                vec = [0.0] * self._dimensions
                vec[2] = 1.0
                out.append(vec)
        return out


_LLM_RESPONSE_A: dict[str, Any] = {
    "nodes": [
        {
            "name": "alice",
            "type": "person",
            "subtype": "individual",
            "properties": {"aliases": []},
        },
        {
            "name": "antelope analytics project",
            "type": "task",
            "subtype": "task",
            "properties": {
                "content": "Own the antelope analytics project (amber dashboard)."
            },
        },
    ],
    "edges": [
        {
            "source_node_id": "alice",
            "source_type": "person",
            "target_node_id": "antelope analytics project",
            "target_type": "task",
            "type": "todo",
            "properties": {},
        }
    ],
}


_LLM_RESPONSE_B: dict[str, Any] = {
    "nodes": [
        {
            "name": "bob",
            "type": "person",
            "subtype": "individual",
            "properties": {"aliases": []},
        },
        {
            "name": "badger reporting service",
            "type": "task",
            "subtype": "task",
            "properties": {
                "content": "Own the badger reporting service (bramble migration)."
            },
        },
    ],
    "edges": [
        {
            "source_node_id": "bob",
            "source_type": "person",
            "target_node_id": "badger reporting service",
            "target_type": "task",
            "type": "todo",
            "properties": {},
        }
    ],
}


# ---------------------------------------------------------------------------
# Pipeline wiring helpers
# ---------------------------------------------------------------------------


def _patch_extraction_deps(
    mocker, mongo_client, *, llm: FakeLLM, embedding_model: FakeEmbeddingModel
) -> None:
    """Swap heavy deps in the extraction pipeline (LLM, embedding, DB)."""

    mocker.patch(
        "tree.memory.extraction.pipeline.init_mongodb",
        return_value=mongo_client,
    )
    mocker.patch(
        "tree.memory.extraction.pipeline.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )
    mocker.patch("tree.memory.extraction.pipeline.get_llm", return_value=llm)
    # #043: the resolver builds from the RESOLUTION model factory.
    mocker.patch(
        "tree.memory.extraction.pipeline.get_resolution_embedding_model",
        return_value=embedding_model,
    )
    # #042: task ④ embeds node-text via the SEARCH model factory.
    mocker.patch(
        "tree.memory.extraction.pipeline.get_search_embedding_model",
        return_value=embedding_model,
    )
    # Skip the live $vectorSearch dedup call so extraction doesn't depend
    # on the vector index being live before indexing runs. The default
    # "none" decision matches the empty-graph starting state both users
    # see when they run the pipeline for the first time.
    mocker.patch(
        "tree.memory.extraction.pipeline.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )
    mocker.patch(
        "tree.memory.extraction.add_entity.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )


def _patch_indexing_deps(mocker, mongo_client, embedding_model) -> None:
    """Point indexing at the test DB and fake embedding model.

    The boot-time ``assert_settings_match_live_vector_index`` check is
    stubbed because the test runs with an 8-dim fake model while
    ``app_config.models.search_embedding.dimensions`` is the production pin (1024).
    """

    mocker.patch(
        "tree.memory.indexing.pipeline.init_mongodb",
        return_value=mongo_client,
    )
    mocker.patch(
        "tree.memory.indexing.pipeline.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )
    mocker.patch(
        "tree.memory.indexing.pipeline.get_embedding_model",
        return_value=embedding_model,
    )
    mocker.patch(
        "tree.memory.indexing.pipeline.assert_settings_match_live_vector_index",
        new=AsyncMock(),
    )


async def _wait_for_vector_index_ready(
    mongo_client,
    *,
    expected_count: int,
    user_id: PydanticObjectId,
    timeout: float = 60.0,
) -> None:
    """Poll ``$vectorSearch`` until it returns ``expected_count`` rows for ``user_id``.

    Atlas Search is eventually consistent. Without this poll, the first
    query after a fresh ``ensure_indexes`` run may return zero hits even
    though every row was written.
    """

    import asyncio

    collection = mongo_client[TEST_DATABASE]["knowledge_graph"]
    probe = _user_a_unit_vector()
    deadline = asyncio.get_running_loop().time() + timeout
    last = 0
    while asyncio.get_running_loop().time() < deadline:
        cursor = await collection.aggregate(
            [
                {
                    "$vectorSearch": {
                        "index": "vector_index",
                        "path": "embedding",
                        "queryVector": probe,
                        "numCandidates": 200,
                        "limit": 200,
                        "filter": {"user_id": user_id, "kind": "node"},
                    }
                },
                {"$count": "n"},
            ]
        )
        rows = [r async for r in cursor]
        last = rows[0].get("n", 0) if rows else 0
        if last >= expected_count:
            return
        await asyncio.sleep(1.0)
    raise RuntimeError(
        f"vector_index never returned {expected_count} rows for user_id={user_id} "
        f"within {timeout}s (last count: {last})."
    )


# ---------------------------------------------------------------------------
# The acceptance test
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.requires_mongot
@pytest.mark.usefixtures("_skip_without_mongot")
class TestTwoUserIsolation:
    """Phase-1 acceptance gate: every query path is tenant-locked.

    See module docstring for the full path enumeration. Each test method
    exercises ONE path so a failure pin-points the leaking surface.
    """

    @pytest.fixture(autouse=True)
    async def _setup(
        self, mongo_client, mocker, two_users_with_content: TwoUserContent
    ) -> None:
        """Run extraction + indexing for both users, then wait for mongot."""

        self.mongo_client = mongo_client
        self.content = two_users_with_content
        self.user_a = two_users_with_content.user_a
        self.user_b = two_users_with_content.user_b
        self.embedding_model = _DirectedEmbeddingModel()

        # --- Extract for A then B (FakeLLM state carries; re-patch each run) ---
        _patch_extraction_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_LLM_RESPONSE_A]),
            embedding_model=self.embedding_model,
        )
        with prefect_tags("tests"):
            await memory_extract_etl_worker(
                user_id=self.user_a.id,
                document_ids=[str(self.content.doc_a.id)],
            )

        _patch_extraction_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_LLM_RESPONSE_B]),
            embedding_model=self.embedding_model,
        )
        with prefect_tags("tests"):
            await memory_extract_etl_worker(
                user_id=self.user_b.id,
                document_ids=[str(self.content.doc_b.id)],
            )

        # --- Index (per-user, like the real one-user-at-a-time deployment) ---
        _patch_indexing_deps(mocker, mongo_client, self.embedding_model)
        with prefect_tags("tests"):
            await memory_indexing(user_id=self.user_a.id)
        with prefect_tags("tests"):
            await memory_indexing(user_id=self.user_b.id)

        # --- Wait for $vectorSearch to surface each user's rows ---
        # The extraction pipeline writes 4 nodes per user (document,
        # chunk, person, task). Plus the self-person node from
        # ``User.after_insert``. The self-person node has a near-empty
        # ``properties`` blob, so its embedding text is sparse and
        # mongot's HNSW may or may not surface it under an orthogonal
        # query vector — we wait on ``count - 1`` to absorb that edge
        # case. The cross-tenant isolation invariant does NOT depend on
        # every node being discoverable via $vectorSearch; it depends on
        # no node from the OTHER tenant ever surfacing.
        kg = mongo_client[TEST_DATABASE]["knowledge_graph"]
        count_a = await kg.count_documents({"user_id": self.user_a.id, "kind": "node"})
        count_b = await kg.count_documents({"user_id": self.user_b.id, "kind": "node"})

        # Sanity: each user actually has data — otherwise the rest of the
        # assertions degenerate to "empty == empty".
        assert count_a > 0, "User A has no nodes; extraction is broken."
        assert count_b > 0, "User B has no nodes; extraction is broken."

        await _wait_for_vector_index_ready(
            mongo_client,
            expected_count=max(1, count_a - 1),
            user_id=self.user_a.id,
        )
        await _wait_for_vector_index_ready(
            mongo_client,
            expected_count=max(1, count_b - 1),
            user_id=self.user_b.id,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _assert_no_b_rows(self, rows: list[dict | KnowledgeGraphEntry]) -> None:
        """Every row in ``rows`` must belong to User A.

        Walks the row recursively so faceted / grouped / unwound shapes
        without a top-level ``user_id`` field are also inspected.
        Concretely, a ``$facet`` output looks like ``{"leaked": [...]}``
        with no top-level ``user_id`` — the previous KeyError-on-missing
        implementation hid leaks under an opaque traceback. The recursive
        walk:

        - For ``KnowledgeGraphEntry`` instances, asserts ``user_id`` is A.
        - For dict rows, if ``user_id`` is present, asserts it is A. Then
          recurses into every dict / list value to catch nested rows
          (faceted output, grouped ``items`` arrays, ``$graphLookup``'s
          ``connected`` arrays, etc).
        """

        def _walk(value: Any) -> None:
            if isinstance(value, KnowledgeGraphEntry):
                assert value.user_id == self.user_a.id, (
                    f"LEAK — KnowledgeGraphEntry from user_id={value.user_id} "
                    f"surfaced in a User-A query: {value}"
                )
                return
            if isinstance(value, dict):
                if "user_id" in value:
                    uid = value["user_id"]
                    assert uid == self.user_a.id, (
                        f"LEAK — row from user_id={uid} surfaced in a "
                        f"User-A query: {value}"
                    )
                for v in value.values():
                    _walk(v)
                return
            if isinstance(value, list):
                for item in value:
                    _walk(item)
                return

        for row in rows:
            _walk(row)

    def _assert_no_b_tokens(self, rows: list[dict | KnowledgeGraphEntry]) -> None:
        """B's distinctive tokens must not appear anywhere in any row.

        Serialises each row (recursively, via ``json.dumps``) and greps
        for B's distinctive secrets. This catches leaks even when the
        leaking row sits inside a faceted/grouped/unwound nested array
        with no top-level ``user_id`` field — the previous
        ``KeyError: 'user_id'`` hid such leaks behind an opaque traceback.
        """

        b_tokens = ("badger", "bramble", "bob", "secret_b")
        for row in rows:
            if isinstance(row, KnowledgeGraphEntry):
                blob = json.dumps(row.model_dump(), default=str).lower()
            else:
                blob = json.dumps(row, default=str).lower()
            for tok in b_tokens:
                assert tok not in blob, (
                    f"LEAK — token {tok!r} from User B appears in a "
                    f"User-A query result: {row}"
                )

    # ------------------------------------------------------------------
    # Query path 1 — KGQuery.find_nodes(type=...)
    # ------------------------------------------------------------------

    async def test_kgquery_find_nodes_by_type_returns_only_user_a(self) -> None:
        kg_a = KGQuery(self.user_a.id)

        chunks = await kg_a.find_nodes(type=NodeType.CHUNK)

        assert chunks, "Expected at least one CHUNK for user A."
        self._assert_no_b_rows(chunks)
        self._assert_no_b_tokens(chunks)

    # ------------------------------------------------------------------
    # Query path 2 — KGQuery.find_nodes(name=...)
    # ------------------------------------------------------------------

    async def test_kgquery_find_nodes_by_name_excludes_user_b(self) -> None:
        kg_a = KGQuery(self.user_a.id)

        # ``bob`` exists in user B's KG. Searching for it under user A's
        # scope must return nothing — there is no Bob in A's tenant.
        bob_under_a = await kg_a.find_nodes(name="bob")

        assert bob_under_a == []

        # Sanity: under B's scope ``bob`` is present.
        kg_b = KGQuery(self.user_b.id)
        bob_under_b = await kg_b.find_nodes(name="bob")
        assert bob_under_b, "Sanity check: 'bob' should exist in User B's KG."

    # ------------------------------------------------------------------
    # Query path 3 — KGQuery.find_node_by_id (cross-tenant id rejection)
    # ------------------------------------------------------------------

    async def test_kgquery_find_node_by_id_rejects_cross_tenant_id(self) -> None:
        kg_a = KGQuery(self.user_a.id)
        # Bob's id under user B's tenant.
        bob_under_b_id = f"{self.user_b.id}:person:bob"

        result = await kg_a.find_node_by_id(bob_under_b_id)

        assert result is None, "find_node_by_id leaked a cross-tenant id."

    # ------------------------------------------------------------------
    # Query path 4 — KGQuery.find_self_person
    # ------------------------------------------------------------------

    async def test_kgquery_find_self_person_returns_only_a_self(self) -> None:
        kg_a = KGQuery(self.user_a.id)

        self_a = await kg_a.find_self_person()

        assert self_a is not None
        assert self_a.user_id == self.user_a.id
        assert self_a.id == f"{self.user_a.id}:person:self"

    # ------------------------------------------------------------------
    # Query path 5 — KGQuery.find_edges
    # ------------------------------------------------------------------

    async def test_kgquery_find_edges_returns_only_user_a_edges(self) -> None:
        # Post-#029 legacy ``todo`` LLM emissions re-route to
        # ``related_to + semantic_type='has_task'``. Query by the
        # umbrella edge type.
        kg_a = KGQuery(self.user_a.id)

        related_a = await kg_a.find_edges(type=EdgeType.RELATED_TO)

        assert related_a, "Expected at least one related_to edge for user A."
        self._assert_no_b_rows(related_a)
        self._assert_no_b_tokens(related_a)

    # ------------------------------------------------------------------
    # Query path 6 — KGQuery.find_neighbors
    # ------------------------------------------------------------------

    async def test_kgquery_find_neighbors_does_not_cross_tenant(self) -> None:
        kg_a = KGQuery(self.user_a.id)
        a_self_id = f"{self.user_a.id}:person:self"

        edges = await kg_a.find_neighbors(a_self_id, max_hops=2)

        # Even an empty neighbor list is fine for self — the constraint is
        # "no cross-tenant edges", not "must be non-empty".
        self._assert_no_b_rows(edges)
        self._assert_no_b_tokens(edges)

    # ------------------------------------------------------------------
    # Query path 7 — search_nodes (text + vector, RRF fused)
    # ------------------------------------------------------------------

    async def test_search_nodes_with_b_token_returns_no_a_rows(self) -> None:
        """Searching for ``badger`` (B's token) under A's scope returns nothing."""

        results = await search_nodes(
            self.mongo_client,
            TEST_DATABASE,
            "badger reporting service",
            self.embedding_model,
            self.user_a.id,
            top_k=20,
        )

        # Every row that DOES come back must belong to A — and ideally
        # there are zero because B's distinctive token has no semantic
        # neighbor in A's tenant.
        self._assert_no_b_rows(results)
        self._assert_no_b_tokens(results)

    async def test_search_nodes_with_a_token_returns_only_a_rows(self) -> None:
        results = await search_nodes(
            self.mongo_client,
            TEST_DATABASE,
            "antelope analytics",
            self.embedding_model,
            self.user_a.id,
            top_k=20,
        )

        assert results, "Expected at least one hit for User-A query."
        self._assert_no_b_rows(results)
        self._assert_no_b_tokens(results)

    # ------------------------------------------------------------------
    # Query path 8 — text-only path
    # ------------------------------------------------------------------

    async def test_text_search_only_does_not_leak_b_rows(self) -> None:
        col = self.mongo_client[TEST_DATABASE]["knowledge_graph"]

        # A-token query under A's scope.
        a_results = await _text_search(
            col, "antelope", user_id=self.user_a.id, limit=20
        )
        self._assert_no_b_rows(a_results)
        self._assert_no_b_tokens(a_results)

        # B-token query under A's scope — must return nothing.
        b_results_for_a = await _text_search(
            col, "badger", user_id=self.user_a.id, limit=20
        )
        self._assert_no_b_rows(b_results_for_a)
        self._assert_no_b_tokens(b_results_for_a)

    # ------------------------------------------------------------------
    # Query path 9 — vector-only path
    # ------------------------------------------------------------------

    async def test_vector_search_only_does_not_leak_b_rows(self) -> None:
        col = self.mongo_client[TEST_DATABASE]["knowledge_graph"]

        a_results = await _vector_search(
            col,
            "antelope amber alice",
            self.embedding_model,
            user_id=self.user_a.id,
            limit=20,
        )
        self._assert_no_b_rows(a_results)
        self._assert_no_b_tokens(a_results)

        # Even firing a B-aligned query vector at A's tenant must not
        # surface B rows.
        b_results_for_a = await _vector_search(
            col,
            "badger bramble bob",
            self.embedding_model,
            user_id=self.user_a.id,
            limit=20,
        )
        self._assert_no_b_rows(b_results_for_a)
        self._assert_no_b_tokens(b_results_for_a)

    # ------------------------------------------------------------------
    # Query path 10 — expand_graph
    # ------------------------------------------------------------------

    async def test_expand_graph_does_not_traverse_into_b_tenant(self) -> None:
        a_self_id = f"{self.user_a.id}:person:self"
        # Mix in a B-tenant id explicitly to verify cross-tenant seeds
        # are filtered out by the seed ``$match``.
        b_self_id = f"{self.user_b.id}:person:self"

        result = await expand_graph(
            self.mongo_client,
            TEST_DATABASE,
            [a_self_id, b_self_id],
            self.user_a.id,
            max_hops=2,
        )

        self._assert_no_b_rows(result.nodes)
        self._assert_no_b_rows(result.edges)
        self._assert_no_b_tokens(result.nodes)
        self._assert_no_b_tokens(result.edges)

    # ------------------------------------------------------------------
    # Query path 11 — query_memory orchestrator
    # ------------------------------------------------------------------

    async def test_query_memory_orchestrator_does_not_leak_b_rows(self) -> None:
        result = await structured_query_memory(
            self.mongo_client,
            TEST_DATABASE,
            "Tell me about the antelope analytics project",
            self.embedding_model,
            self.user_a.id,
        )

        self._assert_no_b_rows(result.nodes)
        self._assert_no_b_rows(result.edges)
        self._assert_no_b_tokens(result.nodes)
        self._assert_no_b_tokens(result.edges)

        # Even when the query intentionally hits B's tokens, the
        # tenant filter holds — every hit must belong to A or the result
        # is empty.
        result_b_token = await structured_query_memory(
            self.mongo_client,
            TEST_DATABASE,
            "Tell me about the badger reporting service",
            self.embedding_model,
            self.user_a.id,
        )
        self._assert_no_b_rows(result_b_token.nodes)
        self._assert_no_b_rows(result_b_token.edges)
        self._assert_no_b_tokens(result_b_token.nodes)
        self._assert_no_b_tokens(result_b_token.edges)

    # ------------------------------------------------------------------
    # Query path 12 — NL query (MCP execute_nl_query path)
    # ------------------------------------------------------------------

    async def test_nl_query_path_injects_user_id_into_match(self) -> None:
        """An LLM-emitted pipeline that OMITS ``user_id`` must still be filtered.

        Even though the FakeLLM returns a pipeline without a tenant
        filter, ``execute_nl_query`` injects ``user_id`` into every
        ``$match`` and ``$vectorSearch.filter``. The result must be
        free of B rows.
        """

        # The LLM forgot the user_id filter. The guard must add it.
        llm = FakeLLM(
            [
                {
                    "pipeline": [
                        {"$match": {"kind": "node", "type": "person"}},
                        {"$limit": 50},
                    ]
                }
            ]
        )

        results = await execute_nl_query(
            self.mongo_client,
            TEST_DATABASE,
            "find every person",
            llm,
            self.embedding_model,
            self.user_a.id,
        )

        self._assert_no_b_rows(results)
        self._assert_no_b_tokens(results)

    # ------------------------------------------------------------------
    # Query path 12b — NL query with $lookup must reject + retry cleanly
    # ------------------------------------------------------------------

    async def test_nl_query_lookup_stage_is_rejected_no_b_leak(self) -> None:
        """An LLM emitting ``$lookup`` cannot leak B rows through a join.

        ``$lookup`` is intentionally NOT in ``_ALLOWED_STAGES`` because its
        sub-pipeline form bypasses ``_inject_user_id`` (which walks the
        top level of the outer pipeline only). Regression test for the PR
        rollup #025 blocker.

        The fake LLM first emits a leaking ``$lookup`` (sub-pipeline lacks
        ``user_id``). The validator rejects it; the retry prompt drives
        the second emission, a clean ``$graphLookup`` shape. The final
        result set must be free of B rows in BOTH the top-level documents
        and any joined ``connected_edges`` array. If the validator ever
        regresses and lets ``$lookup`` through, the LLM-emitted
        sub-pipeline would surface User-B edges in that array and trip
        ``_assert_no_b_rows`` / ``_assert_no_b_tokens``.
        """

        leaking_lookup_pipeline = {
            "pipeline": [
                {"$match": {"kind": "node", "type": "person"}},
                {
                    "$lookup": {
                        "from": "knowledge_graph",
                        # Sub-pipeline does NOT include a user_id predicate.
                        # If validation regressed and let this through,
                        # Mongo would scan the whole collection (every
                        # tenant) and stuff B's edges into ``leaked_edges``.
                        "pipeline": [{"$match": {"kind": "edge"}}],
                        "as": "leaked_edges",
                    }
                },
                {"$limit": 50},
            ]
        }
        clean_followup_pipeline = {
            "pipeline": [
                {"$match": {"kind": "node", "type": "person"}},
                {
                    "$graphLookup": {
                        "from": "knowledge_graph",
                        "startWith": "$_id",
                        "connectFromField": "target_node_id",
                        "connectToField": "source_node_id",
                        "maxDepth": 0,
                        "as": "connected_edges",
                    }
                },
                {"$limit": 50},
            ]
        }
        llm = FakeLLM([leaking_lookup_pipeline, clean_followup_pipeline])

        results = await execute_nl_query(
            self.mongo_client,
            TEST_DATABASE,
            "find every person and their edges",
            llm,
            self.embedding_model,
            self.user_a.id,
            max_retries=1,
        )

        # Top-level isolation must hold.
        self._assert_no_b_rows(results)
        self._assert_no_b_tokens(results)

        # Verify no joined array smuggled in a B row, regardless of
        # which key the (clean) second pipeline used.
        for doc in results:
            for value in doc.values():
                if isinstance(value, list):
                    self._assert_no_b_rows(
                        [item for item in value if isinstance(item, dict)]
                    )
                    self._assert_no_b_tokens(
                        [item for item in value if isinstance(item, dict)]
                    )

        # Sanity: the first (leaking) emission must have been rejected,
        # forcing the LLM to be re-prompted once.
        assert llm.call_count == 2, (
            "Expected validator to reject the $lookup emission and retry; "
            f"saw {llm.call_count} LLM calls."
        )

    # ------------------------------------------------------------------
    # Query path 16 — NL query with $facet wrapping $lookup must reject + retry
    # ------------------------------------------------------------------

    async def test_nl_query_facet_wrapped_lookup_is_rejected_no_b_leak(self) -> None:
        """Adversarial shape: ``$facet`` whose sub-pipeline carries a leaking ``$lookup``.

        The PR-review cycle-1 fix removed ``$lookup`` from the top-level
        allow-list, but the Tester found that an LLM could still smuggle a
        cross-tenant join through ``$facet`` (which carries one sub-pipeline
        per output field). ``_inject_user_id`` only walks the top level of
        the outer pipeline, so the sub-pipeline is never scoped.

        The fix removes ``$facet`` from ``_ALLOWED_STAGES`` so the planted
        shape is rejected at validation time. The fake LLM first emits the
        leaking shape; the retry prompt drives a clean follow-up that
        ``$facet``-free pipeline. The final result set must be free of B
        rows in BOTH the top-level documents and any nested array. If the
        validator ever regresses, the sub-pipeline ``$lookup`` would
        surface B's edges (containing ``badger`` / ``bramble`` tokens)
        inside the ``leaked`` faceted output.
        """

        leaking_facet_pipeline = {
            "pipeline": [
                {"$match": {"kind": "node"}},
                {
                    "$facet": {
                        "leaked": [
                            {
                                "$lookup": {
                                    "from": "knowledge_graph",
                                    # Sub-pipeline does NOT include a user_id
                                    # predicate. If validation regressed and
                                    # let ``$facet`` through, Mongo would scan
                                    # the whole collection (every tenant) and
                                    # stuff B's edges into ``leaked[0].all``.
                                    "pipeline": [{"$match": {"kind": "edge"}}],
                                    "as": "all",
                                }
                            },
                            {"$limit": 5},
                        ],
                    }
                },
                {"$limit": 5},
            ]
        }
        clean_followup_pipeline = {
            "pipeline": [
                {"$match": {"kind": "node", "type": "person"}},
                {"$limit": 50},
            ]
        }
        llm = FakeLLM([leaking_facet_pipeline, clean_followup_pipeline])

        results = await execute_nl_query(
            self.mongo_client,
            TEST_DATABASE,
            "find every person via facet",
            llm,
            self.embedding_model,
            self.user_a.id,
            max_retries=1,
        )

        # Top-level isolation must hold.
        self._assert_no_b_rows(results)
        self._assert_no_b_tokens(results)

        # Verify no nested array smuggled in a B row, regardless of
        # which key the (clean) second pipeline emitted.
        for doc in results:
            for value in doc.values():
                if isinstance(value, list):
                    self._assert_no_b_rows(
                        [item for item in value if isinstance(item, dict)]
                    )
                    self._assert_no_b_tokens(
                        [item for item in value if isinstance(item, dict)]
                    )

        # Sanity: the first (leaking) emission must have been rejected,
        # forcing the LLM to be re-prompted once.
        assert llm.call_count == 2, (
            "Expected validator to reject the $facet emission and retry; "
            f"saw {llm.call_count} LLM calls."
        )

    # ------------------------------------------------------------------
    # Query path 17 — NL query with no leading $match/$vectorSearch
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "first_stage,description",
        [
            pytest.param(
                {"$group": {"_id": "$type", "items": {"$push": "$$ROOT"}}},
                "group on type with $$ROOT push",
                id="group",
            ),
            pytest.param({"$sample": {"size": 20}}, "sample 20", id="sample"),
            pytest.param({"$sort": {"name": 1}}, "sort by name", id="sort"),
            pytest.param(
                {"$project": {"name": 1, "type": 1, "user_id": 1, "properties": 1}},
                "project selected fields",
                id="project",
            ),
            pytest.param(
                {"$unwind": "$properties.aliases"}, "unwind aliases", id="unwind"
            ),
            pytest.param(
                {
                    "$bucket": {
                        # ``$type`` is `$$REMOVE``-coerced into a length via
                        # ``$strLenCP`` on a defaulted string, so even edges
                        # (which have a ``type`` enum) and nodes (which have a
                        # ``type`` enum) both produce a string. Boundaries
                        # 1..200 cover every realistic length; ``default`` 999
                        # is outside the boundaries (Mongo's invariant).
                        "groupBy": {
                            "$strLenCP": {"$ifNull": [{"$toString": "$type"}, ""]}
                        },
                        "boundaries": [1, 200],
                        "default": 999,
                        "output": {"items": {"$push": "$$ROOT"}},
                    }
                },
                "bucket on type-string length",
                id="bucket",
            ),
            pytest.param({"$count": "total"}, "count documents", id="count"),
            pytest.param(
                {"$sortByCount": "$user_id"},
                "sortByCount on user_id (reveals tenant ids if unfiltered)",
                id="sortbycount",
            ),
        ],
    )
    async def test_nl_query_first_stage_without_match_does_not_leak(
        self, first_stage, description
    ) -> None:
        """Regression for the cycle-2 Tester finding (Blocker, cycle 3).

        An LLM-emitted pipeline whose first stage is NOT ``$match`` /
        ``$vectorSearch`` previously ran that stage against the unfiltered
        ``knowledge_graph`` collection — cross-tenant rows surfaced inside
        ``$group``'s ``items``, ``$sample``'s direct output, ``$count``'s
        ``total``, and so on. The fix prepends ``{"$match": {"user_id":
        <bound>}}`` as the leading stage in ``validate_pipeline`` whenever
        the pipeline doesn't already lead with a tenant-scoping stage.

        Drives the full ``execute_nl_query`` path with a fake LLM emitting
        each adversarial first stage. Asserts the result is free of B
        tokens in the top-level documents AND in any nested array
        (faceted / grouped / bucketed output shapes).
        """

        llm = FakeLLM(
            [
                {
                    "pipeline": [
                        first_stage,
                        {"$limit": 50},
                    ]
                }
            ]
        )

        results = await execute_nl_query(
            self.mongo_client,
            TEST_DATABASE,
            f"adversarial first stage: {description}",
            llm,
            self.embedding_model,
            self.user_a.id,
        )

        # The recursive helpers walk nested arrays so faceted /grouped
        # /bucketed shapes are inspected too.
        self._assert_no_b_rows(results)
        self._assert_no_b_tokens(results)

    # ------------------------------------------------------------------
    # Query path 13 — MCP query_memory tool
    # ------------------------------------------------------------------

    async def test_mcp_query_memory_tool_does_not_leak(self) -> None:
        """End-to-end: the MCP ``query_memory`` tool with a pinned ``user_id``.

        Calls the tool function directly with a fake ``Context`` whose
        ``lifespan_context["user_id"]`` is User A. The tool delegates to
        ``execute_nl_query``; we assert the textual output contains no
        B tokens.
        """

        from unittest.mock import MagicMock

        from tree.mcp.tools import query_memory

        llm = FakeLLM(
            [
                {
                    "pipeline": [
                        {"$match": {"kind": "node"}},
                        {"$limit": 50},
                    ]
                }
            ]
        )
        ctx = MagicMock()
        ctx.lifespan_context = {
            "client": self.mongo_client,
            "database": TEST_DATABASE,
            "llm": llm,
            "embedding_model": self.embedding_model,
            "user_id": self.user_a.id,
        }

        text = await query_memory("show me everything", ctx)

        # The tool returns a JSON string; lowercase it and grep for B's
        # distinctive tokens. None must appear.
        lowered = text.lower()
        for tok in ("badger", "bramble"):
            assert tok not in lowered, (
                f"LEAK — token {tok!r} appears in MCP query_memory output:\n{text}"
            )
        # And the User-B tenant id must not appear in any returned row.
        assert str(self.user_b.id) not in text, (
            "LEAK — User-B id appears in MCP query_memory output."
        )

    # ------------------------------------------------------------------
    # Query path 14 — MCP search_memory tool
    # ------------------------------------------------------------------

    async def test_mcp_search_memory_tool_does_not_leak(self) -> None:
        from unittest.mock import MagicMock

        from tree.mcp.tools import search_memory

        ctx = MagicMock()
        ctx.lifespan_context = {
            "client": self.mongo_client,
            "database": TEST_DATABASE,
            "llm": None,
            "embedding_model": self.embedding_model,
            "user_id": self.user_a.id,
        }

        text = await search_memory("badger bramble", ctx)

        lowered = text.lower()
        for tok in ("badger", "bramble"):
            assert tok not in lowered, (
                f"LEAK — token {tok!r} appears in MCP search_memory output:\n{text}"
            )
        assert str(self.user_b.id) not in text

    # ------------------------------------------------------------------
    # Admin escape hatch (DOCUMENTED leak point)
    # ------------------------------------------------------------------

    async def test_raw_pymongo_returns_both_tenants_documented_admin_only(
        self,
    ) -> None:
        """The single supported "leak" path: an admin who bypasses ``KGQuery``.

        Per the task spec, raw ``KnowledgeGraphEntry.find({}).to_list()``
        is the one place that returns rows from both tenants — and that's
        intentional for migration / review tooling. Production query paths
        instead go through ``KGQuery``, which always scopes by ``user_id``.
        """

        col = self.mongo_client[TEST_DATABASE]["knowledge_graph"]
        all_rows = await col.find({}).to_list()
        user_ids = {row["user_id"] for row in all_rows}

        assert self.user_a.id in user_ids
        assert self.user_b.id in user_ids


# ---------------------------------------------------------------------------
# Planted-leak procedure (DOCUMENTED in the SWE log; NOT run by pytest)
# ---------------------------------------------------------------------------
#
# Per the task spec, the SWE must demonstrate that this test is exercising
# the contract — not passing vacuously. The procedure (documented in the
# SWE log of #021):
#
#   1. In ``apps/memory/src/tree/memory/query/kgquery.py``, temporarily
#      remove the ``"user_id": self.user_id`` key from the ``find_nodes``
#      filter dictionary.
#   2. Re-run ``uv run pytest tests/integration/test_two_user_isolation.py
#      -k test_kgquery_find_nodes_by_type_returns_only_user_a``.
#      The test must FAIL with "LEAK — row from user_id=<B> surfaced".
#   3. Revert the change. Re-run. The test must PASS.
#
# The same applies to every other query path's filter: removing it
# surfaces a leak. The acceptance gate hinges on this being exercisable.
