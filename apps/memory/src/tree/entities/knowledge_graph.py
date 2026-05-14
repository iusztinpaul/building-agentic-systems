from datetime import datetime
from enum import StrEnum
from typing import Any

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
    SAME_AS = "same_as"


# --- ID builders ---


def build_node_id(node_type: NodeType, name: str) -> str:
    """Build a node ``_id`` string: ``"type:name"``."""
    return f"{node_type}:{name}"


def build_edge_id(source_node_id: str, edge_type: EdgeType, target_node_id: str) -> str:
    """Build an edge ``_id`` string: ``"source|type|target"``."""
    return f"{source_node_id}|{edge_type}|{target_node_id}"


# --- Single collection (knowledge_graph) ---
# Nodes and edges coexist with string _id values:
#   - Nodes: _id = "type:name" (str), e.g. "person:alice"
#   - Edges: _id = "source|type|target" (str), e.g. "person:alice|todo|task:write"
# Upserted directly during extraction (no separate log collection).


class KnowledgeGraphEntry(BeanieDocument):
    id: str
    kind: Indexed(str)
    type: NodeType | EdgeType

    # Node fields
    name: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] = Field(default_factory=list)

    # Resolution + dedup (node-only; edge rows keep documented defaults)
    canonical_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    merged_into: str | None = None
    merged_at: datetime | None = None

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
