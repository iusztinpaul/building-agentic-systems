from typing import Any

from pydantic import BaseModel, Field

from tree.entities.knowledge_graph import EdgeType, NodeType


# --- Node property schemas ---
# Each model defines the expected properties for a node type.
# Used for LLM prompt construction via .model_json_schema().


class DocumentProperties(BaseModel):
    """A source document (article, video, etc.) ingested into the system."""

    source_type: str = Field(description="Source platform (e.g., substack, youtube)")
    source_uri: str = Field(description="URI of the source document")
    date: str | None = Field(
        default=None, description="Publication date (ISO 8601 format)"
    )


class ChunkProperties(BaseModel):
    """A chunk of text extracted from a document."""

    source_type: str = Field(description="Source platform of the parent document")
    source_uri: str = Field(description="URI of the parent document")
    content: str = Field(description="Text content of the chunk")
    date: str | None = Field(
        default=None, description="Publication date of the parent document"
    )


class PersonProperties(BaseModel):
    """A person mentioned in or related to the content."""

    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative names, nicknames, or references to this person",
    )
    email: str | None = Field(default=None, description="Email address if known")


class TaskProperties(BaseModel):
    """A task, project, or actionable item associated with a person."""

    content: str = Field(description="Description of the task or project")
    date: str | None = Field(
        default=None,
        description="Due date or mentioned date (ISO 8601 format)",
    )


class EpisodeProperties(BaseModel):
    """A life or work episode experienced by a person."""

    content: str = Field(description="Description of the episode or experience")
    date: str | None = Field(
        default=None,
        description="When the episode occurred (ISO 8601 format)",
    )


class PreferenceProperties(BaseModel):
    """A preference, opinion, or pattern exhibited by a person."""

    content: str = Field(description="Description of the preference")


# --- Edge constraint ---


class EdgeConstraint(BaseModel):
    """Defines the valid source and target node types for an edge type."""

    source_type: NodeType
    target_type: NodeType
    description: str


# --- Registries ---

NODE_PROPERTIES: dict[NodeType, type[BaseModel]] = {
    NodeType.DOCUMENT: DocumentProperties,
    NodeType.CHUNK: ChunkProperties,
    NodeType.PERSON: PersonProperties,
    NodeType.TASK: TaskProperties,
    NodeType.EPISODE: EpisodeProperties,
    NodeType.PREFERENCE: PreferenceProperties,
}

EDGE_CONSTRAINTS: dict[EdgeType, list[EdgeConstraint]] = {
    EdgeType.PART_OF: [
        EdgeConstraint(
            source_type=NodeType.CHUNK,
            target_type=NodeType.DOCUMENT,
            description="Chunk belongs to a document",
        )
    ],
    EdgeType.NEXT: [
        EdgeConstraint(
            source_type=NodeType.CHUNK,
            target_type=NodeType.CHUNK,
            description="Sequential ordering between chunks of the same document",
        )
    ],
    EdgeType.MENTIONS: [
        EdgeConstraint(
            source_type=NodeType.DOCUMENT,
            target_type=NodeType.PERSON,
            description="Document mentions a person",
        )
    ],
    EdgeType.REFERENCED: [
        EdgeConstraint(
            source_type=NodeType.DOCUMENT,
            target_type=NodeType.DOCUMENT,
            description="Document references another document",
        )
    ],
    EdgeType.RELATED_TO: [
        EdgeConstraint(
            source_type=NodeType.PERSON,
            target_type=NodeType.PERSON,
            description="Two people are related or connected",
        )
    ],
    EdgeType.TODO: [
        EdgeConstraint(
            source_type=NodeType.PERSON,
            target_type=NodeType.TASK,
            description="Person has a task or project to do",
        )
    ],
    EdgeType.EXPERIENCED: [
        EdgeConstraint(
            source_type=NodeType.PERSON,
            target_type=NodeType.EPISODE,
            description="Person experienced a life or work episode",
        )
    ],
    EdgeType.HAS: [
        EdgeConstraint(
            source_type=NodeType.PERSON,
            target_type=NodeType.PREFERENCE,
            description="Person has a preference or opinion",
        )
    ],
    # SAME_AS applies to all four LLM-extractable self-pairs (PERSON↔PERSON,
    # TASK↔TASK, EPISODE↔EPISODE, PREFERENCE↔PREFERENCE). Emitted by the
    # resolver/dedup pipeline (#011), not by the LLM.
    EdgeType.SAME_AS: [
        EdgeConstraint(
            source_type=NodeType.PERSON,
            target_type=NodeType.PERSON,
            description="Two PERSON nodes refer to the same real-world entity",
        ),
        EdgeConstraint(
            source_type=NodeType.TASK,
            target_type=NodeType.TASK,
            description="Two TASK nodes refer to the same task",
        ),
        EdgeConstraint(
            source_type=NodeType.EPISODE,
            target_type=NodeType.EPISODE,
            description="Two EPISODE nodes refer to the same episode",
        ),
        EdgeConstraint(
            source_type=NodeType.PREFERENCE,
            target_type=NodeType.PREFERENCE,
            description="Two PREFERENCE nodes refer to the same preference",
        ),
    ],
}

# Node/edge types the LLM should extract (vs structural types created by pipeline code).
LLM_EXTRACTABLE_NODE_TYPES: set[NodeType] = {
    NodeType.PERSON,
    NodeType.TASK,
    NodeType.EPISODE,
    NodeType.PREFERENCE,
}

LLM_EXTRACTABLE_EDGE_TYPES: set[EdgeType] = {
    EdgeType.RELATED_TO,
    EdgeType.TODO,
    EdgeType.EXPERIENCED,
    EdgeType.HAS,
}

# Edges created deterministically by pipeline code (not LLM-extracted).
STRUCTURAL_EDGE_TYPES: set[EdgeType] = {
    EdgeType.PART_OF,
    EdgeType.NEXT,
    EdgeType.MENTIONS,
    EdgeType.REFERENCED,
    EdgeType.SAME_AS,
}


def get_ontology_schema() -> dict[str, Any]:
    """Build the ontology schema for LLM extraction prompts.

    Returns a dict describing extractable node types (with their property schemas)
    and edge types (with their source/target constraints).
    """

    node_types = {}
    for node_type in LLM_EXTRACTABLE_NODE_TYPES:
        props_cls = NODE_PROPERTIES[node_type]
        schema = props_cls.model_json_schema()
        node_types[node_type.value] = {
            "description": props_cls.__doc__ or "",
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
        }

    edge_types = {}
    for edge_type in LLM_EXTRACTABLE_EDGE_TYPES:
        # LLM-extractable edges each have a single (source_type, target_type)
        # constraint by design; we pick the first entry for the prompt.
        constraint = EDGE_CONSTRAINTS[edge_type][0]
        edge_types[edge_type.value] = {
            "source_type": constraint.source_type.value,
            "target_type": constraint.target_type.value,
            "description": constraint.description,
        }

    return {"node_types": node_types, "edge_types": edge_types}
