"""Integration tests for the six-task memory-extraction pipeline.

Tests drive ``memory_extraction.fn(...)`` directly so we get the flow's
config validation + DB plumbing without spinning up a full Prefect run.
``mocker.patch`` swaps the LLM and embedding factories for fakes; the
``knowledge_graph`` collection is asserted directly with ``mongo_client``.

The ``TestNormalizeNodes`` scenarios from the deleted ``normalize_nodes``
implementation are rewired here as end-to-end flow assertions on the
collected DB state.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.entities.documents import Document, SourceType
from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.entities.users import User
from tree.memory.extraction.dedup import DeduplicationResult
from tree.memory.extraction.pipeline import memory_extraction
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM


TEST_DATABASE = "integration_tests_twin"


async def _make_user() -> User:
    """Create a test User and return it.

    The ``after_insert`` hook upserts the ``{user_id}:person:self`` node
    automatically; tests rely on the User existing so the pipeline's
    first-person resolver step has something to compare against.
    """

    user = User(identifier=f"test-user-{PydanticObjectId()}")
    await user.insert()
    return user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_doc(
    *,
    title: str = "Test Document",
    content: str = "Alice is building an ML pipeline for production.",
    source_uri: str = "https://example.com/test-doc",
    user_id: PydanticObjectId,
) -> Document:
    doc = Document(
        title=title,
        content=content,
        source_type=SourceType.HUGGINGFACE,
        source_uri=source_uri,
        user_id=user_id,
        authors=["Test Author"],
    )
    await doc.insert()
    return doc


async def _kg_entries(mongo_client, source_doc_id) -> tuple[list[dict], list[dict]]:
    """Return ``(nodes, edges)`` referencing the given source document.

    LLM-extracted nodes go through ``add_entity`` which stores ``source_id``
    as a string per its #011 contract; structural nodes and edges store the
    raw ``PydanticObjectId``. We match either form so the query covers all
    rows the pipeline produced for ``source_doc_id``.
    """

    col = mongo_client[TEST_DATABASE]["knowledge_graph"]
    entries = await col.find(
        {"sources": {"$in": [source_doc_id, str(source_doc_id)]}}
    ).to_list()
    return (
        [e for e in entries if e["kind"] == "node"],
        [e for e in entries if e["kind"] == "edge"],
    )


def _patch_pipeline_deps(
    mocker,
    mongo_client,
    *,
    llm: FakeLLM,
    embedding_model: FakeEmbeddingModel,
) -> None:
    """Patch the heavy dependencies of ``memory_extraction``.

    Importantly, we patch ``get_embedding_model`` in the pipeline module so
    the resolver / dedup / task ④ paths all share one fake model — and patch
    ``init_mongodb`` so the flow reuses the test's open client.

    ``dedupe_entity`` is also patched everywhere it's called (task ⑤'s
    ``_dedupe_entities`` and task ⑥'s ``add_entity``). Otherwise the real
    function would run an Atlas ``$vectorSearch`` against the
    ``knowledge_graph`` collection — in CI this happens before the indexing
    pipeline has had a chance to create ``vector_index``, and an in-flight
    aggregation against a missing search index can stall waiting for it to
    appear. Skipping dedup altogether matches the "no candidates" decision
    these tests expect anyway, and keeps the assertions stable.
    """

    mocker.patch(
        "tree.memory.extraction.pipeline.init_mongodb",
        return_value=mongo_client,
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
    # #042: task ④ embeds via the SEARCH model factory directly. Point it at
    # the same fake so the node-text vectors come from the test model.
    mocker.patch(
        "tree.memory.extraction.pipeline.get_search_embedding_model",
        return_value=embedding_model,
    )
    # Patch ``dedupe_entity`` at every call site so neither task ⑤ nor task ⑥
    # (via ``add_entity``) issues a live ``$vectorSearch``. Default decision
    # is ``"none"`` — i.e. always treat the incoming entity as new — which
    # matches the empty-graph starting state these tests rely on.
    mocker.patch(
        "tree.memory.extraction.pipeline.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )
    mocker.patch(
        "tree.memory.extraction.add_entity.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )


_ALICE_TODO_RESPONSE: dict[str, Any] = {
    "nodes": [
        # Post-#030: every LLM-extractable POLE+O node with a closed
        # subtype vocabulary MUST emit ``subtype`` (envelope check).
        {
            "name": "alice",
            "type": "person",
            "subtype": "individual",
            "properties": {"aliases": ["alice doe"]},
        },
        {
            "name": "build ml pipeline",
            "type": "task",
            "subtype": "task",
            "properties": {"content": "Build an ML pipeline"},
        },
    ],
    "edges": [
        {
            "source_node_id": "alice",
            "source_type": "person",
            "target_node_id": "build ml pipeline",
            "target_type": "task",
            "type": "todo",
            "properties": {},
        }
    ],
}


# ---------------------------------------------------------------------------
# Pipeline shape — end-to-end happy path
# ---------------------------------------------------------------------------


class TestMemoryExtractionPipeline:
    async def test_extracts_nodes_and_edges_from_document(
        self, mongo_client, mocker
    ) -> None:
        user = await _make_user()
        doc = await _insert_doc(user_id=user.id)
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_ALICE_TODO_RESPONSE]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            summary = await memory_extraction(
                user_id=user.id, document_ids=[str(doc.id)]
            )

        assert summary.documents_processed == 1
        assert summary.nodes_written > 0
        assert summary.edges_written > 0

        node_entries, edge_entries = await _kg_entries(mongo_client, doc.id)
        # Post-#028: DOCUMENT, CHUNK, PERSON are unchanged; what used
        # to be ``type=task`` now stores as ``type=object,
        # subtype=task`` — same logical content, new POLE+O shape.
        node_types = {n["type"] for n in node_entries}
        assert NodeType.DOCUMENT in node_types
        assert NodeType.CHUNK in node_types
        assert NodeType.PERSON in node_types
        assert NodeType.OBJECT in node_types
        object_nodes = [n for n in node_entries if n["type"] == NodeType.OBJECT]
        assert any(n.get("subtype") == "task" for n in object_nodes), (
            "expected at least one object-subtype-task node from the LLM emission"
        )
        # PERSON id carries the real user_id prefix (post-#019).
        person_nodes = [n for n in node_entries if n["type"] == NodeType.PERSON]
        assert person_nodes[0]["_id"] == f"{user.id}:person:alice"
        # And every row carries the right tenant.
        for n in node_entries:
            assert n["user_id"] == user.id

    async def test_skips_document_without_content(self, mongo_client, mocker) -> None:
        user = await _make_user()
        doc = Document(
            title="Empty Doc",
            content="",
            source_type=SourceType.HUGGINGFACE,
            source_uri="https://example.com/empty",
            user_id=user.id,
            authors=["Author"],
        )
        await doc.insert()
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM(),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            summary = await memory_extraction(
                user_id=user.id, document_ids=[str(doc.id)]
            )

        assert summary.nodes_written == 0
        assert summary.edges_written == 0
        node_entries, edge_entries = await _kg_entries(mongo_client, doc.id)
        assert node_entries == []
        assert edge_entries == []

    async def test_processes_multiple_documents(self, mongo_client, mocker) -> None:
        """Per-doc fan-out: tasks ① and ② run separately for each document."""

        user = await _make_user()
        doc1 = await _insert_doc(
            title="Doc 1",
            content="Alice works on ML.",
            source_uri="https://example.com/doc1",
            user_id=user.id,
        )
        doc2 = await _insert_doc(
            title="Doc 2",
            content="Bob builds data pipelines.",
            source_uri="https://example.com/doc2",
            user_id=user.id,
        )

        fake_llm = FakeLLM([_ALICE_TODO_RESPONSE, _ALICE_TODO_RESPONSE])
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=fake_llm,
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            summary = await memory_extraction(
                user_id=user.id, document_ids=[str(doc1.id), str(doc2.id)]
            )

        assert summary.documents_processed == 2
        # LLM was called once per chunk per doc (>= 2 calls total).
        assert fake_llm.call_count >= 2

        for doc in [doc1, doc2]:
            node_entries, _ = await _kg_entries(mongo_client, doc.id)
            assert any(n["type"] == NodeType.DOCUMENT for n in node_entries)

    async def test_structural_edges_created(self, mongo_client, mocker) -> None:
        user = await _make_user()
        doc = await _insert_doc(user_id=user.id)
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_ALICE_TODO_RESPONSE]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        _, edge_entries = await _kg_entries(mongo_client, doc.id)
        edge_types = {e["type"] for e in edge_entries}
        assert EdgeType.PART_OF in edge_types
        assert EdgeType.MENTIONS in edge_types

    # NOTE: The earlier ``test_two_users_isolation`` extraction-only
    # check was removed in #021. Its essential assertion (two tenants
    # extracting an identical-name PERSON produce distinct ``_id``s and
    # carry distinct ``user_id``s) is covered by:
    #   - ``tests/unit/entities/test_node_id_isolation.py`` (id shape).
    #   - ``tests/integration/test_two_user_isolation.py`` (full
    #     query-path acceptance gate, including the PERSON-row
    #     ``user_id`` invariant).


# ---------------------------------------------------------------------------
# Idempotency (re-running produces the same state)
# ---------------------------------------------------------------------------


class TestIdempotency:
    async def test_idempotent_upserts(self, mongo_client, mocker) -> None:
        user = await _make_user()
        doc = await _insert_doc(user_id=user.id)
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_ALICE_TODO_RESPONSE]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        first_nodes, first_edges = await _kg_entries(mongo_client, doc.id)
        first_count = len(first_nodes) + len(first_edges)
        assert first_count > 0

        # Re-patch (FakeLLM state carries over otherwise).
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_ALICE_TODO_RESPONSE]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )
        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        second_nodes, second_edges = await _kg_entries(mongo_client, doc.id)
        assert len(second_nodes) + len(second_edges) == first_count


# ---------------------------------------------------------------------------
# Rewired ``TestNormalizeNodes`` scenarios — now driven through the flow
# ---------------------------------------------------------------------------


def _two_alice_response() -> dict[str, Any]:
    return {
        "nodes": [
            {
                "name": "alice smith",
                "type": "person",
                "subtype": "individual",
                "properties": {"aliases": ["ali"]},
            },
            {
                "name": "alice smith",
                "type": "person",
                "subtype": "individual",
                "properties": {"email": "alice@example.com"},
            },
        ],
        "edges": [],
    }


class TestRewiredNormalizeNodesScenarios:
    """The eight ``TestNormalizeNodes`` scenarios, rewired to drive the new
    flow and assert the same observable end-state on the DB."""

    async def test_exact_dedup_within_payload(self, mongo_client, mocker) -> None:
        """Two mentions of the same name in one chunk → one PERSON node."""

        user = await _make_user()
        doc = await _insert_doc(
            content="Alice and Alice met.",
            source_uri="https://example.com/exact-dedup",
            user_id=user.id,
        )
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_two_alice_response()]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        node_entries, _ = await _kg_entries(mongo_client, doc.id)
        persons = [n for n in node_entries if n["type"] == NodeType.PERSON]
        assert len(persons) == 1
        assert persons[0]["_id"] == f"{user.id}:person:alice smith"

    async def test_cross_type_protection(self, mongo_client, mocker) -> None:
        """PERSON ``alice`` and TASK ``alice`` stay as two distinct nodes."""

        response = {
            "nodes": [
                {
                    "name": "alice",
                    "type": "person",
                    "subtype": "individual",
                    "properties": {},
                },
                {
                    "name": "alice",
                    "type": "task",
                    "subtype": "task",
                    "properties": {"content": "the alice task"},
                },
            ],
            "edges": [],
        }
        user = await _make_user()
        doc = await _insert_doc(
            content="Alice has the alice task.",
            source_uri="https://example.com/cross-type",
            user_id=user.id,
        )
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([response]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        node_entries, _ = await _kg_entries(mongo_client, doc.id)
        person = [n for n in node_entries if n["type"] == NodeType.PERSON]
        # Post-#028: legacy ``type=task`` is rerouted at extraction time
        # to ``type=object, subtype=task``; the type-prefix isolation
        # still holds (alice the person vs. alice the object are
        # distinct rows under different parent types).
        task_objects = [
            n
            for n in node_entries
            if n["type"] == NodeType.OBJECT and n.get("subtype") == "task"
        ]
        assert len(person) == 1
        assert len(task_objects) == 1
        # IDs are tenant- and type-prefixed (#018), so even a shared surface
        # form stays distinct across types.
        assert person[0]["_id"] == f"{user.id}:person:alice"
        assert task_objects[0]["_id"] == f"{user.id}:object:alice"

    async def test_edge_remapping_after_in_payload_collapse(
        self, mongo_client, mocker
    ) -> None:
        """Duplicate Alice + Alice → ml-pipeline edges collapse into one."""

        response = {
            "nodes": [
                {
                    "name": "alice",
                    "type": "person",
                    "subtype": "individual",
                    "properties": {},
                },
                {
                    "name": "alice",
                    "type": "person",
                    "subtype": "individual",
                    "properties": {},
                },
                {
                    "name": "build ml pipeline",
                    "type": "task",
                    "subtype": "task",
                    "properties": {"content": "x"},
                },
            ],
            "edges": [
                {
                    "source_node_id": "alice",
                    "source_type": "person",
                    "target_node_id": "build ml pipeline",
                    "target_type": "task",
                    "type": "todo",
                    "properties": {},
                },
                {
                    "source_node_id": "alice",
                    "source_type": "person",
                    "target_node_id": "build ml pipeline",
                    "target_type": "task",
                    "type": "todo",
                    "properties": {},
                },
            ],
        }
        user = await _make_user()
        doc = await _insert_doc(
            content="Alice has a task. Alice owns the build ml pipeline task.",
            source_uri="https://example.com/edge-dedup",
            user_id=user.id,
        )
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([response]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        # Post-#029: legacy ``todo`` LLM emissions re-route to
        # ``related_to + semantic_type='has_task'``; endpoint type
        # also re-routes from ``task`` to ``object``.
        _, edge_entries = await _kg_entries(mongo_client, doc.id)
        related_to_edges = [
            e
            for e in edge_entries
            if e["type"] == EdgeType.RELATED_TO and e.get("semantic_type") == "has_task"
        ]
        assert len(related_to_edges) == 1
        ph = user.id
        assert (
            related_to_edges[0]["_id"]
            == f"{ph}:person:alice|{EdgeType.RELATED_TO}|{ph}:object:build ml pipeline"
        )


# ---------------------------------------------------------------------------
# Misconfiguration fails fast at flow entry
# ---------------------------------------------------------------------------


class TestMisconfigurationFailsFast:
    async def test_type_strict_disagreement_raises_at_entry(
        self, mongo_client, mocker, monkeypatch
    ) -> None:
        user = await _make_user()
        doc = await _insert_doc(user_id=user.id)
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_ALICE_TODO_RESPONSE]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        monkeypatch.setenv("TREE_EXTRACTION__RESOLUTION__TYPE_STRICT", "true")
        monkeypatch.setenv("TREE_EXTRACTION__DEDUP__MATCH_SAME_TYPE_ONLY", "false")

        with pytest.raises(ValueError, match="type_strict.*match_same_type_only"):
            with prefect_tags("tests"):
                await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])
