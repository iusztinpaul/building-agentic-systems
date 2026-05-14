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

import pytest
from prefect import tags as prefect_tags

from tree.entities.documents import Document, SourceType
from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.memory.extraction.pipeline import memory_extraction
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM


TEST_DATABASE = "integration_tests_twin"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_doc(
    *,
    title: str = "Test Document",
    content: str = "Alice is building an ML pipeline for production.",
    source_uri: str = "https://example.com/test-doc",
) -> Document:
    doc = Document(
        title=title,
        content=content,
        source_type=SourceType.HUGGINGFACE,
        source_uri=source_uri,
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


_ALICE_TODO_RESPONSE: dict[str, Any] = {
    "nodes": [
        {"name": "alice", "type": "person", "properties": {"aliases": ["alice doe"]}},
        {
            "name": "build ml pipeline",
            "type": "task",
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
        doc = await _insert_doc()
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_ALICE_TODO_RESPONSE]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            summary = await memory_extraction(document_ids=[str(doc.id)])

        assert summary.documents_processed == 1
        assert summary.nodes_written > 0
        assert summary.edges_written > 0

        node_entries, edge_entries = await _kg_entries(mongo_client, doc.id)
        # DOCUMENT, CHUNK, PERSON, TASK all present.
        node_types = {n["type"] for n in node_entries}
        assert NodeType.DOCUMENT in node_types
        assert NodeType.CHUNK in node_types
        assert NodeType.PERSON in node_types
        assert NodeType.TASK in node_types
        # PERSON id is type-prefixed.
        person_nodes = [n for n in node_entries if n["type"] == NodeType.PERSON]
        assert person_nodes[0]["_id"] == "person:alice"

    async def test_skips_document_without_content(self, mongo_client, mocker) -> None:
        doc = Document(
            title="Empty Doc",
            content="",
            source_type=SourceType.HUGGINGFACE,
            source_uri="https://example.com/empty",
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
            summary = await memory_extraction(document_ids=[str(doc.id)])

        assert summary.nodes_written == 0
        assert summary.edges_written == 0
        node_entries, edge_entries = await _kg_entries(mongo_client, doc.id)
        assert node_entries == []
        assert edge_entries == []

    async def test_processes_multiple_documents(self, mongo_client, mocker) -> None:
        """Per-doc fan-out: tasks ① and ② run separately for each document."""

        doc1 = await _insert_doc(
            title="Doc 1",
            content="Alice works on ML.",
            source_uri="https://example.com/doc1",
        )
        doc2 = await _insert_doc(
            title="Doc 2",
            content="Bob builds data pipelines.",
            source_uri="https://example.com/doc2",
        )

        fake_llm = FakeLLM([_ALICE_TODO_RESPONSE, _ALICE_TODO_RESPONSE])
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=fake_llm,
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            summary = await memory_extraction(document_ids=[str(doc1.id), str(doc2.id)])

        assert summary.documents_processed == 2
        # LLM was called once per chunk per doc (>= 2 calls total).
        assert fake_llm.call_count >= 2

        for doc in [doc1, doc2]:
            node_entries, _ = await _kg_entries(mongo_client, doc.id)
            assert any(n["type"] == NodeType.DOCUMENT for n in node_entries)

    async def test_structural_edges_created(self, mongo_client, mocker) -> None:
        doc = await _insert_doc()
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_ALICE_TODO_RESPONSE]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(document_ids=[str(doc.id)])

        _, edge_entries = await _kg_entries(mongo_client, doc.id)
        edge_types = {e["type"] for e in edge_entries}
        assert EdgeType.PART_OF in edge_types
        assert EdgeType.MENTIONS in edge_types


# ---------------------------------------------------------------------------
# Idempotency (re-running produces the same state)
# ---------------------------------------------------------------------------


class TestIdempotency:
    async def test_idempotent_upserts(self, mongo_client, mocker) -> None:
        doc = await _insert_doc()
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_ALICE_TODO_RESPONSE]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(document_ids=[str(doc.id)])

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
            await memory_extraction(document_ids=[str(doc.id)])

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
                "properties": {"aliases": ["ali"]},
            },
            {
                "name": "alice smith",
                "type": "person",
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

        doc = await _insert_doc(
            content="Alice and Alice met.",
            source_uri="https://example.com/exact-dedup",
        )
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([_two_alice_response()]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(document_ids=[str(doc.id)])

        node_entries, _ = await _kg_entries(mongo_client, doc.id)
        persons = [n for n in node_entries if n["type"] == NodeType.PERSON]
        assert len(persons) == 1
        assert persons[0]["_id"] == "person:alice smith"

    async def test_cross_type_protection(self, mongo_client, mocker) -> None:
        """PERSON ``alice`` and TASK ``alice`` stay as two distinct nodes."""

        response = {
            "nodes": [
                {"name": "alice", "type": "person", "properties": {}},
                {
                    "name": "alice",
                    "type": "task",
                    "properties": {"content": "the alice task"},
                },
            ],
            "edges": [],
        }
        doc = await _insert_doc(
            content="Alice has the alice task.",
            source_uri="https://example.com/cross-type",
        )
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([response]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(document_ids=[str(doc.id)])

        node_entries, _ = await _kg_entries(mongo_client, doc.id)
        person = [n for n in node_entries if n["type"] == NodeType.PERSON]
        task = [n for n in node_entries if n["type"] == NodeType.TASK]
        assert len(person) == 1
        assert len(task) == 1
        # IDs are type-prefixed, so even shared surface form stays distinct.
        assert person[0]["_id"] == "person:alice"
        assert task[0]["_id"] == "task:alice"

    async def test_edge_remapping_after_in_payload_collapse(
        self, mongo_client, mocker
    ) -> None:
        """Duplicate Alice + Alice → ml-pipeline edges collapse into one."""

        response = {
            "nodes": [
                {"name": "alice", "type": "person", "properties": {}},
                {"name": "alice", "type": "person", "properties": {}},
                {
                    "name": "build ml pipeline",
                    "type": "task",
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
        doc = await _insert_doc(
            content="Alice has a task. Alice owns the build ml pipeline task.",
            source_uri="https://example.com/edge-dedup",
        )
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([response]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(document_ids=[str(doc.id)])

        _, edge_entries = await _kg_entries(mongo_client, doc.id)
        todo_edges = [e for e in edge_entries if e["type"] == EdgeType.TODO]
        assert len(todo_edges) == 1
        assert (
            todo_edges[0]["_id"]
            == f"person:alice|{EdgeType.TODO}|task:build ml pipeline"
        )


# ---------------------------------------------------------------------------
# Misconfiguration fails fast at flow entry
# ---------------------------------------------------------------------------


class TestMisconfigurationFailsFast:
    async def test_type_strict_disagreement_raises_at_entry(
        self, mongo_client, mocker, monkeypatch
    ) -> None:
        doc = await _insert_doc()
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
                await memory_extraction(document_ids=[str(doc.id)])
