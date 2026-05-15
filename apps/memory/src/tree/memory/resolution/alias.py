"""Alias-based resolver.

Walks an ``existing_aliases`` mapping ``{canonical_name: [alias, ...]}`` and
returns the first canonical whose alias list (normalized) contains the input.
Used first in the chain — alias matches win over exact and fuzzy because they
encode previously-confirmed identity.
"""

from collections.abc import Iterable, Mapping

from tree.entities.knowledge_graph import NodeType

from tree.memory.resolution.base import AbstractResolver
from tree.memory.resolution.types import ResolvedEntity


class AliasMatchResolver(AbstractResolver):
    """Resolve via a pre-computed canonical → aliases mapping."""

    def resolve(
        self,
        name: str,
        entity_type: NodeType,
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> ResolvedEntity:
        if not existing_aliases:
            return self._no_match(name, entity_type)

        normalized = self._normalize(name)
        for canonical, aliases in existing_aliases.items():
            for alias in aliases:
                if self._normalize(alias) == normalized:
                    return ResolvedEntity(
                        original_name=name,
                        canonical_name=canonical,
                        entity_type=entity_type,
                        confidence=1.0,
                        match_type="alias",
                    )

        return self._no_match(name, entity_type)
