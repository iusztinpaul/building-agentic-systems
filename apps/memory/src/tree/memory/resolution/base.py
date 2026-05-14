"""Base protocol and abstract class for resolvers.

A resolver maps an input name + entity type to a :class:`ResolvedEntity`.
The chain composition (alias → exact → fuzzy → semantic) is wired in #009;
this module exposes only the interface every concrete resolver implements.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

from tree.entities.knowledge_graph import NodeType

from tree.memory.resolution.types import ResolvedEntity, _normalize


@runtime_checkable
class BaseResolver(Protocol):
    """Structural type for the resolver chain. Useful for typing."""

    def resolve(
        self,
        name: str,
        entity_type: NodeType,
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> ResolvedEntity: ...

    def resolve_batch(
        self,
        entities: Iterable[tuple[str, NodeType]],
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> list[ResolvedEntity]: ...


class AbstractResolver(ABC):
    """Concrete ABC every resolver in this package inherits from.

    Concrete classes implement :meth:`resolve`. The default
    :meth:`resolve_batch` loops over inputs sequentially — subclasses can
    override for vectorized backends (e.g. embedding-based search).
    """

    @staticmethod
    def _normalize(text: str) -> str:
        """Delegate to :func:`tree.memory.resolution.types._normalize`."""

        return _normalize(text)

    @abstractmethod
    def resolve(
        self,
        name: str,
        entity_type: NodeType,
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> ResolvedEntity:
        """Return a :class:`ResolvedEntity` for ``name``.

        Implementations MUST return a fully-populated :class:`ResolvedEntity`
        even when no match is found (``match_type="none"``,
        ``confidence=0.0``, ``canonical_name=name``).
        """

    def resolve_batch(
        self,
        entities: Iterable[tuple[str, NodeType]],
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> list[ResolvedEntity]:
        """Resolve each ``(name, entity_type)`` tuple in order.

        Materializes ``candidate_names`` once because it may be a generator
        that callers expect to reuse across every input.
        """

        candidates = list(candidate_names)
        return [
            self.resolve(name, entity_type, candidates, existing_aliases)
            for name, entity_type in entities
        ]

    def _no_match(self, name: str, entity_type: NodeType) -> ResolvedEntity:
        """Construct the standard "no match" :class:`ResolvedEntity`."""

        return ResolvedEntity(
            original_name=name,
            canonical_name=name,
            entity_type=entity_type,
            confidence=0.0,
            match_type="none",
        )
