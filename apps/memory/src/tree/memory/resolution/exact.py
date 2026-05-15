"""Exact-match resolver.

Case-insensitive, whitespace-collapsed equality against ``candidate_names``.
Returns the first matching candidate with its ORIGINAL casing.
"""

from collections.abc import Iterable, Mapping

from tree.entities.knowledge_graph import NodeType

from tree.memory.resolution.base import AbstractResolver
from tree.memory.resolution.types import ResolvedEntity


class ExactMatchResolver(AbstractResolver):
    """Resolve via normalized equality with a candidate name."""

    def resolve(
        self,
        name: str,
        entity_type: NodeType,
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> ResolvedEntity:
        normalized = self._normalize(name)
        for candidate in candidate_names:
            if self._normalize(candidate) == normalized:
                return ResolvedEntity(
                    original_name=name,
                    canonical_name=candidate,
                    entity_type=entity_type,
                    confidence=1.0,
                    match_type="exact",
                )

        return self._no_match(name, entity_type)
