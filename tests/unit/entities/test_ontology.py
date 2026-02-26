from twin.entities.knowledge_graph import EdgeType, NodeType
from twin.entities.ontology import (
    EDGE_CONSTRAINTS,
    LLM_EXTRACTABLE_EDGE_TYPES,
    LLM_EXTRACTABLE_NODE_TYPES,
    NODE_PROPERTIES,
    STRUCTURAL_EDGE_TYPES,
    get_ontology_schema,
)


class TestOntologyRegistries:
    def test_every_node_type_has_properties(self):
        for node_type in NodeType:
            assert node_type in NODE_PROPERTIES, f"Missing properties for {node_type}"

    def test_every_edge_type_has_constraint(self):
        for edge_type in EdgeType:
            assert edge_type in EDGE_CONSTRAINTS, f"Missing constraint for {edge_type}"

    def test_extractable_and_structural_edges_are_disjoint(self):
        overlap = LLM_EXTRACTABLE_EDGE_TYPES & STRUCTURAL_EDGE_TYPES
        assert overlap == set(), f"Overlap: {overlap}"

    def test_extractable_and_structural_edges_cover_all(self):
        combined = LLM_EXTRACTABLE_EDGE_TYPES | STRUCTURAL_EDGE_TYPES
        assert combined == set(EdgeType)

    def test_mentions_is_structural(self):
        assert EdgeType.MENTIONS in STRUCTURAL_EDGE_TYPES
        assert EdgeType.MENTIONS not in LLM_EXTRACTABLE_EDGE_TYPES


class TestGetOntologySchema:
    def test_returns_node_and_edge_types(self):
        schema = get_ontology_schema()

        assert "node_types" in schema
        assert "edge_types" in schema

    def test_only_extractable_nodes(self):
        schema = get_ontology_schema()
        node_keys = set(schema["node_types"].keys())
        expected = {nt.value for nt in LLM_EXTRACTABLE_NODE_TYPES}

        assert node_keys == expected

    def test_only_extractable_edges(self):
        schema = get_ontology_schema()
        edge_keys = set(schema["edge_types"].keys())
        expected = {et.value for et in LLM_EXTRACTABLE_EDGE_TYPES}

        assert edge_keys == expected

    def test_node_schema_has_properties(self):
        schema = get_ontology_schema()

        for node_type, info in schema["node_types"].items():
            assert "properties" in info, f"Missing properties for {node_type}"
            assert "description" in info

    def test_edge_schema_has_constraints(self):
        schema = get_ontology_schema()

        for edge_type, info in schema["edge_types"].items():
            assert "source_type" in info, f"Missing source_type for {edge_type}"
            assert "target_type" in info, f"Missing target_type for {edge_type}"
