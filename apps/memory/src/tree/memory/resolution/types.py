"""Resolution types and the shared ``_normalize`` helper.

The Tree memory's resolver chain (``alias`` → ``exact`` → ``fuzzy`` → ``semantic``)
operates on entity names. Every resolver returns a :class:`ResolvedEntity`
regardless of whether a match was found — callers branch on ``match_type``
instead of on exceptions or ``None``.
"""

from typing import Literal

from pydantic import BaseModel, Field

from tree.entities.knowledge_graph import NodeType


def _normalize(name: str) -> str:
    """Return a normalized form of ``name``: lowercase + whitespace-collapsed.

    Example: ``" Alice   Smith "`` → ``"alice smith"``.

    This function is the single source of truth for canonical-form keys used
    across the resolver chain. The implementation is intentionally identical
    to the reference Neo4j agent-memory port so chain-ordering behaves
    predictably.
    """

    return " ".join(name.strip().lower().split())


class ResolvedEntity(BaseModel):
    """The output of any resolver call.

    Resolvers never raise on "no match" — they return an instance with
    ``match_type="none"`` and ``confidence=0.0`` so callers can branch on the
    field instead of on exceptions.
    """

    original_name: str
    canonical_name: str
    entity_type: NodeType
    confidence: float
    match_type: Literal["alias", "exact", "fuzzy", "semantic", "batch", "none"]
    merged_from: list[str] = Field(default_factory=list)


class ResolutionMatch(BaseModel):
    """A single candidate considered by a resolver.

    Used by the semantic and composite resolvers (#009) to surface
    near-misses; the base resolvers in this module do not yet emit these.
    """

    candidate_name: str
    similarity_score: float
    match_type: Literal["alias", "exact", "fuzzy", "semantic"]
