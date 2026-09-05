"""Unit tests for the resolver interface (``…graph.resolution.base``).

The concrete resolvers are tested in their own modules; what is only testable
here is what the ABC gives every subclass for free — the sequential
``resolve_batch`` (which must materialise a generator of candidates so the
second input still sees them) and the standard ``_no_match`` envelope.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from tree.entities.memory import NodeType
from tree.memory.graph.resolution.base import AbstractResolver, BaseResolver
from tree.memory.graph.resolution.types import ResolvedEntity


class _EchoResolver(AbstractResolver):
    """Minimal subclass: matches iff the name is among the candidates."""

    def __init__(self) -> None:
        self.seen_candidates: list[list[str]] = []

    def resolve(
        self,
        name: str,
        entity_type: NodeType,
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> ResolvedEntity:
        candidates = list(candidate_names)
        self.seen_candidates.append(candidates)
        if name in candidates:
            return ResolvedEntity(
                original_name=name,
                canonical_name=name,
                entity_type=entity_type,
                confidence=1.0,
                match_type="exact",
            )
        return self._no_match(name, entity_type)


class TestNoMatchEnvelope:
    def test_no_match_is_fully_populated(self) -> None:
        """Callers must never branch on ``None`` — a miss is still an entity."""

        result = _EchoResolver()._no_match("Alice", NodeType.PERSON)

        assert result.original_name == "Alice"
        assert result.canonical_name == "Alice"
        assert result.entity_type == NodeType.PERSON
        assert result.confidence == 0.0
        assert result.match_type == "none"


class TestResolveBatch:
    def test_returns_one_result_per_input_in_order(self) -> None:
        results = _EchoResolver().resolve_batch(
            [("Alice", NodeType.PERSON), ("Bob", NodeType.PERSON)],
            ["Alice"],
        )

        assert [r.original_name for r in results] == ["Alice", "Bob"]
        assert [r.match_type for r in results] == ["exact", "none"]

    def test_a_generator_of_candidates_is_materialised_once(self) -> None:
        """A one-shot iterable must not empty out after the first input.

        Callers pass ``(row["name"] for row in cursor)``; without the
        ``list(...)`` in the ABC the second entity would silently resolve
        against zero candidates.
        """

        resolver = _EchoResolver()

        resolver.resolve_batch(
            [("Alice", NodeType.PERSON), ("Alice", NodeType.PERSON)],
            (name for name in ["Alice"]),
        )

        assert resolver.seen_candidates == [["Alice"], ["Alice"]]

    def test_an_empty_input_list_yields_no_results(self) -> None:
        assert _EchoResolver().resolve_batch([], ["Alice"]) == []


class TestBaseResolverProtocol:
    def test_a_concrete_resolver_satisfies_the_structural_type(self) -> None:
        assert isinstance(_EchoResolver(), BaseResolver)
