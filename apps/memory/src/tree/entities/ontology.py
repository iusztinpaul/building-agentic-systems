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
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


@dataclass(frozen=True)
class RelationSemanticSpec:
    """Declarative spec for one ``related_to`` semantic (#029).

    Phase 3 #029 collapses the 14 POLE+O domain relations (plus Tree's
    two extensions ``has_task`` / ``experienced_by``) into a single
    ``related_to`` umbrella edge discriminated by a new
    ``semantic_type`` column. Each ``RelationSemanticSpec`` describes
    one such semantic.

    Attributes:
        name: snake_case identifier, e.g. ``"employed_by"``. Used as
            the ``semantic_type`` string on every ``related_to`` row.
        allowed_pairs: ``[(source_type_name, target_type_name), ...]``
            using POLE+O **parent** type names (never subtypes). A
            ``related_to`` edge with this ``semantic_type`` is only
            accepted when ``(source.type, target.type)`` is in this
            list.
        properties_schema: Pydantic model describing the per-semantic
            ``properties`` payload, or ``None`` when the semantic has
            no typed properties (e.g. ``knows`` / ``alias_of``).
        description: Human-readable description; surfaced to the LLM
            in the prompt assembled by :func:`get_ontology_schema`.
    """

    name: str
    allowed_pairs: list[tuple[str, str]] = field(default_factory=list)
    properties_schema: type[BaseModel] | None = None
    description: str = ""


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

# #029: per-semantic specs for the ``related_to`` umbrella edge. Keyed
# by ``semantic_type`` name (``"employed_by"``, ``"knows"`` ...).
RELATION_SEMANTICS: dict[str, RelationSemanticSpec] = {}


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


def register_relation_semantic(spec: RelationSemanticSpec) -> None:
    """Register a semantic for the ``related_to`` umbrella edge (#029).

    Idempotent on identical re-registration; raises ``ValueError`` on
    conflicting re-registration (same ``name`` but any field differs).
    """

    existing = RELATION_SEMANTICS.get(spec.name)
    if existing is None:
        RELATION_SEMANTICS[spec.name] = spec
        return

    if existing == spec:
        return

    raise ValueError(
        "conflicting re-registration for relation semantic "
        f"'{spec.name}': existing spec {existing!r}, new spec {spec!r}"
    )


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
    """An individual person mentioned in or related to the content."""

    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative names, nicknames, or references to this person",
    )
    email: str | None = Field(default=None, description="Email address if known")
    date_of_birth: str | None = Field(
        default=None,
        description="Date of birth in ISO 8601 format (YYYY-MM-DD)",
    )
    nationality: str | None = Field(
        default=None,
        description="Nationality or country of citizenship",
    )
    occupation: str | None = Field(
        default=None,
        description="Primary job, role, or profession",
    )


class OrganizationProperties(BaseModel):
    """An organization (company, nonprofit, government body, etc.)."""

    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative names, acronyms, or trade names for the organization",
    )
    jurisdiction: str | None = Field(
        default=None,
        description=(
            "Legal jurisdiction the organization is registered or operates in "
            "(e.g. 'Delaware, US', 'United Kingdom')"
        ),
    )
    registration_number: str | None = Field(
        default=None,
        description="Government or registry identifier (EIN, company number, etc.)",
    )


class LocationProperties(BaseModel):
    """A geographic or named location."""

    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative names, abbreviations, or translations of the location",
    )
    address: str | None = Field(
        default=None,
        description="Street address or postal form when applicable",
    )
    city: str | None = Field(default=None, description="City the location belongs to")
    country: str | None = Field(
        default=None,
        description="Country the location belongs to (use ISO 3166 short name when known)",
    )
    coordinates: str | None = Field(
        default=None,
        description=(
            "Decimal latitude/longitude pair, e.g. '37.7749,-122.4194'. "
            "Single string keeps the JSON-schema scalar-friendly for the LLM."
        ),
    )


class EventProperties(BaseModel):
    """An event (incident, meeting, transaction, communication, etc.)."""

    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative names or labels for the event",
    )
    date: str | None = Field(
        default=None,
        description="Date the event occurred (ISO 8601 YYYY-MM-DD)",
    )
    time: str | None = Field(
        default=None,
        description="Time the event occurred (HH:MM:SS, UTC unless otherwise stated)",
    )
    duration: str | None = Field(
        default=None,
        description="ISO 8601 duration (e.g. 'PT1H30M' for 1 hour 30 minutes)",
    )
    outcome: str | None = Field(
        default=None,
        description="Short summary of what happened or resulted from the event",
    )


class ObjectProperties(BaseModel):
    """A physical or digital object (vehicle, phone, document, device, etc.)."""

    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative names or labels for the object",
    )
    identifier: str | None = Field(
        default=None,
        description="External identifier (license plate, IMEI, URL, etc.)",
    )
    make: str | None = Field(
        default=None,
        description="Manufacturer or brand of the object",
    )
    model: str | None = Field(
        default=None,
        description="Model name or version of the object",
    )
    serial_number: str | None = Field(
        default=None,
        description="Serial number or other unique-per-instance identifier",
    )


# --- Retained legacy property schemas (kept importable by callers; the
# top-level ``task`` / ``episode`` registrations are removed in #028 in
# favor of subtype extensions on ``object`` / ``event``). ---


class TaskProperties(BaseModel):
    """A task, project, or actionable item associated with a person.

    **Deprecated** as a top-level POLE+O type after #028; ``task`` now
    lives as a Tree subtype under ``object`` (see
    :mod:`tree.entities.ontology_tree_extensions`). Kept as an
    importable schema so legacy call sites compile during the staging
    window between #028 and #033 (migration).
    """

    content: str = Field(description="Description of the task or project")
    date: str | None = Field(
        default=None,
        description="Due date or mentioned date (ISO 8601 format)",
    )


class EpisodeProperties(BaseModel):
    """A life or work episode experienced by a person.

    **Deprecated** as a top-level POLE+O type after #028; ``episode``
    now lives as a Tree subtype under ``event`` (see
    :mod:`tree.entities.ontology_tree_extensions`).
    """

    content: str = Field(description="Description of the episode or experience")
    date: str | None = Field(
        default=None,
        description="When the episode occurred (ISO 8601 format)",
    )


class PreferenceCategory(StrEnum):
    """Closed enum of preference categories (#032).

    Drives the supersession-candidate partition: a new preference only
    competes with prior preferences in the same ``(user_id, category)``
    slice. Adding a new category here is a schema change that requires
    regenerating the ontology prompt snapshot.
    """

    UI = "ui"
    LANGUAGE = "language"
    FOOD = "food"
    COMMUNICATION = "communication"
    WORK_STYLE = "work_style"
    TIME = "time"
    SOCIAL = "social"
    AESTHETIC = "aesthetic"
    OTHER = "other"


class PreferenceProperties(BaseModel):
    """A first-person preference, opinion, or pattern of the user (#032).

    Replaces the pre-#032 free-form ``content: str`` shape with typed
    slots so retrieval and review tooling can filter / partition
    preferences (e.g. "show me my UI preferences", "supersede this
    preference if I express a contradictory UI preference").

    Strict-mode policy (per `plan.md:461-465`): preferences attribute
    first-person opinions only. Third-party preferences ("Bob prefers
    vegetarian") are emitted as ``fact`` rows instead — never as
    ``preference`` rows.

    Migration: pre-#032 rows carry a ``content`` field and no
    ``statement`` / ``category``. The model accepts them at read time
    by re-mapping legacy ``content`` -> ``statement`` and defaulting
    ``category="other"``, so the pipeline never crashes on legacy
    rows. The canonical migration that rewrites every preference row
    lands in #033 (wipe-and-rebuild).
    """

    model_config = ConfigDict(populate_by_name=True)

    statement: str = Field(
        description=(
            "Short canonical preference statement, <=80 chars (e.g. "
            "'prefers dark mode'). The deterministic node ``name`` is "
            "derived from this string."
        ),
        max_length=80,
    )
    category: PreferenceCategory = Field(
        description=(
            "Closed-enum category - drives filter queries and the "
            "supersession-candidate partition (a new preference only "
            "competes with prior preferences in the same category)."
        ),
    )
    target: str | None = Field(
        default=None,
        description=(
            "What is preferred - a resolved entity name OR a free string "
            "for abstract concepts (e.g. 'dark mode', 'python', 'sushi')."
        ),
    )
    over: str | None = Field(
        default=None,
        description=(
            "What is dis-preferred when the preference is comparative "
            "(e.g. 'prefers dark mode OVER light mode')."
        ),
    )
    context: str | None = Field(
        default=None,
        description=(
            "When / where the preference applies (replaces graph-edge "
            "scoping). For example 'in editors' or 'at work'."
        ),
    )
    strength: Literal["weak", "moderate", "strong"] = Field(
        default="moderate",
        description=(
            "How strongly the user holds this preference. Drives review "
            "ordering and weights for downstream consumers."
        ),
    )


class SupersededByProperties(BaseModel):
    """Per-edge properties for the bi-temporal ``superseded_by`` edge (#032).

    Resolver-written, never LLM-emitted. The edge points from the NEW
    (winning) row to the OLD (superseded) row. The OLD row's
    ``valid_until`` is set to the same timestamp; the NEW row's
    ``valid_from`` is set to ``superseded_at``. Together those three
    writes form one atomic supersession.

    Generalises to both ``(preference, preference)`` and ``(fact, fact)``
    (same-type only - the spec pins same-type supersession).
    """

    superseded_at: datetime = Field(
        description=(
            "When the supersession was written (UTC, tz-aware). Mirrors "
            "the OLD row's ``valid_until`` and the NEW row's "
            "``valid_from``."
        )
    )
    reason: Literal["contradiction", "stale"] = Field(
        description=(
            "Why the supersession was written. 'contradiction' = the "
            "judge fired on two semantically opposing rows; 'stale' = "
            "explicit operator override (no judge call)."
        )
    )
    judge_confidence: float | None = Field(
        default=None,
        description=(
            "Confidence (0.0-1.0) from the contradiction-judge LLM "
            "call when ``reason == 'contradiction'``; None when reason "
            "is 'stale' or the judge didn't surface a confidence."
        ),
    )

    @field_validator("superseded_at", mode="after")
    @classmethod
    def _require_tz_aware(cls, value: datetime) -> datetime:
        """Reject naive datetimes - mirrors the
        :class:`KnowledgeGraphEntry` ``valid_from``/``valid_until`` rule."""

        if value.tzinfo is None:
            raise ValueError(
                "SupersededByProperties.superseded_at must be tz-aware "
                f"(UTC); got naive {value!r}"
            )
        return value


class FactProperties(BaseModel):
    """A free-form proposition that doesn't fit any typed entity relation.

    Facts (#031) are the LLM extraction escape-hatch for propositions
    whose subject or object doesn't resolve to a POLE+O entity worth
    traversing, OR whose relation doesn't match any registered
    ``related_to`` semantic. They are **island nodes** — the envelope
    validator rejects every edge with a ``fact`` endpoint, so a fact
    has zero edges to or from it. Retrieval is by ``name`` /
    ``subject`` / ``object`` string match or vector similarity only
    (see :class:`tree.memory.query.kgquery.KGQuery`).

    Bi-temporal columns ``valid_from`` / ``valid_until`` live on
    :class:`tree.entities.knowledge_graph.KnowledgeGraphEntry` and are
    populated at extraction time when the LLM emits them. Supersession
    of contradictory facts lands in #032; until then contradictory
    facts coexist and both surface in retrieval.

    The ``object`` field shadows the builtin :class:`object` type, so
    it's stored on the Python class as ``object_`` with a Pydantic
    ``alias="object"`` — the wire / JSON-schema / LLM-prompt key stays
    the plain string ``"object"``. ``populate_by_name=True`` lets
    construction work from either spelling.
    """

    model_config = ConfigDict(populate_by_name=True)

    subject: str = Field(
        description=(
            "The proposition's left side. Free-text OR a resolved entity "
            "name (no inverse lookup — facts are island nodes)."
        )
    )
    predicate: str = Field(
        description=(
            "The relation verb (e.g. 'prefers', 'lives_in', 'speaks'). "
            "If this fits one of the registered ``related_to`` semantics "
            "AND both endpoints resolve as entities, emit a ``related_to`` "
            "edge with that ``semantic_type`` instead."
        )
    )
    object_: str = Field(
        alias="object",
        description=(
            "The proposition's right side. Free-text OR a resolved entity name."
        ),
    )


# ---------------------------------------------------------------------------
# Phase-3 #029 — Structural edge property models
# ---------------------------------------------------------------------------
#
# Typed property schemas for the structural ``mentions`` / ``same_as``
# edges. Lenient field-level validation (drop a single bad property
# without rejecting the whole edge) lands in #030; these models exist
# now so the validator + LLM-prompt assembly can reference them.


class MentionsProperties(BaseModel):
    """Per-edge properties for the structural ``mentions`` edge (#029).

    Carried by every ``mentions`` edge from a chunk / document to a
    POLE+O entity. Defaults are deliberately permissive — extraction
    code may write only ``confidence`` or omit the positions entirely.
    """

    confidence: float = Field(
        default=1.0,
        description=(
            "Confidence the mention is a true reference, in [0.0, 1.0]. "
            "Pipeline-generated mentions default to 1.0; downstream "
            "extractors that emit weaker links can lower this."
        ),
    )
    start_pos: int | None = Field(
        default=None,
        description=(
            "Character offset of the mention's start within the chunk's "
            "text, when known. None when the extractor cannot locate the "
            "mention precisely."
        ),
    )
    end_pos: int | None = Field(
        default=None,
        description=(
            "Character offset of the mention's end within the chunk's text, when known."
        ),
    )


class SameAsMatchType(StrEnum):
    """How the dedup pipeline decided two nodes are the same entity.

    Pinned as a closed enum so downstream consumers (review UI, audit
    queries) can switch-statement on the value without string churn.
    """

    EMBEDDING = "embedding"
    FUZZY = "fuzzy"
    BOTH = "both"


class SameAsStatus(StrEnum):
    """Review lifecycle for a SAME_AS audit edge.

    ``PENDING`` rows surface in the human-review queue; ``CONFIRMED``
    rows have been merged; ``REJECTED`` rows are skipped on future
    dedup passes (see ``tree.memory.extraction.dedup``'s reject-pair
    filter).
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class SameAsProperties(BaseModel):
    """Per-edge properties for the structural ``same_as`` edge (#029).

    Schema mirrors the dict-shaped payload the dedup pipeline (#010)
    and the human-review surface (#014) already write today; pinning
    it as a Pydantic model gives the #030 validator a concrete
    target.
    """

    confidence: float = Field(
        default=1.0,
        description=(
            "Best-scoring similarity (raw cosine for embedding, RapidFuzz "
            "ratio in [0,1] for fuzzy, average of the two for ``both``). "
            "Drives the review-queue sort order."
        ),
    )
    match_type: SameAsMatchType = Field(
        default=SameAsMatchType.EMBEDDING,
        description=(
            "Which dedup branch produced the candidate: embedding-only, "
            "fuzzy-only, or both."
        ),
    )
    status: SameAsStatus = Field(
        default=SameAsStatus.PENDING,
        description=(
            "Review lifecycle. ``pending`` rows show up in the human-review "
            "queue; ``confirmed`` rows have been merged; ``rejected`` rows "
            "are excluded from future dedup passes."
        ),
    )


# ---------------------------------------------------------------------------
# Phase-3 #029 — Per-semantic property models for ``related_to``
# ---------------------------------------------------------------------------
#
# One ``*Properties`` model per ``RelationSemanticSpec`` that carries
# typed properties. The semantic table at ``plan.md:227-242`` lists
# each spec's properties verbatim — the models below mirror that
# table. Semantics with no per-spec properties (``knows``,
# ``alias_of``, ``headquarters_at``, ``occurred_at``, ``subsidiary_of``,
# ``partner_with``, ``uses``) carry ``properties_schema=None``.


class _EmploymentRoleDates(BaseModel):
    """Shared shape for ``member_of`` / ``employed_by``.

    Both semantics carry ``role`` + ``start_date`` / ``end_date``;
    storing them as a base class keeps the model definitions DRY while
    each consumer-facing class still has its own docstring.
    """

    role: str | None = Field(
        default=None,
        description=(
            "Role or title held during the relationship (e.g. 'senior "
            "engineer', 'board member')."
        ),
    )
    start_date: str | None = Field(
        default=None,
        description=(
            "Start of the relationship in ISO 8601 (YYYY-MM-DD), or None "
            "when the start is unknown / open-ended."
        ),
    )
    end_date: str | None = Field(
        default=None,
        description=(
            "End of the relationship in ISO 8601 (YYYY-MM-DD), or None "
            "when the relationship is ongoing or the end is unknown."
        ),
    )


class MemberOfProperties(_EmploymentRoleDates):
    """Properties for ``related_to + semantic_type=member_of``."""


class EmployedByProperties(_EmploymentRoleDates):
    """Properties for ``related_to + semantic_type=employed_by``."""


class OwnsProperties(BaseModel):
    """Properties for ``related_to + semantic_type=owns``."""

    acquisition_date: str | None = Field(
        default=None,
        description=(
            "Date of ownership acquisition in ISO 8601 (YYYY-MM-DD), or "
            "None when the date is unknown."
        ),
    )


class _DateRangeProperties(BaseModel):
    """Shared shape for ``located_at`` / ``resides_at`` — both carry an
    optional ``from_date`` / ``to_date`` pair."""

    from_date: str | None = Field(
        default=None,
        description=(
            "Start of the period the relation held, in ISO 8601 (YYYY-MM-DD)."
        ),
    )
    to_date: str | None = Field(
        default=None,
        description=(
            "End of the period the relation held, in ISO 8601 "
            "(YYYY-MM-DD), or None when the relation is current / "
            "unknown."
        ),
    )


class LocatedAtProperties(_DateRangeProperties):
    """Properties for ``related_to + semantic_type=located_at``."""


class ResidesAtProperties(_DateRangeProperties):
    """Properties for ``related_to + semantic_type=resides_at``."""


class ParticipatedInProperties(BaseModel):
    """Properties for ``related_to + semantic_type=participated_in``."""

    role: str | None = Field(
        default=None,
        description=(
            "Role the participant played in the event (e.g. 'speaker', "
            "'attendee', 'organizer')."
        ),
    )


class InvolvedProperties(BaseModel):
    """Properties for ``related_to + semantic_type=involved`` (object → event)."""

    role: str | None = Field(
        default=None,
        description=(
            "Role the object played in the event (e.g. 'weapon', "
            "'evidence', 'collateral')."
        ),
    )


class HasTaskProperties(BaseModel):
    """Properties for the Tree extension ``has_task`` (person → object[task]).

    Tree-only extension that re-routes the legacy ``EdgeType.TODO``
    semantics into the POLE+O umbrella. Carries ``status`` so MCP /
    UI consumers can filter ``pending`` vs ``done`` tasks without
    cracking the object subtype.
    """

    status: str | None = Field(
        default=None,
        description=(
            "Lifecycle status of the task — e.g. 'pending', 'in_progress', "
            "'done', 'cancelled'. None means unspecified."
        ),
    )


class ExperiencedByProperties(BaseModel):
    """Properties for the Tree extension ``experienced_by`` (person → event).

    Tree-only extension that re-routes the legacy ``EdgeType.EXPERIENCED``
    semantics into the POLE+O umbrella. The optional ``role`` mirrors
    ``ParticipatedInProperties.role`` for shape-symmetry.
    """

    role: str | None = Field(
        default=None,
        description=(
            "Role the person played in the event (e.g. 'protagonist', "
            "'witness'). None when unspecified."
        ),
    )


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
        # #028: closed POLE+O subtype set.
        subtypes=frozenset({"individual", "alias", "persona"}),
        llm_extractable=True,
    )
)

register_node_type(
    NodeTypeSpec(
        name="organization",
        properties_schema=OrganizationProperties,
        description=OrganizationProperties.__doc__ or "",
        subtypes=frozenset(
            {
                "company",
                "nonprofit",
                "government",
                "educational",
                "political",
                "religious",
                "military",
            }
        ),
        llm_extractable=True,
    )
)

register_node_type(
    NodeTypeSpec(
        name="location",
        properties_schema=LocationProperties,
        description=LocationProperties.__doc__ or "",
        subtypes=frozenset(
            {"address", "city", "region", "country", "landmark", "coordinates"}
        ),
        llm_extractable=True,
    )
)

register_node_type(
    NodeTypeSpec(
        name="event",
        properties_schema=EventProperties,
        description=EventProperties.__doc__ or "",
        # Canonical POLE+O subtypes only; Tree's ``episode`` extension
        # is registered downstream in ``ontology_tree_extensions``.
        subtypes=frozenset(
            {
                "incident",
                "meeting",
                "transaction",
                "communication",
                "travel",
                "employment",
                "observation",
            }
        ),
        llm_extractable=True,
    )
)

register_node_type(
    NodeTypeSpec(
        name="object",
        properties_schema=ObjectProperties,
        description=ObjectProperties.__doc__ or "",
        # Canonical POLE+O subtypes only; Tree's ``task`` / ``topic`` /
        # ``project`` extensions are registered downstream.
        subtypes=frozenset(
            {"vehicle", "phone", "email", "document", "device", "software"}
        ),
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

# #031: ``fact`` is the LLM-extractable escape-hatch node type for
# propositions that don't fit any registered relation semantic. Island
# rule: the envelope validator rejects every edge whose source or
# target is a ``fact`` row (see ``_FORBIDDEN_EDGE_ENDPOINT_TYPES`` in
# :mod:`tree.memory.extraction.validation`). Subtypes are ``None``
# (freeform — facts are not categorized).
register_node_type(
    NodeTypeSpec(
        name="fact",
        properties_schema=FactProperties,
        description=FactProperties.__doc__ or "",
        subtypes=None,
        llm_extractable=True,
    )
)


# ---------------------------------------------------------------------------
# #029 — Relation-semantics catalogue (16 entries)
# ---------------------------------------------------------------------------
#
# 14 canonical POLE+O semantics (``plan.md:227-242``) plus 2 Tree
# extensions (``has_task``, ``experienced_by``). Registered BEFORE the
# umbrella ``related_to`` edge so its ``allowed_pairs`` can be
# computed as the union of every spec's pairs.

register_relation_semantic(
    RelationSemanticSpec(
        name="knows",
        allowed_pairs=[("person", "person")],
        properties_schema=None,
        description="Two persons know each other (acquaintance-grade link).",
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="member_of",
        allowed_pairs=[("person", "organization")],
        properties_schema=MemberOfProperties,
        description=(
            "Person is a member of an organization (board, club, team). "
            "Carries role + date range."
        ),
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="employed_by",
        allowed_pairs=[("person", "organization")],
        properties_schema=EmployedByProperties,
        description=(
            "Person is employed by an organization. Carries role + date "
            "range; the canonical Tree subtype for the resulting period "
            "is event/employment."
        ),
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="owns",
        allowed_pairs=[
            ("person", "object"),
            ("organization", "object"),
        ],
        properties_schema=OwnsProperties,
        description="A person or organization owns an object.",
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="uses",
        allowed_pairs=[
            ("person", "object"),
            ("organization", "object"),
        ],
        properties_schema=None,
        description="A person or organization uses an object (no transfer of ownership).",
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="located_at",
        allowed_pairs=[
            ("object", "location"),
            ("event", "location"),
        ],
        properties_schema=LocatedAtProperties,
        description=(
            "An object or event is located at a place (with optional date "
            "range so the relation can describe historical placement)."
        ),
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="resides_at",
        allowed_pairs=[("person", "location")],
        properties_schema=ResidesAtProperties,
        description="A person resides at a location, with optional date range.",
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="headquarters_at",
        allowed_pairs=[("organization", "location")],
        properties_schema=None,
        description="An organization's headquarters is at a location.",
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="participated_in",
        allowed_pairs=[
            ("person", "event"),
            ("organization", "event"),
        ],
        properties_schema=ParticipatedInProperties,
        description="A person or organization participated in an event.",
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="occurred_at",
        allowed_pairs=[("event", "location")],
        properties_schema=None,
        description="An event occurred at a location (point-in-time anchor).",
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="involved",
        allowed_pairs=[("object", "event")],
        properties_schema=InvolvedProperties,
        description="An object was involved in an event (e.g. weapon, evidence).",
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="subsidiary_of",
        allowed_pairs=[("organization", "organization")],
        properties_schema=None,
        description="An organization is a subsidiary of another organization.",
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="partner_with",
        allowed_pairs=[("organization", "organization")],
        properties_schema=None,
        description="Two organizations are in a partnership relation.",
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="alias_of",
        allowed_pairs=[
            ("person", "person"),
            ("organization", "organization"),
            ("location", "location"),
            ("event", "event"),
            ("object", "object"),
        ],
        properties_schema=None,
        description=(
            "Surface form is an alias for the target. Distinct from "
            "structural ``same_as`` (which is the dedup-confirmed merge "
            "edge) — ``alias_of`` is LLM-extractable narrative."
        ),
    )
)

# --- Tree extensions (per the task spec). Not canonical POLE+O. ---

register_relation_semantic(
    RelationSemanticSpec(
        name="has_task",
        allowed_pairs=[("person", "object")],
        properties_schema=HasTaskProperties,
        description=(
            "Tree extension: a person has a task (object with subtype "
            "``task``). Re-routes the legacy ``EdgeType.TODO`` semantics "
            "into the POLE+O umbrella so the LLM emits a single "
            "``related_to`` edge type."
        ),
    )
)
register_relation_semantic(
    RelationSemanticSpec(
        name="experienced_by",
        allowed_pairs=[("person", "event")],
        properties_schema=ExperiencedByProperties,
        description=(
            "Tree extension: a person experienced an event (commonly an "
            "event with subtype ``episode``). Re-routes the legacy "
            "``EdgeType.EXPERIENCED`` semantics."
        ),
    )
)


# ---------------------------------------------------------------------------
# Built-in edge registrations
# ---------------------------------------------------------------------------


# Compute the union of every semantic's allowed_pairs for ``related_to``.
# Deduplicated + sorted so the registry view is stable across runs.
def _related_to_allowed_pairs() -> list[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for spec in RELATION_SEMANTICS.values():
        pairs.update(spec.allowed_pairs)
    return sorted(pairs)


# POLE+O LLM-extractable node types — the targets that ``mentions`` and
# ``same_as`` accept after #029's broadening. Derived from the registry
# (rather than hard-coded) so a future #031 ``fact`` registration that
# adds ``llm_extractable=True`` would NOT silently slip into ``mentions``
# — the carve-out below is explicit.
def _pole_o_llm_extractable_for_mentions() -> list[str]:
    """Targets the ``mentions`` edge accepts (carve-out per ``plan.md:479``).

    ``preference`` is excluded because preferences attach only to
    ``person:self`` via the structural ``has`` edge, not via chunk
    mentions. ``fact`` (lands in #031) is also excluded — encoding the
    carve-out now keeps #031's surface area small.
    """

    out: list[str] = []
    for name in sorted(NODE_REGISTRY):
        spec = NODE_REGISTRY[name]
        if not spec.llm_extractable:
            continue
        if name in {"preference", "fact"}:
            continue
        out.append(name)
    return out


def _pole_o_llm_extractable_for_same_as() -> list[str]:
    """Source/target type for ``same_as`` (self-pair only).

    Includes ``preference`` (a confirmed merge between two preference
    rows is a legitimate dedup outcome) but excludes ``fact`` (island
    rule per #031). All other LLM-extractable POLE+O types qualify.
    """

    out: list[str] = []
    for name in sorted(NODE_REGISTRY):
        spec = NODE_REGISTRY[name]
        if not spec.llm_extractable:
            continue
        if name == "fact":
            continue
        out.append(name)
    return out


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

# #029: ``mentions`` is broadened from (document, person) to
# {chunk, document} → every POLE+O LLM-extractable type EXCEPT
# preference (carve-out per ``plan.md:479``).
register_edge_type(
    EdgeTypeSpec(
        name="mentions",
        allowed_pairs=[
            (src, tgt)
            for src in ("chunk", "document")
            for tgt in _pole_o_llm_extractable_for_mentions()
        ],
        properties_schema=MentionsProperties,
        description="A chunk or document mentions a POLE+O entity (except preference).",
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

# #029: ``related_to`` is now the LLM-extractable umbrella edge. Its
# allowed_pairs are the union of every RELATION_SEMANTICS spec's
# allowed_pairs. The per-semantic constraint (``semantic_type`` ∈ registry
# AND (source.type, target.type) ∈ spec.allowed_pairs) is enforced by
# the ``KnowledgeGraphEntry`` model validator.
register_edge_type(
    EdgeTypeSpec(
        name="related_to",
        allowed_pairs=_related_to_allowed_pairs(),
        properties_schema=None,
        description=(
            "Umbrella edge for the 14 POLE+O domain relations (+ Tree "
            "extensions). Discriminated by ``semantic_type``."
        ),
        llm_extractable=True,
    )
)

# #029: ``has`` survives as a STRUCTURAL edge (pipeline-emitted, not
# LLM-extractable) for ``person:self`` attachments only. Today's
# constraint covers (person, preference); the spec also calls for
# broadening to (person, object) so the deterministic pipeline can
# write "self has a task" without re-shaping it as a ``related_to``
# row. The LLM never emits this edge.
register_edge_type(
    EdgeTypeSpec(
        name="has",
        allowed_pairs=[
            ("person", "preference"),
            ("person", "object"),
        ],
        properties_schema=None,
        description=(
            "Structural attachment from ``person:self`` to a preference "
            "or a Tree task (object/task). Pipeline-emitted, never "
            "LLM-extractable."
        ),
        llm_extractable=False,
    )
)

# #029: ``same_as`` broadened from the legacy four-type set to every
# POLE+O LLM-extractable type (excluding ``fact``), source==target.
register_edge_type(
    EdgeTypeSpec(
        name="same_as",
        allowed_pairs=[(t, t) for t in _pole_o_llm_extractable_for_same_as()],
        properties_schema=SameAsProperties,
        description="Two nodes of the same POLE+O type refer to the same real-world entity",
        llm_extractable=False,
    )
)

# #032: ``superseded_by`` is the bi-temporal supersession edge. The NEW
# (winning) row points at the OLD (superseded) row; the OLD row's
# ``valid_until`` is set to the same instant the NEW row's
# ``valid_from`` carries. The edge is resolver-written -
# ``llm_extractable=False`` - and intentionally same-type only:
# ``(preference, preference)`` for the canonical user-preference
# supersession path and ``(fact, fact)`` for contradictory propositions
# (per ``plan.md:557``). Cross-type chains (e.g. preference -> fact)
# are intentionally rejected at the envelope: contradictions only
# make sense within a single row type.
register_edge_type(
    EdgeTypeSpec(
        name="superseded_by",
        allowed_pairs=[("preference", "preference"), ("fact", "fact")],
        properties_schema=SupersededByProperties,
        description=(
            "Bi-temporal supersession edge. Newer row points at the row "
            "it replaced. Resolver-written; same-type only "
            "(preference->preference or fact->fact)."
        ),
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
    edge. The SAME_AS pair-specific descriptions kept for legacy
    callers; pair endpoints that aren't part of the active enum
    (``NodeType``) are skipped so the back-compat view never raises.
    """

    spec = EDGE_REGISTRY[name]

    constraints: list[EdgeConstraint] = []
    for src, tgt in spec.allowed_pairs:
        try:
            src_enum = NodeType(src)
            tgt_enum = NodeType(tgt)
        except ValueError:
            # A registered pair references a node type not in the
            # backward-compat enum (e.g. a future ``fact`` type). Skip
            # — back-compat callers iterate the enum, so they only need
            # entries the enum can express.
            continue
        constraints.append(
            EdgeConstraint(
                source_type=src_enum,
                target_type=tgt_enum,
                description=spec.description,
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


def _edge_type_safe(name: str) -> EdgeType | None:
    """Best-effort lookup against the back-compat enum.

    Returns ``None`` when the registered edge isn't in ``EdgeType`` —
    which happens transiently if a downstream consumer registers a
    new edge before the next ``EdgeType`` shim refresh. Today every
    built-in registration has a matching enum member.
    """

    try:
        return EdgeType(name)
    except ValueError:
        return None


LLM_EXTRACTABLE_EDGE_TYPES: set[EdgeType] = {
    et
    for name, spec in EDGE_REGISTRY.items()
    if spec.llm_extractable and (et := _edge_type_safe(name)) is not None
}

# Edges created deterministically by pipeline code (not LLM-extracted).
STRUCTURAL_EDGE_TYPES: set[EdgeType] = {
    et
    for name, spec in EDGE_REGISTRY.items()
    if not spec.llm_extractable and (et := _edge_type_safe(name)) is not None
}


def get_ontology_schema() -> dict[str, Any]:
    """Build the ontology schema for LLM extraction prompts.

    Returns a dict describing extractable node types (with their
    property schemas + closed subtype vocabularies) and edge types
    (with their source/target constraints). Iteration is deterministic
    (alphabetical by name) so the generated prompt is byte-stable across
    Python runs — pinned by a golden-file snapshot test.

    Phase-3 #028: each LLM-extractable node type now also surfaces its
    ``subtypes`` list when the registry pins a closed vocabulary; the
    LLM is expected to emit a ``subtype`` field alongside ``type`` and
    ``name``. Types with ``subtypes=None`` (freeform — e.g. legacy
    ``preference``) omit the ``subtypes`` key, signalling the LLM that
    subtype is optional and freeform.
    """

    node_types: dict[str, Any] = {}
    # Sort alphabetically by name for deterministic prompt output.
    # Prior to #028 this iterated a Python ``set`` (non-deterministic
    # ordering across hash-randomized runs); the Tester surfaced that
    # while reviewing #027.
    extractable_node_names = sorted(
        name for name, spec in NODE_REGISTRY.items() if spec.llm_extractable
    )
    for name in extractable_node_names:
        spec = NODE_REGISTRY[name]
        schema = spec.properties_schema.model_json_schema()
        node_info: dict[str, Any] = {
            "description": spec.properties_schema.__doc__ or "",
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
        }
        if spec.subtypes is not None:
            # Sorted for deterministic prompt; an empty set still surfaces
            # so the LLM knows the type accepts no subtypes today.
            node_info["subtypes"] = sorted(spec.subtypes)
        node_types[name] = node_info

    edge_types: dict[str, Any] = {}
    extractable_edge_names = sorted(
        name for name, spec in EDGE_REGISTRY.items() if spec.llm_extractable
    )
    for name in extractable_edge_names:
        spec = EDGE_REGISTRY[name]
        edge_info: dict[str, Any] = {
            "description": spec.description,
            "allowed_pairs": [list(pair) for pair in sorted(spec.allowed_pairs)],
        }
        # #029: the ``related_to`` umbrella edge surfaces its per-
        # semantic catalogue so the LLM emits
        # ``{"type": "related_to", "semantic_type": "...", ...}``.
        if name == "related_to":
            semantic_types: dict[str, Any] = {}
            for semantic_name in sorted(RELATION_SEMANTICS):
                rs = RELATION_SEMANTICS[semantic_name]
                semantic_info: dict[str, Any] = {
                    "description": rs.description,
                    "allowed_pairs": [list(pair) for pair in sorted(rs.allowed_pairs)],
                }
                if rs.properties_schema is not None:
                    schema = rs.properties_schema.model_json_schema()
                    semantic_info["properties"] = schema.get("properties", {})
                    semantic_info["required"] = schema.get("required", [])
                semantic_types[semantic_name] = semantic_info
            edge_info["semantic_types"] = semantic_types
        edge_types[name] = edge_info

    # #030: surface the row-level common columns to the LLM so it
    # knows it can emit ``description`` / ``valid_from`` / ``valid_until``
    # alongside the type-specific ``properties`` payload. The
    # ``extractor`` column is **server-stamped** by the extraction
    # pipeline, so the LLM is told not to emit it.
    common_fields = {
        "description": {
            "type": "string",
            "optional": True,
            "description": (
                "Optional human-readable label for the row, surfaced in "
                "UIs and preview prompts."
            ),
        },
        "valid_from": {
            "type": "string",
            "optional": True,
            "description": (
                "ISO 8601 timestamp the row's validity period begins, "
                "or null when unknown."
            ),
        },
        "valid_until": {
            "type": "string",
            "optional": True,
            "description": (
                "ISO 8601 timestamp the row's validity period ends, or "
                "null when the row is still current / unknown."
            ),
        },
    }
    return {
        "node_types": node_types,
        "edge_types": edge_types,
        "common_fields": common_fields,
    }


# ---------------------------------------------------------------------------
# Tree downstream subtype extensions (self-application of the extension API)
# ---------------------------------------------------------------------------
#
# Imported here at the BOTTOM of the canonical ontology module so the
# canonical POLE+O parents (``object`` / ``event``) are already in
# :data:`NODE_REGISTRY` when ``register_node_subtype`` is called. The
# extensions module is intentionally side-effecting — see its docstring.
#
# We intentionally do **not** re-export anything from it; consumers that
# need the Tree-specific Pydantic shells (``ProjectExtras`` /
# ``ExternalRef``) import them from
# :mod:`tree.entities.ontology_tree_extensions` directly.
from tree.entities import ontology_tree_extensions as _tree_extensions  # noqa: E402, F401
