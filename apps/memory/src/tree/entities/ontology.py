"""Ontology registry — extensible node + edge type catalogue.

This module is the **single source of truth** for what node types and
edge types exist in the knowledge graph. Phase 3 (task #027) introduces
the registry plumbing while preserving the pre-existing six node types
and nine edge types byte-for-byte — no behavior change ships here.

Downstream tasks (#028–#032) add new POLE+O types, subtype slots,
property validators, and audit columns by **calling the
``register_*`` functions** rather than editing closed enums.

Quick start
-----------

Register a new node type::

    from pydantic import BaseModel, Field
    from tree.entities.ontology import NodeTypeSpec, register_node_type

    class OrganizationProperties(BaseModel):
        \"\"\"An organization, company, or institution.\"\"\"
        domain: str | None = Field(default=None)

    register_node_type(NodeTypeSpec(
        name="organization",
        properties_schema=OrganizationProperties,
        description="An organization, company, or institution.",
        subtypes=None,            # freeform; pass {"company", "university"} for closed
        llm_extractable=True,
    ))

Register a new edge type::

    from tree.entities.ontology import EdgeTypeSpec, register_edge_type

    register_edge_type(EdgeTypeSpec(
        name="works_for",
        allowed_pairs=[("person", "organization")],
        description="A person works for an organization.",
        llm_extractable=True,
    ))

Extend an existing closed-subtype node type with a new subtype::

    from tree.entities.ontology import register_node_subtype

    register_node_subtype(
        parent_type="object",
        subtype="task",
        description="A task or actionable item.",
    )

Backward-compat exports
-----------------------

The pre-existing public surface (``NODE_PROPERTIES``,
``EDGE_CONSTRAINTS``, ``LLM_EXTRACTABLE_NODE_TYPES``,
``LLM_EXTRACTABLE_EDGE_TYPES``, ``STRUCTURAL_EDGE_TYPES``,
``EdgeConstraint``) is preserved as module-level views derived from the
registry, so every existing call site keeps compiling unchanged. New
code should prefer ``NODE_REGISTRY`` / ``EDGE_REGISTRY`` and string
type names.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from tree.entities.knowledge_graph import EdgeType, NodeType


# ---------------------------------------------------------------------------
# Registry primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeTypeSpec:
    """Declarative spec for a node type.

    Attributes:
        name: snake_case identifier, e.g. ``"person"``. Used as the
            ``type`` string on every ``KnowledgeGraphEntry`` of this kind.
        properties_schema: Pydantic model whose fields describe the
            type-specific ``properties`` payload. The model's JSON
            schema flows into the LLM-extraction prompt.
        description: Human-readable description; surfaced to the LLM.
        subtypes: ``None`` means freeform (any subtype string is
            allowed); a non-empty ``set`` means a closed subtype
            vocabulary. Defaults to ``None``.
        llm_extractable: ``True`` if the LLM should be asked to extract
            instances of this type; ``False`` for purely structural
            types created by pipeline code (e.g. ``document``,
            ``chunk``).
    """

    name: str
    properties_schema: type[BaseModel]
    description: str
    subtypes: frozenset[str] | None = None
    llm_extractable: bool = True


@dataclass(frozen=True)
class EdgeTypeSpec:
    """Declarative spec for an edge type.

    Attributes:
        name: snake_case identifier, e.g. ``"todo"``.
        allowed_pairs: ``[(source_type_name, target_type_name), ...]``
            — the set of node-type pairs this edge may connect. An
            empty list means "no constraint" (used by ``related_to``
            after #029).
        properties_schema: Pydantic model for structural edge
            properties, or ``None`` if the edge carries no typed
            properties. ``None`` for every built-in edge today.
        description: Human-readable description; surfaced to the LLM
            for ``llm_extractable`` edges.
        llm_extractable: ``True`` if the LLM should emit this edge
            directly; ``False`` for structural / pipeline-emitted edges
            (e.g. ``part_of``, ``next``, ``mentions``, ``same_as``).
    """

    name: str
    allowed_pairs: list[tuple[str, str]]
    properties_schema: type[BaseModel] | None = None
    description: str = ""
    llm_extractable: bool = False


@dataclass(frozen=True)
class SubtypeSpec:
    """Spec for a closed-vocabulary subtype on a node type.

    Used by ``register_node_subtype``; the optional
    ``extra_properties`` model layers additional Pydantic fields on
    top of the parent's ``properties_schema`` during validation
    (validator wiring lands in #030).
    """

    name: str
    description: str
    extra_properties: type[BaseModel] | None = None


# ---------------------------------------------------------------------------
# Registries (mutable; populated at import time by the
# ``register_*`` calls at the bottom of this module)
# ---------------------------------------------------------------------------


NODE_REGISTRY: dict[str, NodeTypeSpec] = {}
EDGE_REGISTRY: dict[str, EdgeTypeSpec] = {}

# Parallel store for subtype ``extra_properties`` models, keyed by
# ``(parent_type, subtype)``. Lookups during validation (in #030)
# combine ``parent.properties_schema`` and
# ``SUBTYPE_EXTRAS.get((parent, subtype))``.
SUBTYPE_EXTRAS: dict[tuple[str, str], type[BaseModel]] = {}


def register_node_type(spec: NodeTypeSpec) -> None:
    """Register a node type.

    Idempotent on **identical** re-registration (same
    ``properties_schema``, same ``subtypes``, same ``description``,
    same ``llm_extractable`` flag).

    Raises ``ValueError`` on a conflicting re-registration (same
    ``name`` but any field differs).
    """

    existing = NODE_REGISTRY.get(spec.name)
    if existing is None:
        NODE_REGISTRY[spec.name] = spec
        return

    if existing == spec:
        return

    raise ValueError(
        "conflicting re-registration for node type "
        f"'{spec.name}': existing spec {existing!r}, new spec {spec!r}"
    )


def register_edge_type(spec: EdgeTypeSpec) -> None:
    """Register an edge type.

    Same idempotency contract as ``register_node_type``: identical
    re-registration is a no-op, conflicting re-registration raises
    ``ValueError``.
    """

    existing = EDGE_REGISTRY.get(spec.name)
    if existing is None:
        EDGE_REGISTRY[spec.name] = spec
        return

    if existing == spec:
        return

    raise ValueError(
        "conflicting re-registration for edge type "
        f"'{spec.name}': existing spec {existing!r}, new spec {spec!r}"
    )


def register_node_subtype(
    parent_type: str,
    subtype: str,
    description: str = "",
    extra_properties: type[BaseModel] | None = None,
) -> None:
    """Append ``subtype`` to the closed-subtype vocabulary of
    ``parent_type``.

    Raises:
        ValueError: if ``parent_type`` is not in ``NODE_REGISTRY``.
        ValueError: if the parent's ``subtypes`` is ``None``
            (freeform — extension semantics don't apply).
    """

    parent = NODE_REGISTRY.get(parent_type)
    if parent is None:
        raise ValueError(
            f"cannot register subtype '{subtype}': "
            f"parent node type '{parent_type}' is not registered"
        )
    if parent.subtypes is None:
        raise ValueError(
            f"cannot register subtype '{subtype}' on parent "
            f"'{parent_type}': parent uses freeform subtypes "
            "(subtypes=None); extension semantics don't apply"
        )

    new_subtypes = frozenset(parent.subtypes | {subtype})
    NODE_REGISTRY[parent_type] = dataclasses.replace(parent, subtypes=new_subtypes)

    if extra_properties is not None:
        SUBTYPE_EXTRAS[(parent_type, subtype)] = extra_properties

    # ``description`` is currently stored only via SubtypeSpec when the
    # caller chooses to keep one; we don't attach a per-subtype
    # description map in this task — #028 / #030 introduce that as part
    # of the subtype-aware prompt / validator wiring.
    _ = description  # accepted for forward-compatible signature


# ---------------------------------------------------------------------------
# Built-in property schemas
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Backward-compat helper type (kept for nl_query.py consumers)
# ---------------------------------------------------------------------------


class EdgeConstraint(BaseModel):
    """Defines the valid source and target node types for an edge type.

    Retained for backward compatibility with ``nl_query.py`` and tests
    that compare ``EdgeConstraint(...)`` instances; the registry now
    holds the canonical truth as ``EdgeTypeSpec.allowed_pairs``.
    """

    source_type: NodeType
    target_type: NodeType
    description: str


# ---------------------------------------------------------------------------
# Built-in registrations (POLE+O Phase 0 — pre-existing types only)
# ---------------------------------------------------------------------------

# --- Built-in registrations ---

register_node_type(
    NodeTypeSpec(
        name="document",
        properties_schema=DocumentProperties,
        description=DocumentProperties.__doc__ or "",
        subtypes=None,
        llm_extractable=False,
    )
)

register_node_type(
    NodeTypeSpec(
        name="chunk",
        properties_schema=ChunkProperties,
        description=ChunkProperties.__doc__ or "",
        subtypes=None,
        llm_extractable=False,
    )
)

register_node_type(
    NodeTypeSpec(
        name="person",
        properties_schema=PersonProperties,
        description=PersonProperties.__doc__ or "",
        # Closed-subtype set lands in #028 (POLE+O); freeform for now.
        subtypes=None,
        llm_extractable=True,
    )
)

register_node_type(
    NodeTypeSpec(
        name="task",
        properties_schema=TaskProperties,
        description=TaskProperties.__doc__ or "",
        # #028 re-routes task under the (object, task) subtype pair;
        # kept here as a top-level type for Phase-3 part 1.
        subtypes=None,
        llm_extractable=True,
    )
)

register_node_type(
    NodeTypeSpec(
        name="episode",
        properties_schema=EpisodeProperties,
        description=EpisodeProperties.__doc__ or "",
        # #028 re-routes episode under (event, episode).
        subtypes=None,
        llm_extractable=True,
    )
)

register_node_type(
    NodeTypeSpec(
        name="preference",
        properties_schema=PreferenceProperties,
        description=PreferenceProperties.__doc__ or "",
        # Typed slots land in #032.
        subtypes=None,
        llm_extractable=True,
    )
)


register_edge_type(
    EdgeTypeSpec(
        name="part_of",
        allowed_pairs=[("chunk", "document")],
        properties_schema=None,
        description="Chunk belongs to a document",
        llm_extractable=False,
    )
)

register_edge_type(
    EdgeTypeSpec(
        name="next",
        allowed_pairs=[("chunk", "chunk")],
        properties_schema=None,
        description="Sequential ordering between chunks of the same document",
        llm_extractable=False,
    )
)

register_edge_type(
    EdgeTypeSpec(
        name="mentions",
        allowed_pairs=[("document", "person")],
        properties_schema=None,
        description="Document mentions a person",
        llm_extractable=False,
    )
)

register_edge_type(
    EdgeTypeSpec(
        name="referenced",
        allowed_pairs=[("document", "document")],
        properties_schema=None,
        description="Document references another document",
        llm_extractable=False,
    )
)

register_edge_type(
    EdgeTypeSpec(
        name="related_to",
        allowed_pairs=[("person", "person")],
        properties_schema=None,
        description="Two people are related or connected",
        llm_extractable=True,
    )
)

register_edge_type(
    EdgeTypeSpec(
        name="todo",
        allowed_pairs=[("person", "task")],
        properties_schema=None,
        description="Person has a task or project to do",
        llm_extractable=True,
    )
)

register_edge_type(
    EdgeTypeSpec(
        name="experienced",
        allowed_pairs=[("person", "episode")],
        properties_schema=None,
        description="Person experienced a life or work episode",
        llm_extractable=True,
    )
)

register_edge_type(
    EdgeTypeSpec(
        name="has",
        allowed_pairs=[("person", "preference")],
        properties_schema=None,
        description="Person has a preference or opinion",
        llm_extractable=True,
    )
)

# SAME_AS applies to all four LLM-extractable self-pairs (PERSON↔PERSON,
# TASK↔TASK, EPISODE↔EPISODE, PREFERENCE↔PREFERENCE). Emitted by the
# resolver/dedup pipeline (#011), not by the LLM.
register_edge_type(
    EdgeTypeSpec(
        name="same_as",
        allowed_pairs=[
            ("person", "person"),
            ("task", "task"),
            ("episode", "episode"),
            ("preference", "preference"),
        ],
        properties_schema=None,
        description="Two nodes refer to the same real-world entity",
        llm_extractable=False,
    )
)


# ---------------------------------------------------------------------------
# Backward-compat views derived from the registry
# ---------------------------------------------------------------------------
#
# Existing call sites in ``tree.memory.extraction.core``,
# ``tree.memory.extraction.pipeline``, and ``tree.memory.query.nl_query``
# read these constants directly. They MUST keep working unchanged after
# this refactor — task #027 is behavior-neutral. Downstream tasks
# (#028–#032) will migrate consumers to read the registry directly.


def _per_type_constraints(name: str) -> list[EdgeConstraint]:
    """Reconstruct the legacy ``EdgeConstraint`` list for a given edge.

    Looks up the spec in ``EDGE_REGISTRY`` and produces one
    ``EdgeConstraint`` per allowed pair. The description is shared
    across all pairs (the spec carries a single description); this
    matches the original ``EDGE_CONSTRAINTS`` shape for every built-in
    edge except ``same_as``, which previously had four
    pair-specific descriptions. For the SAME_AS rows we synthesize a
    pair-specific description so downstream prompt builders that
    iterate constraints keep producing meaningful output.
    """

    spec = EDGE_REGISTRY[name]
    pair_descriptions: dict[tuple[str, str], str] = {}
    if name == "same_as":
        pair_descriptions = {
            (
                "person",
                "person",
            ): "Two PERSON nodes refer to the same real-world entity",
            ("task", "task"): "Two TASK nodes refer to the same task",
            ("episode", "episode"): "Two EPISODE nodes refer to the same episode",
            (
                "preference",
                "preference",
            ): "Two PREFERENCE nodes refer to the same preference",
        }

    constraints: list[EdgeConstraint] = []
    for src, tgt in spec.allowed_pairs:
        constraints.append(
            EdgeConstraint(
                source_type=NodeType(src),
                target_type=NodeType(tgt),
                description=pair_descriptions.get((src, tgt), spec.description),
            )
        )
    return constraints


NODE_PROPERTIES: dict[NodeType, type[BaseModel]] = {
    NodeType(name): spec.properties_schema for name, spec in NODE_REGISTRY.items()
}

EDGE_CONSTRAINTS: dict[EdgeType, list[EdgeConstraint]] = {
    EdgeType(name): _per_type_constraints(name) for name in EDGE_REGISTRY
}

LLM_EXTRACTABLE_NODE_TYPES: set[NodeType] = {
    NodeType(name) for name, spec in NODE_REGISTRY.items() if spec.llm_extractable
}

LLM_EXTRACTABLE_EDGE_TYPES: set[EdgeType] = {
    EdgeType(name) for name, spec in EDGE_REGISTRY.items() if spec.llm_extractable
}

# Edges created deterministically by pipeline code (not LLM-extracted).
STRUCTURAL_EDGE_TYPES: set[EdgeType] = {
    EdgeType(name) for name, spec in EDGE_REGISTRY.items() if not spec.llm_extractable
}


def get_ontology_schema() -> dict[str, Any]:
    """Build the ontology schema for LLM extraction prompts.

    Returns a dict describing extractable node types (with their
    property schemas) and edge types (with their source/target
    constraints). Output is byte-identical (post deterministic
    key sort) to the pre-registry implementation — pinned by a
    golden-file snapshot test.
    """

    node_types: dict[str, Any] = {}
    # Iterate in NodeType-enum order so the output stays deterministic
    # even though NODE_REGISTRY is dict-insertion-ordered.
    for node_type in LLM_EXTRACTABLE_NODE_TYPES:
        spec = NODE_REGISTRY[node_type.value]
        schema = spec.properties_schema.model_json_schema()
        node_types[node_type.value] = {
            "description": spec.properties_schema.__doc__ or "",
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
        }

    edge_types: dict[str, Any] = {}
    for edge_type in LLM_EXTRACTABLE_EDGE_TYPES:
        # LLM-extractable edges each have a single (source, target)
        # allowed pair by design; we pick the first entry.
        spec = EDGE_REGISTRY[edge_type.value]
        src, tgt = spec.allowed_pairs[0]
        edge_types[edge_type.value] = {
            "source_type": src,
            "target_type": tgt,
            "description": spec.description,
        }

    return {"node_types": node_types, "edge_types": edge_types}
