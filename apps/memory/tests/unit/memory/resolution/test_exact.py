"""Tests for :class:`ExactMatchResolver`."""

import pytest

from tree.entities.knowledge_graph import NodeType
from tree.memory.resolution import ExactMatchResolver, ResolvedEntity


@pytest.fixture
def resolver() -> ExactMatchResolver:
    return ExactMatchResolver()


class TestExactMatchResolver:
    def test_case_insensitive_match_preserves_candidate_casing(
        self, resolver: ExactMatchResolver
    ) -> None:
        # Act
        result = resolver.resolve(
            "Alice",
            NodeType.PERSON,
            candidate_names=["alice"],
        )

        # Assert
        assert isinstance(result, ResolvedEntity)
        assert result.canonical_name == "alice"
        assert result.original_name == "Alice"
        assert result.confidence == 1.0
        assert result.match_type == "exact"

    @pytest.mark.parametrize(
        "name,candidates",
        [
            ("ALICE", ["alice"]),
            ("alice", ["ALICE"]),
            ("  Alice  Smith  ", ["alice smith"]),
            ("alice smith", ["  Alice   Smith  "]),
        ],
    )
    def test_normalization_collapses_case_and_whitespace(
        self,
        resolver: ExactMatchResolver,
        name: str,
        candidates: list[str],
    ) -> None:
        # Act
        result = resolver.resolve(name, NodeType.PERSON, candidate_names=candidates)

        # Assert
        assert result.match_type == "exact"
        assert result.canonical_name == candidates[0]

    def test_no_match_when_candidates_empty(self, resolver: ExactMatchResolver) -> None:
        # Act
        result = resolver.resolve("ALICE", NodeType.PERSON, candidate_names=[])

        # Assert
        assert result.match_type == "none"
        assert result.confidence == 0.0
        assert result.canonical_name == "ALICE"

    def test_no_match_when_no_candidate_equals_name(
        self, resolver: ExactMatchResolver
    ) -> None:
        # Act
        result = resolver.resolve(
            "Alice",
            NodeType.PERSON,
            candidate_names=["Bob", "Charlie"],
        )

        # Assert
        assert result.match_type == "none"

    def test_returns_first_matching_candidate(
        self, resolver: ExactMatchResolver
    ) -> None:
        # Arrange — two normalize-equal candidates; first one wins.
        candidates = ["Alice Smith", "alice smith", "ALICE  SMITH"]

        # Act
        result = resolver.resolve(
            "alice smith",
            NodeType.PERSON,
            candidate_names=candidates,
        )

        # Assert
        assert result.canonical_name == "Alice Smith"

    def test_resolve_batch_returns_one_result_per_input_in_order(
        self, resolver: ExactMatchResolver
    ) -> None:
        # Arrange
        inputs = [
            ("alice", NodeType.PERSON),
            ("bob", NodeType.PERSON),
            ("ghost", NodeType.PERSON),
        ]

        # Act
        results = resolver.resolve_batch(
            inputs,
            candidate_names=["Alice", "Bob"],
        )

        # Assert
        assert [r.match_type for r in results] == ["exact", "exact", "none"]
        assert [r.canonical_name for r in results] == ["Alice", "Bob", "ghost"]
