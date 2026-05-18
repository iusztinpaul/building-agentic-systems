"""Two-tier validation policy for LLM-emitted knowledge-graph rows (#030).

Per `plan.md:308-349`, extraction-time validation is **strict at the
envelope and lenient at the field**:

* :func:`validate_envelope` — strict. Drops the entire row when the
  envelope fails (unknown type, disallowed pair, missing name, missing
  required subtype, unknown semantic, ``fact`` endpoint).
* :func:`validate_properties` — lenient. Drops only invalid fields and
  returns the surviving subset plus a list of :class:`FieldDrop`
  records. Never raises.

The asymmetry is intentional: the envelope is the row's
*identity-shape* and a wrong identity means we can't safely write
anything (the ``_id`` would be malformed). Per-field validation is
the *content quality* check; a single bad field is signal for prompt
iteration, not justification for losing the whole row.

Both functions are pure / synchronous so they can be unit-tested
without a Mongo connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from tree.entities.ontology import (
    EDGE_REGISTRY,
    NODE_REGISTRY,
    RELATION_SEMANTICS,
    SUBTYPE_EXTRAS,
)


# ---------------------------------------------------------------------------
# Field-level lenient validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDrop:
    """One property dropped by :func:`validate_properties`.

    Carried out of the validator and into the audit pipeline so the
    ``extraction_dropped_fields`` collection can record which keys the
    LLM emitted that the ontology rejected.
    """

    field: str
    """Name of the property the LLM emitted."""

    value: Any
    """Raw value as the LLM emitted it (pre-validation)."""

    reason: str
    """Short structured reason: ``"unknown_field"`` or a Pydantic message."""


def validate_properties(
    raw: dict[str, Any],
    schema: type[BaseModel] | None,
    extras: type[BaseModel] | None = None,
) -> tuple[dict[str, Any], list[FieldDrop]]:
    """Per-field validation — keep valid, drop invalid, never raise.

    Combines the parent's ``properties_schema`` fields with the optional
    subtype ``extras`` model (for closed-vocab subtypes whose
    ``SUBTYPE_EXTRAS`` layer additional fields, e.g.
    ``("object", "project") → ProjectExtras``).

    Unknown keys (not in either schema) → dropped with
    ``reason="unknown_field"``.

    Type-validation failures → dropped with ``reason=<pydantic-msg>``.
    Valid fields are kept verbatim in the returned dict. The function
    **never raises** — that's the whole point: a single bad LLM
    emission must not destroy the row.

    Args:
        raw: The LLM-emitted ``properties`` dict.
        schema: The parent's ``properties_schema`` Pydantic model, or
            ``None`` if the parent declares no schema (rare — mostly
            structural-edge property models that lack a schema).
        extras: Optional subtype-specific extras model layered on top.

    Returns:
        ``(validated, drops)`` where ``validated`` is the surviving
        ``{key: coerced_value}`` dict and ``drops`` is the per-key
        diagnostic list.
    """

    validated: dict[str, Any] = {}
    drops: list[FieldDrop] = []

    # Build the combined field map. ``extras`` wins when both define
    # the same key — matches Pydantic's own multiple-inheritance MRO.
    combined_fields: dict[str, Any] = {}
    if schema is not None:
        combined_fields.update(schema.model_fields)
    if extras is not None:
        combined_fields.update(extras.model_fields)

    # If neither schema nor extras was supplied (no typed properties on
    # this row type), every key is unknown.
    if not combined_fields:
        for key, value in raw.items():
            drops.append(FieldDrop(field=key, value=value, reason="unknown_field"))
        return validated, drops

    for key, value in raw.items():
        if key not in combined_fields:
            drops.append(FieldDrop(field=key, value=value, reason="unknown_field"))
            continue
        field_info = combined_fields[key]
        annotation = field_info.annotation
        try:
            adapter = TypeAdapter(annotation)
            validated[key] = adapter.validate_python(value)
        except ValidationError as e:
            # Compact the Pydantic error message — full validators
            # include the schema URL which is noisy in the audit row.
            reason = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
                for err in e.errors()
            )
            drops.append(FieldDrop(field=key, value=value, reason=reason))
        except Exception as e:  # noqa: BLE001
            # Some types (e.g. exotic forward refs) can raise non-
            # ValidationError at adapter construction. Treat the same.
            drops.append(FieldDrop(field=key, value=value, reason=str(e)))

    return validated, drops


# ---------------------------------------------------------------------------
# Envelope-level strict validation
# ---------------------------------------------------------------------------


# Endpoint type names that disqualify an edge entirely. Encoding the
# ``fact`` carve-out here keeps #031's surface area small — once
# ``fact`` registers, this list is the single point of truth for the
# "facts are an island" rule.
_FORBIDDEN_EDGE_ENDPOINT_TYPES: frozenset[str] = frozenset({"fact"})


@dataclass(frozen=True)
class EnvelopeResult:
    """Outcome of :func:`validate_envelope`.

    ``ok=True`` means the row passed; ``ok=False`` means the entire
    row should be dropped and an ``ExtractionRejection`` row written
    with ``rejection_reason=reason``.
    """

    ok: bool
    reason: str | None = None


def validate_envelope(
    *,
    kind: str,
    type: str,
    subtype: str | None = None,
    name: str | None = None,
    source_type: str | None = None,
    target_type: str | None = None,
    semantic_type: str | None = None,
) -> EnvelopeResult:
    """Envelope-level strict validation.

    Returns :class:`EnvelopeResult` ``(ok, reason)``. ``reason`` is a
    short structured token (``"unknown_type"`` / ``"disallowed_pair"``
    / ``"missing_name"`` / ``"missing_subtype"`` / ``"unknown_semantic"``
    / ``"semantic_on_non_related_to"`` / ``"fact_endpoint_disallowed"``)
    suitable for the ``ExtractionRejection.rejection_reason`` column.

    Contract (per `plan.md:316-324`):

    1. ``kind="node"``:
       * ``type`` ∈ :data:`NODE_REGISTRY`.
       * If the registered spec is LLM-extractable: ``name`` is
         non-empty (deterministic ``_id`` needs it).
       * If the registered spec has a closed ``subtypes`` set and is
         LLM-extractable: ``subtype`` is required and a member.
    2. ``kind="edge"``:
       * ``type`` ∈ :data:`EDGE_REGISTRY`.
       * Neither endpoint type is in
         :data:`_FORBIDDEN_EDGE_ENDPOINT_TYPES` (``fact`` carve-out).
       * If ``type=="related_to"``: ``semantic_type`` is in
         :data:`RELATION_SEMANTICS` AND ``(source_type, target_type)``
         is in that semantic's ``allowed_pairs``.
       * If ``type!="related_to"``: ``semantic_type`` is ``None``.
    3. ``kind`` outside ``{"node", "edge"}`` → rejected.
    """

    if kind == "node":
        return _validate_node_envelope(type=type, name=name, subtype=subtype)
    if kind == "edge":
        return _validate_edge_envelope(
            type=type,
            source_type=source_type,
            target_type=target_type,
            semantic_type=semantic_type,
        )
    return EnvelopeResult(ok=False, reason="unknown_kind")


def _validate_node_envelope(
    *, type: str, name: str | None, subtype: str | None
) -> EnvelopeResult:
    spec = NODE_REGISTRY.get(type)
    if spec is None:
        return EnvelopeResult(ok=False, reason="unknown_type")

    # ``fact`` is reserved as a future node type (#031). Even on
    # ``kind="node"`` we drop emissions until #031 explicitly enables
    # it — keeps the validator forward-compatible.
    if type in _FORBIDDEN_EDGE_ENDPOINT_TYPES:
        return EnvelopeResult(ok=False, reason="fact_endpoint_disallowed")

    # The strict name+subtype rules apply only to LLM-extractable
    # nodes. Structural nodes (``document`` / ``chunk``) are
    # pipeline-emitted; the pipeline always supplies a name and never
    # a subtype, so checking them would be dead code for that path.
    if spec.llm_extractable:
        if not name or not str(name).strip():
            return EnvelopeResult(ok=False, reason="missing_name")
        if spec.subtypes is not None and subtype is None:
            # Strict: every POLE+O LLM-extractable node MUST have a
            # subtype when the parent has a closed vocabulary
            # (`plan.md:170-173`, surfaced as the tightening pass in
            # #028).
            return EnvelopeResult(ok=False, reason="missing_subtype")
        if (
            spec.subtypes is not None
            and subtype is not None
            and subtype not in spec.subtypes
        ):
            return EnvelopeResult(ok=False, reason="unknown_subtype")

    return EnvelopeResult(ok=True)


def _validate_edge_envelope(
    *,
    type: str,
    source_type: str | None,
    target_type: str | None,
    semantic_type: str | None,
) -> EnvelopeResult:
    spec = EDGE_REGISTRY.get(type)
    if spec is None:
        return EnvelopeResult(ok=False, reason="unknown_type")

    # Fact-endpoint carve-out: edges that touch a ``fact`` row are
    # rejected (per `plan.md`'s "facts are an island" rule).
    if source_type in _FORBIDDEN_EDGE_ENDPOINT_TYPES or (
        target_type in _FORBIDDEN_EDGE_ENDPOINT_TYPES
    ):
        return EnvelopeResult(ok=False, reason="fact_endpoint_disallowed")

    # Edges always need both endpoint types — without them we can't
    # check ``allowed_pairs``.
    if source_type is None or target_type is None:
        return EnvelopeResult(ok=False, reason="missing_endpoint_type")

    if type == "related_to":
        if semantic_type is None:
            return EnvelopeResult(ok=False, reason="missing_semantic_type")
        rel_spec = RELATION_SEMANTICS.get(semantic_type)
        if rel_spec is None:
            return EnvelopeResult(ok=False, reason="unknown_semantic")
        if (source_type, target_type) not in rel_spec.allowed_pairs:
            return EnvelopeResult(ok=False, reason="disallowed_pair")
        return EnvelopeResult(ok=True)

    # Non-``related_to`` edges MUST NOT carry a semantic_type.
    if semantic_type is not None:
        return EnvelopeResult(ok=False, reason="semantic_on_non_related_to")

    # Generic pair check.
    if (source_type, target_type) not in spec.allowed_pairs:
        return EnvelopeResult(ok=False, reason="disallowed_pair")

    return EnvelopeResult(ok=True)


# ---------------------------------------------------------------------------
# Schema-lookup helpers (used by pipeline integration)
# ---------------------------------------------------------------------------


def get_node_property_schemas(
    *, type: str, subtype: str | None
) -> tuple[type[BaseModel] | None, type[BaseModel] | None]:
    """Return ``(parent_schema, extras_schema)`` for a node row.

    Looks up the parent's ``properties_schema`` and, when ``subtype`` is
    a closed-vocab member with ``SUBTYPE_EXTRAS`` registered, also
    returns the extras model. ``None`` for either when not registered.
    """

    parent = NODE_REGISTRY.get(type)
    parent_schema = parent.properties_schema if parent is not None else None
    extras_schema: type[BaseModel] | None = None
    if subtype is not None:
        extras_schema = SUBTYPE_EXTRAS.get((type, subtype))
    return parent_schema, extras_schema


def get_edge_property_schema(
    *, type: str, semantic_type: str | None
) -> type[BaseModel] | None:
    """Return the property-schema model for an edge row.

    For ``related_to`` edges, returns the per-semantic
    ``RELATION_SEMANTICS[semantic_type].properties_schema`` (may be
    ``None`` for semantics without typed properties). For every other
    edge type, returns the spec's ``properties_schema``.
    """

    if type == "related_to":
        if semantic_type is None:
            return None
        rel_spec = RELATION_SEMANTICS.get(semantic_type)
        if rel_spec is None:
            return None
        return rel_spec.properties_schema

    spec = EDGE_REGISTRY.get(type)
    if spec is None:
        return None
    return spec.properties_schema
