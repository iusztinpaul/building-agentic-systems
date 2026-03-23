"""Integration tests for the memory extraction pipeline."""

from prefect import tags as prefect_tags

from twin.entities.documents import Document, SourceType
from twin.entities.knowledge_graph import EdgeType, KnowledgeGraphLogEntry, NodeType
from twin.memory.extraction.pipeline import memory_extraction
from twin.models.fake_model import FakeLLM


FAKE_LLM_RESPONSE = {
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
        },
    ],
}

TEST_DATABASE = "integration_tests_twin"


async def _insert_test_document(
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


async def _get_raw_logs(mongo_client, doc_id) -> tuple[list[dict], list[dict]]:
    """Query raw log entries via pymongo to avoid Beanie is_root deserialization."""
    col = mongo_client[TEST_DATABASE]["knowledge_graph_log"]
    all_logs = await col.find({"source_document_id": doc_id}).to_list()
    node_logs = [e for e in all_logs if e["kind"] == "node"]
    edge_logs = [e for e in all_logs if e["kind"] == "edge"]
    return node_logs, edge_logs


class TestMemoryExtractionPipeline:
    async def test_extracts_nodes_and_edges_from_document(
        self, mongo_client, mocker
    ) -> None:
        doc = await _insert_test_document()

        mocker.patch(
            "twin.memory.extraction.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.extraction.pipeline.get_llm",
            return_value=FakeLLM([FAKE_LLM_RESPONSE]),
        )

        with prefect_tags("tests"):
            results = await memory_extraction(document_ids=[str(doc.id)])

        assert len(results) == 1
        result = results[0]
        assert len(result.nodes) > 0
        assert len(result.edges) > 0

        node_logs, edge_logs = await _get_raw_logs(mongo_client, doc.id)
        assert len(node_logs) > 0
        assert len(edge_logs) > 0

        # Verify structural nodes: at least one DOCUMENT and one CHUNK node
        node_types = {n["type"] for n in node_logs}
        assert NodeType.DOCUMENT in node_types
        assert NodeType.CHUNK in node_types

        # Verify LLM-extracted person node
        person_nodes = [n for n in node_logs if n["type"] == NodeType.PERSON]
        assert len(person_nodes) >= 1
        assert person_nodes[0]["name"] == "alice"

    async def test_skips_document_without_content(self, mongo_client, mocker) -> None:
        doc = Document(
            title="Empty Doc",
            content="",
            source_type=SourceType.HUGGINGFACE,
            source_uri="https://example.com/empty",
            authors=["Author"],
        )
        await doc.insert()

        mocker.patch(
            "twin.memory.extraction.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.extraction.pipeline.get_llm",
            return_value=FakeLLM(),
        )

        with prefect_tags("tests"):
            results = await memory_extraction(document_ids=[str(doc.id)])

        assert len(results) == 1
        assert len(results[0].nodes) == 0
        assert len(results[0].edges) == 0

        all_logs = await KnowledgeGraphLogEntry.find(
            KnowledgeGraphLogEntry.source_document_id == doc.id
        ).to_list()
        assert len(all_logs) == 0

    async def test_processes_multiple_documents(self, mongo_client, mocker) -> None:
        doc1 = await _insert_test_document(
            title="Doc 1",
            content="Alice works on ML.",
            source_uri="https://example.com/doc1",
        )
        doc2 = await _insert_test_document(
            title="Doc 2",
            content="Bob builds data pipelines.",
            source_uri="https://example.com/doc2",
        )

        mocker.patch(
            "twin.memory.extraction.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.extraction.pipeline.get_llm",
            return_value=FakeLLM([FAKE_LLM_RESPONSE, FAKE_LLM_RESPONSE]),
        )

        with prefect_tags("tests"):
            results = await memory_extraction(document_ids=[str(doc1.id), str(doc2.id)])

        assert len(results) == 2
        for result in results:
            assert len(result.nodes) > 0

        for doc in [doc1, doc2]:
            node_logs, _ = await _get_raw_logs(mongo_client, doc.id)
            assert len(node_logs) > 0

    async def test_structural_edges_created(self, mongo_client, mocker) -> None:
        doc = await _insert_test_document()

        mocker.patch(
            "twin.memory.extraction.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.extraction.pipeline.get_llm",
            return_value=FakeLLM([FAKE_LLM_RESPONSE]),
        )

        with prefect_tags("tests"):
            await memory_extraction(document_ids=[str(doc.id)])

        _, edge_logs = await _get_raw_logs(mongo_client, doc.id)
        edge_types = {e["type"] for e in edge_logs}

        assert EdgeType.PART_OF in edge_types
        assert EdgeType.MENTIONS in edge_types

    async def test_idempotent_log_entries_accumulate(
        self, mongo_client, mocker
    ) -> None:
        """Running extraction twice on the same document appends new log entries."""
        doc = await _insert_test_document()

        mocker.patch(
            "twin.memory.extraction.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.memory.extraction.pipeline.get_llm",
            return_value=FakeLLM([FAKE_LLM_RESPONSE]),
        )

        with prefect_tags("tests"):
            await memory_extraction(document_ids=[str(doc.id)])

        first_node_logs, _ = await _get_raw_logs(mongo_client, doc.id)
        first_count = len(first_node_logs)
        assert first_count > 0

        mocker.patch(
            "twin.memory.extraction.pipeline.get_llm",
            return_value=FakeLLM([FAKE_LLM_RESPONSE]),
        )

        with prefect_tags("tests"):
            await memory_extraction(document_ids=[str(doc.id)])

        second_node_logs, _ = await _get_raw_logs(mongo_client, doc.id)
        second_count = len(second_node_logs)
        assert second_count == first_count * 2
