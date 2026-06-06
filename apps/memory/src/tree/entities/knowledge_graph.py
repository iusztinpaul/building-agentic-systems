from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from beanie import Document as BeanieDocument
from beanie import Indexed, PydanticObjectId
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import IndexModel


# --- Enums (backward-compat shims) ---
#
# ``NodeType`` and ``EdgeType`` were the closed enums shipped before
# Phase-3 ontology registry (task #027). They now live as **thin
# re-export shims** over ``tree.entities.ontology.NODE_REGISTRY`` /
# ``EDGE_REGISTRY``. Every existing call site that imports
# ``NodeType.PERSON`` keeps working — the enum members map 1:1 to the
# registered type names. New code should reference type names as
# strings or pull from the registry directly.
#
# Deletion target: once #028–#032 land and the downstream call sites
# migrate to string type names.
#
# These enums stay declared here (rather than being dynamically built
# from the registry at import time) to avoid an import cycle:
# ``tree.entities.ontology`` imports ``NodeType`` / ``EdgeType``
# from this module, and a registry-driven definition would have to
# live downstream of that import. A drift check is asserted as a unit
# test (``test_ontology.py::TestEnumShim``).


class NodeType(StrEnum):
    """Backward-compat shim built from ``NODE_REGISTRY`` (#027, extended #028).

    Members map 1:1 to registered node-type names — **except** ``TASK``,
    which is retained as a **legacy alias** after #028. The string
    ``"task"`` is no longer registered as a top-level node type; it's
    re-routed by :class:`KnowledgeGraphEntry`'s ``mode="before"``
    validator to ``(type="object", subtype="task")``. Reading the enum
    member in consumer code still works (StrEnum -> str), but any
    ``KnowledgeGraphEntry`` constructed with ``type=NodeType.TASK``
    silently re-shapes to the new POLE+O storage form.

    New code should reference type names as strings or read
    ``NODE_REGISTRY`` directly.
    """

    DOCUMENT = "document"
    CHUNK = "chunk"
    PERSON = "person"
    ORGANIZATION = "organization"
    LOCATION = "location"
    EVENT = "event"
    OBJECT = "object"
    PREFERENCE = "preference"
    # #031: ``fact`` is an LLM-extractable POLE+O escape-hatch node type
    # for propositions that don't fit any registered relation semantic.
    # Island-style: facts participate in no edges (the envelope validator
    # rejects every edge whose source or target is a ``fact``).
    FACT = "fact"
    # --- Legacy alias (#028) — re-routed at write time ---
    TASK = "task"


class EdgeType(StrEnum):
    """Backward-compat shim built from ``EDGE_REGISTRY`` (#027, refactored #029).

    #029 collapsed the LLM-extractable domain edges ``todo`` and
    ``experienced`` into the ``related_to`` umbrella discriminated by
    ``semantic_type``. The enum members are GONE — code that used to
    write ``EdgeType.TODO`` / ``EdgeType.EXPERIENCED`` now writes
    ``EdgeType.RELATED_TO`` with ``semantic_type="has_task"`` or
    ``"experienced_by"`` respectively. There is no legacy alias because
    the wire shape changes: keeping a dead enum member would let
    callers silently emit edges the validator now rejects.
    """

    PART_OF = "part_of"
    NEXT = "next"
    MENTIONS = "mentions"
    REFERENCED = "referenced"
    RELATED_TO = "related_to"
    HAS = "has"
    SAME_AS = "same_as"
    # #032: bi-temporal supersession edge. Resolver-written
    # (LLM never emits this). Allowed pairs:
    # ``(preference, preference)`` and ``(fact, fact)``.
    SUPERSEDED_BY = "superseded_by"


# --- Phase-3 #030: ExtractorInfo (provenance metadata) ---


class ExtractorInfo(BaseModel):
    """Provenance metadata for an LLM-extracted ``KnowledgeGraphEntry``.

    Stored as an **embedded** Pydantic model on every row the extraction
    pipeline writes from an LLM emission. Structural rows
    (``document``, ``chunk``) leave the column ``None`` per `plan.md:210`.

    A query like
    ``db.knowledge_graph.find({"extractor.name": "gemini-2.5-pro"})`` then
    surfaces every LLM-extracted row, which is the audit signal `plan.md`
    calls out at line 208.
    """

    name: str = Field(
        description="Extractor identifier — typically the LLM model name (e.g. 'gemini-2.5-pro').",
    )
    version: str = Field(
        description="Pipeline / model version tag (e.g. 'tree-memory-0.1.0').",
    )
    extraction_time_ms: int | None = Field(
        default=None,
        description="Wall-time the LLM call took for this row, in milliseconds. Optional perf metric.",
    )


# --- ID builders ---


def build_node_id(
    user_id: PydanticObjectId,
    node_type: NodeType | str,
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

    Post-#027: ``node_type`` accepts either a :class:`NodeType` enum
    member or a plain ``str`` (e.g. ``"person"``) — both produce the
    same ``_id``. New code can use string type names directly without
    going through the enum shim.
    """

    return f"{user_id}:{node_type}:{name}"


def build_edge_id(
    source_node_id: str,
    edge_type: EdgeType | str,
    target_node_id: str,
) -> str:
    """Build an edge ``_id`` string: ``"source|type|target"``.

    Edge ids carry no explicit ``user_id`` segment because both endpoint
    node ids already begin with ``"{user_id}:"`` (post-#018). Cross-user
    edges are impossible by construction — the resulting ``_id`` would
    mix two distinct tenant prefixes, and the indexed ``user_id`` field
    on the row pins the edge to exactly one tenant.

    Post-#027: ``edge_type`` accepts either a :class:`EdgeType` enum
    member or a plain ``str`` (e.g. ``"todo"``).
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
    # Post-#027: ``type`` is a plain string on the wire. A model
    # validator below (``_check_type_against_registry``) rejects
    # construction of node rows whose ``type`` is not in
    # ``NODE_REGISTRY`` and edge rows whose ``type`` is not in
    # ``EDGE_REGISTRY``. The :class:`NodeType` / :class:`EdgeType`
    # enums are still accepted as inputs (they're ``StrEnum``
    # subclasses) and serialize to the same strings.
    type: str

    # Node fields
    name: str | None = None
    # Phase-3 #028: closed-vocabulary subtype slot. Validated against
    # the parent type's ``NODE_REGISTRY[type].subtypes`` set when the
    # parent has a closed subtype vocabulary. ``None`` is accepted at
    # construction (loose); the strict-every-LLM-node-has-a-subtype
    # rule lands at the envelope-validator pipeline in #030.
    subtype: str | None = None
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
    # #029: ``semantic_type`` discriminates the new ``related_to`` umbrella
    # edge. Required on every ``type="related_to"`` row (validated below);
    # MUST be ``None`` on every other edge type. The compound index
    # ``(user_id, type, semantic_type)`` declared in ``Settings.indexes``
    # is partial-filtered on ``semantic_type``, so only ``related_to``
    # rows pay the index cost.
    semantic_type: str | None = None
    # Provenance
    sources: list[PydanticObjectId] = Field(default_factory=list)

    # Timestamps
    created_at: datetime
    updated_at: datetime

    # --- Phase-3 #030: common columns added by the validator + provenance task ---
    #
    # ``description`` is the free-text human-readable label the LLM may
    # emit alongside ``name``; it's surfaced on UIs / preview prompts.
    # ``valid_from`` / ``valid_until`` are the bi-temporal columns used
    # by #031 (`fact`) and #032 (`preference` supersession). Both
    # accept tz-aware UTC datetimes only — naive datetimes are rejected
    # by ``_check_temporal_fields_are_tz_aware`` so we never silently
    # mix wall-clock and UTC values inside the graph.
    # ``extractor`` is the embedded :class:`ExtractorInfo` provenance
    # block populated by the extraction pipeline on every LLM-extracted
    # row. ``None`` on structural rows (``document`` / ``chunk``).
    description: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    extractor: ExtractorInfo | None = None

    @field_validator("valid_from", "valid_until", mode="after")
    @classmethod
    def _require_tz_aware_temporal(cls, value: datetime | None) -> datetime | None:
        """Reject naive datetimes on ``valid_from`` / ``valid_until``.

        Per ``CLAUDE.md``: all datetimes are timezone aware (UTC by
        default). Mixing a naive value into the graph corrupts later
        comparisons against tz-aware ``now`` — make that a hard error
        at validation time. ``None`` is fine (the column is optional).
        """

        if value is None:
            return value
        if value.tzinfo is None:
            raise ValueError(
                "valid_from / valid_until must be timezone-aware (UTC); "
                f"got naive datetime {value!r}"
            )
        return value

    # --- Phase-3 #028: legacy (type=task) → (parent, subtype) ---
    # Re-routes legacy node rows at construction time so the rest of the
    # validator chain sees the new POLE+O shape. Runs in ``mode="before"``
    # because the downstream type-vs-registry check would otherwise
    # reject ``"task"`` (no longer registered as a top-level node type
    # after #028).
    #
    # Idempotent: a row that already carries ``type="object",
    # subtype="task"`` is untouched. If the caller has already set
    # ``subtype`` explicitly, the legacy ``type`` is rewritten but
    # ``subtype`` is left as the caller provided it.
    _LEGACY_NODE_REWRITES: ClassVar[dict[str, tuple[str, str]]] = {
        # legacy type -> (new parent, subtype)
        "task": ("object", "task"),
    }

    @model_validator(mode="before")
    @classmethod
    def _reroute_legacy_node_types(cls, data: Any) -> Any:
        """Rewrite legacy ``(type=task)`` to the POLE+O subtype shape.

        Only touches ``kind="node"`` rows. Edge rows keep their legacy
        ``source_type`` / ``target_type`` columns untouched — those are
        cleaned up by #029's edge collapse, not here.
        """

        if not isinstance(data, dict):
            return data
        if data.get("kind") != "node":
            return data
        raw_type = data.get("type")
        # ``type`` may arrive as a ``NodeType`` enum member or a plain
        # str; normalize once for the lookup.
        type_value = raw_type.value if hasattr(raw_type, "value") else raw_type
        rewrite = cls._LEGACY_NODE_REWRITES.get(type_value)
        if rewrite is None:
            return data
        new_type, new_subtype = rewrite
        out = dict(data)
        out["type"] = new_type
        # Only fill subtype if the caller didn't already supply one
        # (defensive — lets a future migration override legacy mappings).
        if out.get("subtype") is None:
            out["subtype"] = new_subtype
        return out

    @model_validator(mode="after")
    def _check_type_against_registry(self) -> "KnowledgeGraphEntry":
        """Phase-3 #027: enforce that ``type`` matches a registered
        node/edge type for the given ``kind``.

        Import lazily inside the validator to keep
        ``tree.entities.knowledge_graph`` free of any top-level
        dependency on ``tree.entities.ontology`` (the latter imports
        ``NodeType`` / ``EdgeType`` from here — a top-level import
        would be a cycle).
        """

        from tree.entities.ontology import EDGE_REGISTRY, NODE_REGISTRY

        if self.kind == "node":
            if self.type not in NODE_REGISTRY:
                raise ValueError(
                    f"type {self.type!r} is not a registered node type "
                    f"(known: {sorted(NODE_REGISTRY)})"
                )
        elif self.kind == "edge":
            if self.type not in EDGE_REGISTRY:
                raise ValueError(
                    f"type {self.type!r} is not a registered edge type "
                    f"(known: {sorted(EDGE_REGISTRY)})"
                )
        # Unknown ``kind`` values fall through; the ``kind`` validator
        # (Phase 1) is the gate that rejects those.
        return self

    @model_validator(mode="after")
    def _check_subtype_against_registry(self) -> "KnowledgeGraphEntry":
        """Phase-3 #028: enforce ``subtype`` is in the parent's closed set.

        Loose contract at construction time:

        * Non-node rows pass through unchanged.
        * Nodes whose parent type isn't registered fall through (the
          previous validator already rejected unregistered types).
        * Nodes whose parent's ``subtypes`` is ``None`` (freeform —
          e.g. ``document`` / ``chunk`` / pre-#028 ``person``) accept
          any subtype value, including ``None``.
        * Nodes whose parent has a non-empty closed set:
          - ``subtype is None`` is accepted (the strict
            "every LLM node must have a subtype" rule is an
            envelope-level check that lands at #030's validator
            pipeline; intermediate construction in the resolver /
            indexing path may legitimately leave subtype unfilled).
          - ``subtype not in spec.subtypes`` -> ``ValueError``.
        """

        from tree.entities.ontology import NODE_REGISTRY

        if self.kind != "node":
            return self
        spec = NODE_REGISTRY.get(self.type)
        if spec is None:
            # The type-vs-registry validator already raised; this branch
            # is defensive and unreachable in practice.
            return self
        if spec.subtypes is None:
            # Freeform parent — no validation.
            return self
        if self.subtype is None:
            # Loose at construction; tighter checks land in #030.
            return self
        if self.subtype not in spec.subtypes:
            raise ValueError(
                f"subtype {self.subtype!r} not in allowed set "
                f"{sorted(spec.subtypes)} for node type {self.type!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_related_to_semantic(self) -> "KnowledgeGraphEntry":
        """Phase-3 #029: enforce ``related_to`` umbrella semantics.

        Contract (per the task spec):

        * ``kind="edge", type="related_to"`` rows MUST set
          ``semantic_type`` to a registered ``RELATION_SEMANTICS``
          name, AND ``(source_type.value, target_type.value)`` MUST
          appear in that semantic's ``allowed_pairs``.
        * Any other edge row MUST leave ``semantic_type`` as ``None``
          — silently allowing it to be set on, say, a ``mentions`` row
          would let one row be indexed under the umbrella index and
          another under the legacy index, splitting reads.
        * Node rows pass through unchanged.

        Pair lookup uses the **parent** type names (``source_type`` /
        ``target_type`` always carry parent names — subtypes live on
        the node row, not the edge row). Edges that violate the
        contract raise ``ValueError`` so the extraction-write path's
        try/except can drop them and audit-log the rejection (the
        ``extraction_rejections`` collection lands in #030).
        """

        if self.kind != "edge":
            return self

        from tree.entities.ontology import RELATION_SEMANTICS

        if self.type == "related_to":
            if self.semantic_type is None:
                raise ValueError(
                    "kind='edge' type='related_to' requires a "
                    "non-None 'semantic_type'. Known semantics: "
                    f"{sorted(RELATION_SEMANTICS)}"
                )
            spec = RELATION_SEMANTICS.get(self.semantic_type)
            if spec is None:
                raise ValueError(
                    f"semantic_type {self.semantic_type!r} is not a "
                    f"registered relation semantic. Known: "
                    f"{sorted(RELATION_SEMANTICS)}"
                )
            src = self.source_type.value if self.source_type is not None else None
            tgt = self.target_type.value if self.target_type is not None else None
            if (src, tgt) not in spec.allowed_pairs:
                raise ValueError(
                    f"related_to[semantic_type={self.semantic_type!r}] "
                    f"does not allow pair ({src!r}, {tgt!r}); "
                    f"allowed pairs: {spec.allowed_pairs}"
                )
            return self

        # Non-related_to edge: semantic_type MUST be None.
        if self.semantic_type is not None:
            raise ValueError(
                f"semantic_type is reserved for type='related_to' edges; "
                f"got type={self.type!r} with "
                f"semantic_type={self.semantic_type!r}"
            )

        # Enforce ``EdgeTypeSpec.allowed_pairs`` strictly for every
        # other registered edge type so the broadened ``mentions`` and
        # ``same_as`` constraints — and the narrowed ``has`` — are
        # write-time constraints, not just LLM-prompt advisories
        # (per #029's "strict-by-omission" policy).
        from tree.entities.ontology import EDGE_REGISTRY

        spec = EDGE_REGISTRY.get(self.type)
        if spec is None:
            # Already rejected by ``_check_type_against_registry``; safe
            # to fall through.
            return self
        src = self.source_type.value if self.source_type is not None else None
        tgt = self.target_type.value if self.target_type is not None else None
        if (src, tgt) not in spec.allowed_pairs:
            raise ValueError(
                f"edge type {self.type!r} does not allow pair ({src!r}, "
                f"{tgt!r}); allowed pairs: {spec.allowed_pairs}"
            )
        return self

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
            # #028: filter by (type, subtype) for POLE+O — e.g. "all
            # `object/task` for this user". The `subtype` column is
            # sparse-by-nature (None on document/chunk and on rows
            # written before #028), but the index still serves the
            # explicit-subtype queries the MCP tools issue.
            IndexModel(
                [("user_id", 1), ("kind", 1), ("type", 1), ("subtype", 1)],
                name="user_kind_type_subtype",
            ),
            # #029: partial index for the ``related_to`` umbrella edge.
            # Filtered on ``semantic_type`` non-null so only the new
            # umbrella rows pay the maintenance cost; queries like
            # ``find_edges(type='related_to', semantic_type='employed_by')``
            # land on this index prefix.
            IndexModel(
                [("user_id", 1), ("type", 1), ("semantic_type", 1)],
                name="user_type_semantic_type",
                partialFilterExpression={"semantic_type": {"$type": "string"}},
            ),
        ]
