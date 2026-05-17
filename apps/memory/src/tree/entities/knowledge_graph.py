from datetime import datetime
from enum import StrEnum
from typing import Any

from beanie import Document as BeanieDocument
from beanie import Indexed, PydanticObjectId
from pydantic import Field
from pymongo import IndexModel


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


def build_node_id(
    user_id: PydanticObjectId,
    node_type: NodeType,
    name: str,
) -> str:
    """Build a tenant-scoped node ``_id`` string: ``"{user_id}:{type}:{name}"``.

    Strict isolation per Phase-1 decision #1: cross-user collisions are
    impossible at the DB level. The indexed ``user_id`` field on the entry
    provides the fast read-path; this ``_id`` prefix is the correctness
    guarantee.

    ``user_id`` is a **required, positional** parameter — there is
    intentionally no default. Forgetting it is a type-checker error, never
    a silent runtime fallback (decision #6).
    """

    return f"{user_id}:{node_type}:{name}"


def build_edge_id(source_node_id: str, edge_type: EdgeType, target_node_id: str) -> str:
    """Build an edge ``_id`` string: ``"source|type|target"``.

    Edge ids carry no explicit ``user_id`` segment because both endpoint
    node ids already begin with ``"{user_id}:"`` (post-#018). Cross-user
    edges are impossible by construction — the resulting ``_id`` would
    mix two distinct tenant prefixes, and the indexed ``user_id`` field
    on the row pins the edge to exactly one tenant.
    """

    return f"{source_node_id}|{edge_type}|{target_node_id}"


# --- Single collection (knowledge_graph) ---
# Nodes and edges coexist with string _id values:
#   - Nodes: _id = "{user_id}:type:name" (str), e.g. "65f...:person:alice"
#   - Edges: _id = "source|type|target" (str), source/target carry the user prefix.
# Upserted directly during extraction (no separate log collection).


class KnowledgeGraphEntry(BeanieDocument):
    id: str
    # No standalone single-key index on ``user_id``: every compound
    # index in ``Settings.indexes`` below (and the dynamic indexes
    # created in :mod:`tree.memory.indexing.core`) leads with
    # ``user_id``, so tenant-scoped queries hit the index prefix
    # without a redundant single-key maintenance cost per row.
    user_id: PydanticObjectId
    kind: Indexed(str)  # type: ignore[valid-type]
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
        indexes = [
            # user_id-prepended compound indexes for fast filtered reads.
            # The dynamic indexes (kind_source_node, kind_target_node,
            # kind_embedding, canonical_name) created in
            # tree.memory.indexing.core get user_id prepended in #019 —
            # this declaration only covers the two static compound indexes
            # the entry model owns directly.
            IndexModel(
                [("user_id", 1), ("kind", 1), ("type", 1)],
                name="user_kind_type",
            ),
            IndexModel(
                [("user_id", 1), ("type", 1), ("name", 1)],
                name="user_type_name",
            ),
        ]
