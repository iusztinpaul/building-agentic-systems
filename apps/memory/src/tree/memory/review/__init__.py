"""Human-review API for flagged SAME_AS pairs.

Re-exports the public surface:

* :class:`PendingDuplicate`, :class:`ReviewDecision`,
  :class:`ReviewResult` — data structures.
* :class:`MergeStrategy` — re-exported from
  :mod:`tree.memory.extraction.dedup` so callers of the review API don't
  need to import from two places.
* :func:`find_pending_duplicates`, :func:`review_duplicate`,
  :func:`get_same_as_cluster` — the three async functions.
"""

from tree.memory.review.core import (
    find_pending_duplicates,
    get_same_as_cluster,
    review_duplicate,
)
from tree.memory.review.types import (
    MergeStrategy,
    PendingDuplicate,
    ReviewDecision,
    ReviewResult,
)

__all__ = [
    "MergeStrategy",
    "PendingDuplicate",
    "ReviewDecision",
    "ReviewResult",
    "find_pending_duplicates",
    "get_same_as_cluster",
    "review_duplicate",
]
