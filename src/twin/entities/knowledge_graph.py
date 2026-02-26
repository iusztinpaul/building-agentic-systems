from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from beanie import Document as BeanieDocument
from beanie import Indexed, PydanticObjectId
from pydantic import Field


# --- Enums ---


class NodeType(StrEnum):
    DOCUMENT = "document"
    CHUNK = "chunk"
    PERSON = "person"
    TASK = "task"
    EPISODE = "episode"
    PREFERENCE = "preference"


class EdgeType(StrEnum):
    PART_OF = "part_of"
    NEXT = "next"
    MENTIONS = "mentions"
    REFERENCED = "referenced"
    RELATED_TO = "related_to"
    TODO = "todo"
    EXPERIENCED = "experienced"
    HAS = "has"


# --- Log Collection (knowledge_graph_log) ---
# Immutable, append-only. Each entry is a single observation
# of a node or edge extracted from a specific chunk.


class KnowledgeGraphLogEntry(BeanieDocument):
    kind: Indexed(str)
    properties: dict[str, Any] = Field(default_factory=dict)
    source_document_id: Indexed(PydanticObjectId)
    chunk_id: str
    created_at: datetime

    class Settings:
        name = "knowledge_graph_log"
        is_root = True


class NodeLogEntry(KnowledgeGraphLogEntry):
    kind: Literal["node"] = "node"
    name: Indexed(str)
    type: NodeType


class EdgeLogEntry(KnowledgeGraphLogEntry):
    kind: Literal["edge"] = "edge"
    source_node_id: Indexed(str)
    source_type: NodeType
    target_node_id: Indexed(str)
    target_type: NodeType
    type: EdgeType


# --- Materialized Collection (knowledge_graph) ---
# Rebuilt from the log via aggregation pipeline with $out.
# Nodes and edges coexist with different _id types:
#   - Nodes: _id = entity name (str)
#   - Edges: _id = {source_node_id, target_node_id, type} (dict)
# Single model with optional fields since $out bypasses Beanie.


class KnowledgeGraphEntry(BeanieDocument):
    id: Any = None
    kind: Indexed(str)
    type: NodeType | EdgeType

    # Node fields
    properties: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] = Field(default_factory=list)

    # Edge fields
    source_node_id: str | None = None
    source_type: NodeType | None = None
    target_node_id: str | None = None
    target_type: NodeType | None = None

    # Provenance
    sources: list[PydanticObjectId] = Field(default_factory=list)

    # Timestamps
    created_at: datetime
    updated_at: datetime

    class Settings:
        name = "knowledge_graph"
