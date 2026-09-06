"""The ``memory`` collection: ONE polymorphic row model for BOTH memory modes.

Renamed from ``knowledge_graph`` by ADR-006 (which supersedes ADR-001 decision 1
on the collection NAME only). There is no compat alias for the old module or
class name — every import site was updated.

:data:`MEMORY_COLLECTION` is the single spelling of the collection name; no
module keeps a private copy. :data:`RAG_NODE_TYPES` is the single, explicit
place that says which rows the RAG layer writes.
"""

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
    re-routed by :class:`MemoryEntry`'s ``mode="before"``
    validator to ``(type="object", subtype="task")``. Reading the enum
    member in consumer code still works (StrEnum -> str), but any
    ``MemoryEntry`` constructed with ``type=NodeType.TASK``
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
    """Provenance metadata for an LLM-extracted ``MemoryEntry``.

    Stored as an **embedded** Pydantic model on every row the extraction
    pipeline writes from an LLM emission. Structural rows
    (``document``, ``chunk``) leave the column ``None`` per `plan.md:210`.

    A query like
    ``db.memory.find({"extractor.name": "gemini-2.5-pro"})`` then
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


# --- ADR-007 §3: Embedding-map coordinates on a child chunk ---


class ChunkViz(BaseModel):
    """A **Child chunk**'s position on the **Embedding map** (ADR-007 §3).

    Written as an embedded block by a **Clustering run** so the coordinates and
    the run that produced them can never be separated: a chunk whose ``run_id``
    differs from the latest run is STALE and is dropped from the map (and
    counted in its legend) rather than drawn at coordinates from an older UMAP
    fit, which would place it in a space nothing else shares.
    """

    x: float = Field(description="x coordinate in the 2-D Embedding map space.")
    y: float = Field(description="y coordinate in the 2-D Embedding map space.")
    run_id: str = Field(
        description=(
            "The Clustering run that produced these coordinates (the Prefect "
            "flow-run id). Coordinates from an older run are stale."
        ),
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


# --- Single collection (memory) ---
# Nodes and edges coexist with string _id values:
#   - Nodes: _id = "{user_id}:type:name" (str), e.g. "65f...:person:alice"
#   - Edges: _id = "source|type|target" (str), source/target carry the user prefix.
# Upserted directly during extraction (no separate log collection).

MEMORY_COLLECTION = "memory"
"""Name of the ONE collection holding every memory row, in BOTH memory modes.

The single source of truth for the string: :class:`MemoryEntry`'s
``Settings.name``, every raw-pymongo reader (``database[MEMORY_COLLECTION]``),
every ``$lookup`` / ``$graphLookup`` ``from:`` value and the ``nl_query``
system prompt all read THIS constant. Renamed from ``knowledge_graph``
(ADR-006 decision 1); ``knowledge_graph_meta_state`` (the graph-only dream
watermark) intentionally keeps its own name.
"""

RAG_NODE_TYPES: frozenset[str] = frozenset({"document", "chunk"})
"""The closed set of node types the RAG layer writes (ADR-006 decision 1).

The rag/graph split is by MODE, not by row: every row the RAG layer writes (a
``document`` root, its parent chunks and its child chunks) is ALSO a first-class
graph node in ``graphrag``, with ``part_of`` / ``next`` edges hung off it. So
there are no per-mode ODM subclasses (ADR-006 rejected that hierarchy) — this
constant is the single, explicit place that says "these rows are the RAG rows;
every other node type, and every ``kind: edge`` row, is graph".

Both members are structural (``llm_extractable=False`` in ``NODE_REGISTRY``):
the RAG loader builds them deterministically, the LLM never emits them.
Consumed by the loader, the child-chunk search filter, and the "rag mode never
writes anything else" test.
"""


class MemoryEntry(BeanieDocument):
    id: str
    # No standalone single-key index on ``user_id``: every compound
    # index in ``Settings.indexes`` below (and the dynamic indexes
    # created in :mod:`tree.memory.rag.indexing`) leads with
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

    # --- ADR-006 decision 2: the two-level chunk hierarchy ---
    #
    # Top-level graph-modeling meta fields (ADR-001 §11), NOT properties: a
    # parent chunk points at its ``document`` row, a child chunk at its parent
    # chunk. In ``rag`` mode these two columns ARE the hierarchy (the
    # collection holds no edge rows); in ``graphrag`` the same values are also
    # materialised as ``part_of`` edges, so chunk rows stay byte-identical
    # across modes. ``None`` on every non-chunk row.
    parent_id: str | None = Field(
        default=None,
        description=(
            "_id of this row's parent row — the document row for a parent "
            "chunk, the parent chunk row for a child chunk. None elsewhere."
        ),
    )
    chunk_index: int | None = Field(
        default=None,
        description=(
            "0-based position among siblings: parent chunks within their "
            "document, child chunks within their parent. None on non-chunk rows."
        ),
    )

    # --- ADR-007 §3: clustering membership + map coordinates ---
    #
    # Two more top-level graph-modeling meta fields (ADR-001 §11), written ONLY
    # by a Clustering run and ONLY on child chunk rows (validator below). Both
    # default to ``None``, so every row written before ADR-007 validates
    # unchanged — there is no migration. Membership lives here rather than on
    # the cluster row because the chunk is what the Embedding map draws.
    cluster_id: int | None = Field(
        default=None,
        description=(
            "Memory cluster this child chunk belongs to in the latest "
            "Clustering run. -1 = noise (HDBSCAN placed it in no cluster); "
            "None = never clustered. Child chunk rows only."
        ),
    )
    viz: ChunkViz | None = Field(
        default=None,
        description=(
            "This child chunk's Embedding map coordinates and the Clustering "
            "run that produced them. None until the chunk is clustered. Child "
            "chunk rows only."
        ),
    )

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
    def _check_type_against_registry(self) -> "MemoryEntry":
        """Phase-3 #027: enforce that ``type`` matches a registered
        node/edge type for the given ``kind``.

        Import lazily inside the validator to keep
        ``tree.entities.memory`` free of any top-level
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
    def _check_subtype_against_registry(self) -> "MemoryEntry":
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
    def _check_cluster_fields_are_child_only(self) -> "MemoryEntry":
        """ADR-007 §3: only a **Child chunk** may carry ``cluster_id`` / ``viz``.

        A Clustering run reads exactly the ``type="chunk", subtype="child"``
        rows with an embedding, and the Embedding map plots exactly those. A
        ``document``, a parent chunk or an entity node carrying coordinates
        would either be silently ignored by every surface or — worse — drawn as
        a point that no cluster ever counted, so make it a write-time error.
        """

        if self.cluster_id is None and self.viz is None:
            return self
        if self.type == "chunk" and self.subtype == "child":
            return self

        populated = [
            name
            for name, value in (("cluster_id", self.cluster_id), ("viz", self.viz))
            if value is not None
        ]
        raise ValueError(
            f"{' and '.join(populated)} may only be set on a child chunk row "
            "(type='chunk', subtype='child') — clustering reads and the "
            f"Embedding map draws child chunks only; got type={self.type!r}, "
            f"subtype={self.subtype!r}"
        )

    @model_validator(mode="after")
    def _check_related_to_semantic(self) -> "MemoryEntry":
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
        name = MEMORY_COLLECTION
        indexes = [
            # user_id-prepended compound indexes for fast filtered reads.
            # The dynamic indexes (kind_source_node, kind_target_node,
            # kind_embedding, canonical_name) created in
            # tree.memory.rag.indexing get user_id prepended in #019 —
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
