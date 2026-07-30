from unittest.mock import AsyncMock, MagicMock

from beanie import PydanticObjectId

from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.memory.extraction.core import (
    _MAX_ALIASES,
    _MAX_SOURCES,
    _parse_extraction,
    build_structural_entries,
    chunk_document,
    extract_entities,
    upsert_graph_entries,
)
from tree.memory.types import ExtractionResult, ExtractedEdge, ExtractedNode
from tree.models.fake_model import FakeLLM


# ---------------------------------------------------------------------------
# chunk_document
# ---------------------------------------------------------------------------


class TestChunkDocument:
    def test_short_text_single_chunk(self):
        chunks = chunk_document("Hello world", chunk_size=100)
        assert len(chunks) == 1
        assert "Hello world" in chunks[0]

    def test_empty_text(self):
        assert chunk_document("") == []

    def test_long_text_multiple_chunks(self):
        text = "word " * 1000
        chunks = chunk_document(text, chunk_size=100, chunk_overlap=20)
        assert len(chunks) > 1

    def test_overlap_produces_more_chunks(self):
        text = "word " * 200
        no_overlap = chunk_document(text, chunk_size=100, chunk_overlap=0)
        with_overlap = chunk_document(text, chunk_size=100, chunk_overlap=50)
        assert len(with_overlap) > len(no_overlap)


# ---------------------------------------------------------------------------
# _parse_extraction
# ---------------------------------------------------------------------------


class TestParseExtraction:
    def test_valid_nodes_and_edges(self):
        # Post-#029: the LLM-extractable wire shape uses ``related_to``
        # with a ``semantic_type``. The parser also tolerates a legacy
        # ``"todo"`` emission (re-routed to ``related_to + has_task``);
        # exercise the canonical new shape here, the legacy re-route is
        # pinned by ``test_legacy_todo_reroutes_to_related_to``.
        raw = {
            "nodes": [
                {"name": "Alice", "type": "person", "properties": {"aliases": []}},
                {
                    "name": "Write code",
                    "type": "object",
                    "subtype": "task",
                    "properties": {"content": "x"},
                },
            ],
            "edges": [
                {
                    "source_node_id": "alice",
                    "source_type": "person",
                    "target_node_id": "write code",
                    "target_type": "object",
                    "type": "related_to",
                    "semantic_type": "has_task",
                }
            ],
        }
        result = _parse_extraction(raw)

        assert len(result.nodes) == 2
        assert result.nodes[0].name == "alice"
        assert result.nodes[0].type == NodeType.PERSON
        assert len(result.edges) == 1
        assert result.edges[0].type == EdgeType.RELATED_TO
        assert result.edges[0].semantic_type == "has_task"

    def test_legacy_todo_reroutes_to_related_to(self):
        # The parser re-routes legacy LLM emissions so cached examples
        # / older prompts still produce valid POLE+O edges.
        raw = {
            "nodes": [],
            "edges": [
                {
                    "source_node_id": "alice",
                    "source_type": "person",
                    "target_node_id": "write code",
                    "target_type": "task",  # legacy top-level type
                    "type": "todo",
                }
            ],
        }
        result = _parse_extraction(raw)
        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.type == EdgeType.RELATED_TO
        assert edge.semantic_type == "has_task"
        assert edge.target_type == NodeType.OBJECT

    def test_legacy_experienced_reroutes_to_related_to(self):
        raw = {
            "nodes": [],
            "edges": [
                {
                    "source_node_id": "alice",
                    "source_type": "person",
                    "target_node_id": "first day",
                    "target_type": "event",
                    "type": "experienced",
                }
            ],
        }
        result = _parse_extraction(raw)
        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.type == EdgeType.RELATED_TO
        assert edge.semantic_type == "experienced_by"
        assert edge.target_type == NodeType.EVENT

    def test_drops_related_to_with_unknown_semantic(self):
        raw = {
            "nodes": [],
            "edges": [
                {
                    "source_node_id": "alice",
                    "source_type": "person",
                    "target_node_id": "bob",
                    "target_type": "person",
                    "type": "related_to",
                    "semantic_type": "dragon_breath",
                }
            ],
        }
        result = _parse_extraction(raw)
        assert result.edges == []

    def test_drops_related_to_with_pair_violation(self):
        # employed_by is (person, organization), not (organization, person).
        raw = {
            "nodes": [],
            "edges": [
                {
                    "source_node_id": "anthropic",
                    "source_type": "organization",
                    "target_node_id": "alice",
                    "target_type": "person",
                    "type": "related_to",
                    "semantic_type": "employed_by",
                }
            ],
        }
        result = _parse_extraction(raw)
        assert result.edges == []

    def test_drops_related_to_with_missing_semantic(self):
        raw = {
            "nodes": [],
            "edges": [
                {
                    "source_node_id": "alice",
                    "source_type": "person",
                    "target_node_id": "bob",
                    "target_type": "person",
                    "type": "related_to",
                }
            ],
        }
        result = _parse_extraction(raw)
        assert result.edges == []

    def test_skips_invalid_node_type(self):
        raw = {"nodes": [{"name": "x", "type": "invalid_type"}], "edges": []}
        result = _parse_extraction(raw)
        assert result.nodes == []

    def test_skips_non_extractable_node_type(self):
        raw = {"nodes": [{"name": "doc", "type": "document"}], "edges": []}
        result = _parse_extraction(raw)
        assert result.nodes == []

    def test_skips_structural_edge_type(self):
        raw = {
            "nodes": [],
            "edges": [
                {
                    "source_node_id": "chunk",
                    "source_type": "chunk",
                    "target_node_id": "doc",
                    "target_type": "document",
                    "type": "part_of",
                }
            ],
        }
        result = _parse_extraction(raw)
        assert result.edges == []

    def test_skips_edge_violating_constraint(self):
        # The legacy reverse-direction ``todo`` is rewritten to
        # ``related_to + has_task`` first, then dropped because
        # (object, person) is not in ``has_task.allowed_pairs``.
        raw = {
            "nodes": [],
            "edges": [
                {
                    "source_node_id": "task1",
                    "source_type": "task",
                    "target_node_id": "alice",
                    "target_type": "person",
                    "type": "todo",
                }
            ],
        }
        result = _parse_extraction(raw)
        assert result.edges == []

    def test_empty_input(self):
        result = _parse_extraction({})
        assert result.nodes == []
        assert result.edges == []

    def test_lowercases_and_strips_names(self):
        raw = {
            "nodes": [{"name": "  Alice Smith  ", "type": "person", "properties": {}}],
            "edges": [],
        }
        result = _parse_extraction(raw)
        assert result.nodes[0].name == "alice smith"


# ---------------------------------------------------------------------------
# extract_entities
# ---------------------------------------------------------------------------


class TestExtractEntities:
    async def test_calls_llm_and_parses(self):
        llm = FakeLLM(
            responses=[
                {
                    "nodes": [
                        {
                            "name": "bob",
                            "type": "person",
                            "properties": {"aliases": []},
                        }
                    ],
                    "edges": [],
                }
            ]
        )
        result = await extract_entities(llm, "Bob went to work.")

        assert len(result.nodes) == 1
        assert result.nodes[0].name == "bob"
        assert llm.call_count == 1

    async def test_stamps_chunk_id(self):
        llm = FakeLLM(
            responses=[
                {
                    "nodes": [
                        {"name": "alice", "type": "person", "properties": {}},
                    ],
                    "edges": [
                        {
                            "source_node_id": "alice",
                            "source_type": "person",
                            "target_node_id": "write code",
                            "target_type": "task",
                            "type": "todo",
                        },
                    ],
                }
            ]
        )
        result = await extract_entities(llm, "text", chunk_id="chunk-42")

        assert result.nodes[0].chunk_id == "chunk-42"
        assert result.edges[0].chunk_id == "chunk-42"


# ---------------------------------------------------------------------------
# build_structural_entries
# ---------------------------------------------------------------------------


def _build(
    chunk_texts: list[str],
    extracted: ExtractionResult | None = None,
    reference_uris: list[str] | None = None,
) -> ExtractionResult:
    """Helper to call build_structural_entries with sensible defaults."""
    chunk_ids = [f"cid-{i}" for i in range(len(chunk_texts))]
    return build_structural_entries(
        document_id=PydanticObjectId(),
        source_type="substack",
        source_uri="https://example.com/article",
        date="2026-01-01",
        chunk_texts=chunk_texts,
        chunk_ids=chunk_ids,
        extracted=extracted or ExtractionResult(),
        reference_uris=reference_uris,
    )


class TestBuildStructuralEntries:
    def test_creates_document_node(self):
        result = _build(["chunk 0"])

        doc_nodes = [n for n in result.nodes if n.type == NodeType.DOCUMENT]
        assert len(doc_nodes) == 1
        assert doc_nodes[0].name == "https://example.com/article"

    def test_creates_chunk_nodes(self):
        result = _build(["chunk 0", "chunk 1", "chunk 2"])

        chunk_nodes = [n for n in result.nodes if n.type == NodeType.CHUNK]
        assert len(chunk_nodes) == 3

    def test_creates_part_of_edges(self):
        result = _build(["a", "b"])

        part_of = [e for e in result.edges if e.type == EdgeType.PART_OF]
        assert len(part_of) == 2

    def test_creates_next_edges(self):
        result = _build(["a", "b", "c"])

        next_edges = [e for e in result.edges if e.type == EdgeType.NEXT]
        assert len(next_edges) == 2

    def test_creates_mentions_edges(self):
        extracted = ExtractionResult(
            nodes=[
                ExtractedNode(name="alice", type=NodeType.PERSON, properties={}),
                ExtractedNode(name="bob", type=NodeType.PERSON, properties={}),
            ],
        )

        result = _build(["text"], extracted=extracted)

        mentions = [e for e in result.edges if e.type == EdgeType.MENTIONS]
        assert len(mentions) == 2
        target_names = {e.target_node_id for e in mentions}
        assert target_names == {"alice", "bob"}

    def test_no_next_edge_for_single_chunk(self):
        result = _build(["only one"])

        next_edges = [e for e in result.edges if e.type == EdgeType.NEXT]
        assert len(next_edges) == 0

    def test_creates_referenced_edges(self):
        refs = ["https://ref1.com/article", "https://ref2.com/article"]
        result = _build(["text"], reference_uris=refs)

        referenced = [e for e in result.edges if e.type == EdgeType.REFERENCED]
        assert len(referenced) == 2
        targets = {e.target_node_id for e in referenced}
        assert targets == set(refs)
        for edge in referenced:
            assert edge.source_type == NodeType.DOCUMENT
            assert edge.target_type == NodeType.DOCUMENT

    def test_no_referenced_edges_when_none(self):
        result = _build(["text"], reference_uris=None)

        referenced = [e for e in result.edges if e.type == EdgeType.REFERENCED]
        assert len(referenced) == 0

    def test_chunk_ids_stamped_on_entries(self):
        result = _build(["a", "b"])

        chunk_nodes = [n for n in result.nodes if n.type == NodeType.CHUNK]
        assert chunk_nodes[0].chunk_id == "cid-0"
        assert chunk_nodes[1].chunk_id == "cid-1"

        part_of = [e for e in result.edges if e.type == EdgeType.PART_OF]
        assert part_of[0].chunk_id == "cid-0"
        assert part_of[1].chunk_id == "cid-1"


# ---------------------------------------------------------------------------
# normalize_nodes — DELETED in #012. Behavior is now covered by the six-task
# pipeline (see ``tests/unit/memory/extraction/test_pipeline.py``).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ExtractionResult.merge
# ---------------------------------------------------------------------------


class TestExtractionResultMerge:
    def test_merge_combines_nodes_and_edges(self):
        a = ExtractionResult(
            nodes=[ExtractedNode(name="alice", type=NodeType.PERSON, properties={})],
            edges=[],
        )
        b = ExtractionResult(
            nodes=[ExtractedNode(name="bob", type=NodeType.PERSON, properties={})],
            edges=[
                ExtractedEdge(
                    source_node_id="alice",
                    source_type=NodeType.PERSON,
                    target_node_id="bob",
                    target_type=NodeType.PERSON,
                    type=EdgeType.RELATED_TO,
                )
            ],
        )

        merged = a.merge(b)
        assert len(merged.nodes) == 2
        assert len(merged.edges) == 1


# ---------------------------------------------------------------------------
# upsert_graph_entries — array cap verification
# ---------------------------------------------------------------------------


def _extract_pipeline_ops(bulk_write_mock: AsyncMock) -> list:
    """Return the list of UpdateOne operations passed to bulk_write."""
    args, _ = bulk_write_mock.call_args
    return args[0]


def _find_slice_in_pipeline(pipeline_stages: list, path: str) -> int | None:
    """Walk aggregation-pipeline update stages and return the $slice limit
    applied to *path*, or ``None`` if no $slice is found."""
    for stage in pipeline_stages:
        sets = stage.get("$set", {})
        # Walk dot-path keys (e.g. "properties.aliases" or "sources").
        for key, expr in sets.items():
            if key == path and isinstance(expr, dict) and "$slice" in expr:
                return expr["$slice"][1]
    return None


class TestUpsertGraphEntriesArrayCaps:
    async def test_node_sources_capped(self):
        collection = AsyncMock()
        client = MagicMock()
        client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=collection))
        )

        result = ExtractionResult(
            nodes=[ExtractedNode(name="alice", type=NodeType.PERSON, properties={})],
            edges=[],
        )
        await upsert_graph_entries(
            result,
            user_id=PydanticObjectId(),
            source_document_id=PydanticObjectId(),
            database="test",
            client=client,
        )

        ops = _extract_pipeline_ops(collection.bulk_write)
        node_op = ops[0]
        pipeline = node_op._doc
        assert _find_slice_in_pipeline(pipeline, "sources") == _MAX_SOURCES

    async def test_node_aliases_capped(self):
        collection = AsyncMock()
        client = MagicMock()
        client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=collection))
        )

        result = ExtractionResult(
            nodes=[
                ExtractedNode(
                    name="alice",
                    type=NodeType.PERSON,
                    properties={"aliases": ["ali"]},
                )
            ],
            edges=[],
        )
        await upsert_graph_entries(
            result,
            user_id=PydanticObjectId(),
            source_document_id=PydanticObjectId(),
            database="test",
            client=client,
        )

        ops = _extract_pipeline_ops(collection.bulk_write)
        node_op = ops[0]
        pipeline = node_op._doc
        assert _find_slice_in_pipeline(pipeline, "properties.aliases") == _MAX_ALIASES

    async def test_edge_sources_capped(self):
        collection = AsyncMock()
        client = MagicMock()
        client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=collection))
        )

        result = ExtractionResult(
            nodes=[],
            edges=[
                ExtractedEdge(
                    source_node_id="alice",
                    source_type=NodeType.PERSON,
                    target_node_id="write code",
                    target_type=NodeType.OBJECT,
                    type=EdgeType.RELATED_TO,
                    semantic_type="has_task",
                )
            ],
        )
        await upsert_graph_entries(
            result,
            user_id=PydanticObjectId(),
            source_document_id=PydanticObjectId(),
            database="test",
            client=client,
        )

        ops = _extract_pipeline_ops(collection.bulk_write)
        edge_op = ops[0]
        pipeline = edge_op._doc
        assert _find_slice_in_pipeline(pipeline, "sources") == _MAX_SOURCES
