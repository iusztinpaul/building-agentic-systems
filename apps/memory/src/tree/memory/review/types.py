"""Public types for the human-review surface (#014).

Three dataclasses + one StrEnum describe the inputs and outputs of the
review API:

* :class:`PendingDuplicate` — one row returned by
  :func:`find_pending_duplicates`. Hydrated from the SAME_AS edge plus its
  two endpoints.
* :class:`ReviewDecision` — what the human (or external agent) decided:
  ``CONFIRM`` (merge) or ``REJECT`` (mark as not-a-duplicate).
* :class:`ReviewResult` — what :func:`review_duplicate` returned: which
  node won, which lost, how many edges were transferred, and the
  audit-trail edge id.

The merge strategy enum is imported from
:mod:`tree.memory.extraction.dedup` and re-exported here so callers of the
review API can pick a strategy without having to reach into the dedup
module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from tree.entities.knowledge_graph import NodeType
from tree.memory.extraction.dedup import MergeStrategy


__all__ = [
    "MergeStrategy",
    "PendingDuplicate",
    "ReviewDecision",
    "ReviewResult",
]


@dataclass(frozen=True)
class PendingDuplicate:
    """A SAME_AS edge with ``status="pending"`` plus hydrated endpoint info.

    Returned by :func:`find_pending_duplicates`. ``edge_id`` is the SAME_AS
    edge's ``_id`` (the audit trail). ``similarity_score`` is the score
    written by ``dedupe_entity`` at flagging time.
    """

    source_node_id: str
    target_node_id: str
    source_name: str
    target_name: str
    entity_type: NodeType
    similarity_score: float
    match_type: Literal["embedding", "fuzzy", "both"]
    flagged_at: datetime
    edge_id: str


class ReviewDecision(StrEnum):
    """Outcome the reviewer chose for a flagged SAME_AS pair."""

    CONFIRM = "confirm"
    REJECT = "reject"


@dataclass
class ReviewResult:
    """The audit record returned by :func:`review_duplicate`.

    On ``CONFIRM`` all of ``winner_node_id``, ``loser_node_id`` and
    ``applied_strategy`` are populated; ``edges_transferred`` counts how
    many non-SAME_AS edges were re-keyed from loser to winner.

    On ``REJECT`` ``winner_node_id``, ``loser_node_id`` and
    ``applied_strategy`` are ``None`` and ``edges_transferred`` is ``0``.

    ``same_as_edge_id`` is always populated — it points at the SAME_AS
    audit edge so callers can re-query the persisted state.
    """

    decision: ReviewDecision
    winner_node_id: str | None
    loser_node_id: str | None
    applied_strategy: MergeStrategy | None
    edges_transferred: int
    same_as_edge_id: str
