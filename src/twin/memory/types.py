from typing import Any

from pydantic import BaseModel, Field

from twin.entities.knowledge_graph import EdgeType, NodeType


class ExtractedNode(BaseModel):
    """A node extracted by the LLM (before persistence)."""

    name: str
    type: NodeType
    properties: dict[str, Any] = {}
    chunk_id: str = ""


class ExtractedEdge(BaseModel):
    """An edge extracted by the LLM (before persistence)."""

    source_node_id: str
    source_type: NodeType
    target_node_id: str
    target_type: NodeType
    type: EdgeType
    properties: dict[str, Any] = {}
    chunk_id: str = ""


class ExtractionResult(BaseModel):
    """Aggregated extraction output from one or more chunks."""

    nodes: list[ExtractedNode] = []
    edges: list[ExtractedEdge] = []

    def merge(self, other: "ExtractionResult") -> "ExtractionResult":
        """Combine two results (e.g. from different chunks)."""
        return ExtractionResult(
            nodes=self.nodes + other.nodes,
            edges=self.edges + other.edges,
        )


class QueryResult(BaseModel):
    """Result of a memory query: seed nodes, expanded nodes, and edges."""

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
