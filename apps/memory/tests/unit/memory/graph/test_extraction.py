from tree.entities.memory import EdgeType, NodeType
from tree.memory.graph.extraction import (
    _parse_extraction,
    build_structural_entries,
    extract_entities,
)
from tree.memory.rag.types import ChildChunk, ParentChunk
from tree.memory.types import ExtractionResult, ExtractedEdge, ExtractedNode
from tree.models.fake_model import FakeLLM


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


_SOURCE_URI = "https://example.com/article"


def _parents(*shape: int) -> list[ParentChunk]:
    """Build a hierarchy with ``shape[i]`` children under parent ``i``."""

    return [
        ParentChunk(
            index=parent_index,
            content=f"parent {parent_index}",
            children=[
                ChildChunk(index=child_index, content=f"child {child_index}")
                for child_index in range(n_children)
            ],
        )
        for parent_index, n_children in enumerate(shape)
    ]


def _build(
    parents: list[ParentChunk],
    reference_uris: list[str] | None = None,
) -> ExtractionResult:
    """Call build_structural_entries with sensible defaults."""

    return build_structural_entries(
        source_uri=_SOURCE_URI,
        parents=parents,
        reference_uris=reference_uris,
    )


class TestBuildStructuralEntries:
    def test_emits_no_nodes_because_the_rag_loader_owns_the_rows(self):
        result = _build(_parents(2, 2))

        assert result.nodes == []

    def test_part_of_edges_cover_both_levels(self):
        # 2 parents x 3 children: 6 child->parent hops + 2 parent->document.
        result = _build(_parents(3, 3))

        part_of = [e for e in result.edges if e.type == EdgeType.PART_OF]
        child_to_parent = [e for e in part_of if e.target_type == NodeType.CHUNK]
        parent_to_document = [e for e in part_of if e.target_type == NodeType.DOCUMENT]
        assert len(child_to_parent) == 6
        assert len(parent_to_document) == 2

    def test_part_of_endpoints_are_the_deterministic_row_names(self):
        result = _build(_parents(1))

        part_of = {(e.source_node_id, e.target_node_id) for e in result.edges}
        assert part_of == {
            (f"{_SOURCE_URI}#parent-0#child-0", f"{_SOURCE_URI}#parent-0"),
            (f"{_SOURCE_URI}#parent-0", _SOURCE_URI),
        }

    def test_next_edges_link_siblings_at_both_levels(self):
        # 3 parents x 2 children: 2 parent-level hops + 3 x 1 child-level hops.
        result = _build(_parents(2, 2, 2))

        next_edges = [e for e in result.edges if e.type == EdgeType.NEXT]
        assert len(next_edges) == 5

    def test_next_never_crosses_a_parent_boundary(self):
        result = _build(_parents(2, 2))

        next_edges = [e for e in result.edges if e.type == EdgeType.NEXT]
        child_hops = {
            (e.source_node_id, e.target_node_id)
            for e in next_edges
            if "#child-" in e.source_node_id
        }
        assert child_hops == {
            (f"{_SOURCE_URI}#parent-0#child-0", f"{_SOURCE_URI}#parent-0#child-1"),
            (f"{_SOURCE_URI}#parent-1#child-0", f"{_SOURCE_URI}#parent-1#child-1"),
        }

    def test_no_next_edge_for_a_single_parent_with_a_single_child(self):
        result = _build(_parents(1))

        assert [e for e in result.edges if e.type == EdgeType.NEXT] == []

    def test_emits_no_mentions_because_apply_writes_owns_them(self):
        result = _build(_parents(1))

        assert [e for e in result.edges if e.type == EdgeType.MENTIONS] == []

    def test_creates_referenced_edges(self):
        refs = ["https://ref1.com/article", "https://ref2.com/article"]

        result = _build(_parents(1), reference_uris=refs)

        referenced = [e for e in result.edges if e.type == EdgeType.REFERENCED]
        assert len(referenced) == 2
        assert {e.target_node_id for e in referenced} == set(refs)
        for edge in referenced:
            assert edge.source_type == NodeType.DOCUMENT
            assert edge.target_type == NodeType.DOCUMENT

    def test_no_referenced_edges_when_none(self):
        result = _build(_parents(1), reference_uris=None)

        assert [e for e in result.edges if e.type == EdgeType.REFERENCED] == []

    def test_empty_hierarchy_emits_nothing(self):
        result = _build([])

        assert result.nodes == []
        assert result.edges == []


# ---------------------------------------------------------------------------
# normalize_nodes — DELETED in #012. Behavior is now covered by the six-task
# pipeline (see ``tests/unit/memory/graph/test_pipeline.py``).
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
