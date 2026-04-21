from unittest.mock import AsyncMock, MagicMock

from beanie import PydanticObjectId

from twin.entities.knowledge_graph import EdgeType, NodeType
from twin.memory.extraction.core import (
    _MAX_ALIASES,
    _MAX_SOURCES,
    _parse_extraction,
    build_structural_entries,
    chunk_document,
    extract_entities,
    normalize_nodes,
    upsert_graph_entries,
)
from twin.memory.types import ExtractionResult, ExtractedEdge, ExtractedNode
from twin.models.fake_model import FakeLLM


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
        raw = {
            "nodes": [
                {"name": "Alice", "type": "person", "properties": {"aliases": []}},
                {"name": "Write code", "type": "task", "properties": {"content": "x"}},
            ],
            "edges": [
                {
                    "source_node_id": "alice",
                    "source_type": "person",
                    "target_node_id": "write code",
                    "target_type": "task",
                    "type": "todo",
                }
            ],
        }
        result = _parse_extraction(raw)

        assert len(result.nodes) == 2
        assert result.nodes[0].name == "alice"
        assert result.nodes[0].type == NodeType.PERSON
        assert len(result.edges) == 1
        assert result.edges[0].type == EdgeType.TODO

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
# normalize_nodes
# ---------------------------------------------------------------------------


class TestNormalizeNodes:
    async def test_merges_similar_names(self):
        result = ExtractionResult(
            nodes=[
                ExtractedNode(
                    name="alice smith",
                    type=NodeType.PERSON,
                    properties={"aliases": ["ali"]},
                ),
                ExtractedNode(
                    name="alice smith",
                    type=NodeType.PERSON,
                    properties={"email": "alice@example.com"},
                ),
            ],
            edges=[],
        )

        normalised = await normalize_nodes(result)
        person_nodes = [n for n in normalised.nodes if n.type == NodeType.PERSON]
        assert len(person_nodes) == 1

    async def test_keeps_different_names(self):
        result = ExtractionResult(
            nodes=[
                ExtractedNode(name="alice", type=NodeType.PERSON, properties={}),
                ExtractedNode(name="bob", type=NodeType.PERSON, properties={}),
            ],
            edges=[],
        )

        normalised = await normalize_nodes(result)
        assert len(normalised.nodes) == 2

    async def test_different_types_not_merged(self):
        result = ExtractionResult(
            nodes=[
                ExtractedNode(
                    name="project alpha",
                    type=NodeType.TASK,
                    properties={"content": "a"},
                ),
                ExtractedNode(
                    name="project alpha",
                    type=NodeType.EPISODE,
                    properties={"content": "b"},
                ),
            ],
            edges=[],
        )

        normalised = await normalize_nodes(result)
        assert len(normalised.nodes) == 2

    async def test_remaps_edge_endpoints(self):
        result = ExtractionResult(
            nodes=[
                ExtractedNode(name="alice smith", type=NodeType.PERSON, properties={}),
                ExtractedNode(name="alice smith", type=NodeType.PERSON, properties={}),
            ],
            edges=[
                ExtractedEdge(
                    source_node_id="alice smith",
                    source_type=NodeType.PERSON,
                    target_node_id="alice smith",
                    target_type=NodeType.PERSON,
                    type=EdgeType.RELATED_TO,
                ),
            ],
        )

        normalised = await normalize_nodes(result)
        assert normalised.edges[0].source_node_id == "alice smith"
        assert normalised.edges[0].target_node_id == "alice smith"

    async def test_empty_result(self):
        result = ExtractionResult()
        normalised = await normalize_nodes(result)
        assert normalised.nodes == []
        assert normalised.edges == []

    async def test_resolves_via_alias_match(self):
        """New node name matches a kept node's aliases."""
        result = ExtractionResult(
            nodes=[
                ExtractedNode(
                    name="alice",
                    type=NodeType.PERSON,
                    properties={"aliases": ["ali", "bob"]},
                ),
                ExtractedNode(
                    name="bob",
                    type=NodeType.PERSON,
                    properties={"email": "bob@x.com"},
                ),
            ],
            edges=[],
        )

        normalised = await normalize_nodes(result)
        person_nodes = [n for n in normalised.nodes if n.type == NodeType.PERSON]
        assert len(person_nodes) == 1
        assert person_nodes[0].name == "alice"
        assert "bob" in person_nodes[0].properties.get("aliases", [])

    async def test_resolves_via_reverse_alias_match(self):
        """New node's aliases contain a kept node's name."""
        result = ExtractionResult(
            nodes=[
                ExtractedNode(
                    name="robert",
                    type=NodeType.PERSON,
                    properties={},
                ),
                ExtractedNode(
                    name="bob",
                    type=NodeType.PERSON,
                    properties={"aliases": ["robert"]},
                ),
            ],
            edges=[],
        )

        normalised = await normalize_nodes(result)
        person_nodes = [n for n in normalised.nodes if n.type == NodeType.PERSON]
        assert len(person_nodes) == 1
        assert person_nodes[0].name == "robert"
        assert "bob" in person_nodes[0].properties.get("aliases", [])

    async def test_no_cross_type_alias_resolution(self):
        """Alias match should respect type boundaries."""
        result = ExtractionResult(
            nodes=[
                ExtractedNode(
                    name="alpha",
                    type=NodeType.TASK,
                    properties={"content": "task", "aliases": ["beta"]},
                ),
                ExtractedNode(
                    name="beta",
                    type=NodeType.EPISODE,
                    properties={"content": "episode"},
                ),
            ],
            edges=[],
        )

        normalised = await normalize_nodes(result)
        assert len(normalised.nodes) == 2

    async def test_alias_lists_merged_as_union(self):
        """Aliases from both nodes are unioned, non-canonical name added."""
        result = ExtractionResult(
            nodes=[
                ExtractedNode(
                    name="alice",
                    type=NodeType.PERSON,
                    properties={"aliases": ["ali"]},
                ),
                ExtractedNode(
                    name="alice",
                    type=NodeType.PERSON,
                    properties={"aliases": ["alice doe"]},
                ),
            ],
            edges=[],
        )

        normalised = await normalize_nodes(result)
        aliases = normalised.nodes[0].properties.get("aliases", [])
        assert "ali" in aliases
        assert "alice doe" in aliases

    async def test_edge_dedup_after_resolution(self):
        """Edges that become identical after node resolution are merged."""
        result = ExtractionResult(
            nodes=[
                ExtractedNode(name="alice", type=NodeType.PERSON, properties={}),
                ExtractedNode(name="alice", type=NodeType.PERSON, properties={}),
            ],
            edges=[
                ExtractedEdge(
                    source_node_id="alice",
                    source_type=NodeType.PERSON,
                    target_node_id="write code",
                    target_type=NodeType.TASK,
                    type=EdgeType.TODO,
                ),
                ExtractedEdge(
                    source_node_id="alice",
                    source_type=NodeType.PERSON,
                    target_node_id="write code",
                    target_type=NodeType.TASK,
                    type=EdgeType.TODO,
                ),
            ],
        )

        normalised = await normalize_nodes(result)
        assert len(normalised.edges) == 1

    async def test_normalize_without_db_params(self):
        """Calling without client/database still works (in-memory only)."""
        result = ExtractionResult(
            nodes=[
                ExtractedNode(name="alice", type=NodeType.PERSON, properties={}),
            ],
            edges=[],
        )

        normalised = await normalize_nodes(result)
        assert len(normalised.nodes) == 1


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
                    target_type=NodeType.TASK,
                    type=EdgeType.TODO,
                )
            ],
        )
        await upsert_graph_entries(
            result,
            source_document_id=PydanticObjectId(),
            database="test",
            client=client,
        )

        ops = _extract_pipeline_ops(collection.bulk_write)
        edge_op = ops[0]
        pipeline = edge_op._doc
        assert _find_slice_in_pipeline(pipeline, "sources") == _MAX_SOURCES
