"""Audit collections for the extraction validator (#030).

Two Beanie Documents that surface schema drift between the LLM and the
ontology as **structured signal** rather than `logger.warning` lines:

* :class:`ExtractionRejection` — written when the envelope validator
  drops an entire row (unknown type, disallowed pair, missing name,
  unknown semantic, fact-endpoint, missing-subtype on a closed-vocab
  POLE+O parent).
* :class:`ExtractionDroppedField` — written when the lenient field-level
  validator drops one or more properties off an otherwise-valid row.
  Aggregations like
  ``{$group: {_id: "$dropped_field", count: {$sum: 1}}}`` drive prompt
  iteration: high drop counts on a given field point at an ambiguous
  prompt rule.

Both collections are tenant-scoped (``user_id`` is required) and
indexed on ``(user_id, timestamp)`` so an operator can pull "the last
24h of rejections for tenant X" in O(log n).

Per `plan.md:431-434`, both collections are dev-only audit signal and
get wiped by the #033 migration; there is no production-grade
retention policy attached today.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Document as BeanieDocument
from beanie import PydanticObjectId
from pydantic import Field
from pymongo import IndexModel

from tree.entities.knowledge_graph import ExtractorInfo


# Cap on the raw-row / raw-value payloads stored in the audit rows.
# A pathological LLM emission can balloon the dict to many KB; capping
# keeps the audit collection small and protects against accidental PII
# spillage if a downstream operator runs ``find()`` over the table.
_MAX_RAW_ROW_BYTES = 4_096
_MAX_RAW_VALUE_BYTES = 1_024


def _truncate_for_audit(value: Any, max_bytes: int) -> Any:
    """Best-effort size cap for audit payloads.

    Pydantic serializes the value through ``BaseModel.model_dump()``
    when this Document is inserted; the resulting BSON document is
    bounded by Mongo's 16MB limit regardless, so this cap is a
    *defensive* second tier — it stops a single malformed emission
    from filling the audit table with megabyte-scale rows.

    Returns ``value`` unchanged when it serializes under the cap;
    returns a ``str`` truncation marker otherwise.
    """

    try:
        # ``repr`` is the cheap path: structured values keep their
        # shape, scalars stringify, and we get a deterministic length
        # estimate without an extra json import.
        text = repr(value)
    except Exception:  # noqa: BLE001 — defensive against pathological reprs
        return "<unrepresentable>"
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    # Truncate the text rather than the raw value so the audit row
    # remains valid BSON.
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{truncated!s}…<truncated:{len(encoded)}B>"


class ExtractionRejection(BeanieDocument):
    """One whole-row rejection from the envelope validator (#030).

    The envelope validator drops the row entirely (no
    :class:`KnowledgeGraphEntry` is written); this Document carries
    enough provenance to investigate why.
    """

    user_id: PydanticObjectId = Field(
        description="Tenant whose extraction surfaced the rejection.",
    )
    chunk_id: str | None = Field(
        default=None,
        description="ID of the chunk the row came from, when known.",
    )
    document_id: PydanticObjectId | None = Field(
        default=None,
        description="ID of the parent document, when known.",
    )
    timestamp: datetime = Field(
        description="UTC wall-time the rejection was recorded.",
    )
    rejected_at_stage: str = Field(
        default="envelope",
        description="Pipeline stage that dropped the row (typically 'envelope').",
    )
    rejection_reason: str = Field(
        description=(
            "Short structured reason, e.g. 'unknown_type', 'disallowed_pair', "
            "'missing_name', 'missing_subtype', 'unknown_semantic', "
            "'fact_endpoint_disallowed'."
        ),
    )
    raw_row: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw LLM-emitted payload, truncated to ~4KB for safety.",
    )
    extractor: ExtractorInfo | None = Field(
        default=None,
        description="Provenance of the extractor that emitted the row.",
    )

    class Settings:
        name = "extraction_rejections"
        indexes = [
            IndexModel(
                [("user_id", 1), ("timestamp", -1)],
                name="user_timestamp_desc",
            ),
            IndexModel(
                [("user_id", 1), ("rejection_reason", 1)],
                name="user_reason",
            ),
        ]


class ExtractionDroppedField(BeanieDocument):
    """One per-field drop from the lenient field-level validator (#030).

    The row itself was written to ``knowledge_graph``; only the named
    property was dropped (either unknown to the schema or failed type
    validation). One Document is written per dropped field; an
    emission with three bad fields produces three rows.
    """

    user_id: PydanticObjectId = Field(
        description="Tenant whose extraction surfaced the dropped field.",
    )
    chunk_id: str | None = Field(
        default=None,
        description="ID of the chunk the row came from, when known.",
    )
    document_id: PydanticObjectId | None = Field(
        default=None,
        description="ID of the parent document, when known.",
    )
    timestamp: datetime = Field(
        description="UTC wall-time the drop was recorded.",
    )
    row_type: str = Field(
        description="Parent type of the row that hosted the bad field (e.g. 'person', 'related_to').",
    )
    row_subtype: str | None = Field(
        default=None,
        description="Subtype of the row, when set.",
    )
    semantic_type: str | None = Field(
        default=None,
        description="``semantic_type`` of the row when it was a ``related_to`` edge.",
    )
    dropped_field: str = Field(
        description="Name of the property dropped by the validator.",
    )
    raw_value: Any = Field(
        default=None,
        description="Raw value the LLM emitted for the dropped field, truncated to ~1KB.",
    )
    reason: str = Field(
        description=(
            "Short structured reason, e.g. 'unknown_field' or a Pydantic "
            "validation message."
        ),
    )
    extractor: ExtractorInfo | None = Field(
        default=None,
        description="Provenance of the extractor that emitted the row.",
    )

    class Settings:
        name = "extraction_dropped_fields"
        indexes = [
            IndexModel(
                [("user_id", 1), ("row_type", 1), ("dropped_field", 1)],
                name="user_type_field",
            ),
            IndexModel(
                [("user_id", 1), ("timestamp", -1)],
                name="user_timestamp_desc",
            ),
        ]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def truncate_raw_row(value: dict[str, Any]) -> dict[str, Any]:
    """Cap the ``raw_row`` payload for :class:`ExtractionRejection`.

    Keys whose individual ``repr`` exceeds :data:`_MAX_RAW_ROW_BYTES`
    are replaced with a truncation marker; the dict shape is preserved
    so audit readers can still see which keys were present.
    """

    return {k: _truncate_for_audit(v, _MAX_RAW_ROW_BYTES) for k, v in value.items()}


def truncate_raw_value(value: Any) -> Any:
    """Cap a single ``raw_value`` for :class:`ExtractionDroppedField`."""

    return _truncate_for_audit(value, _MAX_RAW_VALUE_BYTES)
