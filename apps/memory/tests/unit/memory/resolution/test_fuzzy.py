"""Tests for :class:`FuzzyMatchResolver`."""

from typing import Any

import pytest
from pytest_mock import MockerFixture

from tree.entities.knowledge_graph import NodeType
from tree.memory.resolution import FuzzyMatchResolver, ResolvedEntity


@pytest.fixture
def resolver() -> FuzzyMatchResolver:
    return FuzzyMatchResolver()


class TestFuzzyMatchResolverAvailability:
    def test_is_available_when_rapidfuzz_imports(
        self, resolver: FuzzyMatchResolver
    ) -> None:
        # Assert — rapidfuzz is a hard dep, so the default instance is ready.
        assert resolver.is_available is True

    def test_falls_back_when_rapidfuzz_missing(self, mocker: MockerFixture) -> None:
        # Arrange — break the import BEFORE the resolver constructs.
        mocker.patch.dict("sys.modules", {"rapidfuzz": None})

        # Act
        instance = FuzzyMatchResolver()
        result = instance.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["Alice Smith"],
        )

        # Assert
        assert instance.is_available is False
        assert result.match_type == "none"
        assert result.canonical_name == "alice"
        assert result.confidence == 0.0


class TestFuzzyMatchResolverScoring:
    def test_highest_score_above_threshold_wins(
        self, resolver: FuzzyMatchResolver
    ) -> None:
        """Regression guard: the resolver must pick the BEST score above
        threshold, not the first one that clears it."""

        # Arrange
        candidates = ["Alyce Smyth", "Alice Smyth", "Bob"]

        # Act
        result = resolver.resolve(
            "alice smith",
            NodeType.PERSON,
            candidate_names=candidates,
        )

        # Assert — "Alice Smyth" is closer to "alice smith" than "Alyce Smyth".
        assert isinstance(result, ResolvedEntity)
        assert result.canonical_name == "Alice Smyth"
        assert result.match_type == "fuzzy"
        assert result.confidence >= 0.85
        assert result.confidence <= 1.0

    def test_no_match_when_best_score_below_threshold(self) -> None:
        # Arrange
        instance = FuzzyMatchResolver(threshold=0.85)

        # Act
        result = instance.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["Robert"],
        )

        # Assert
        assert result.match_type == "none"
        assert result.confidence == 0.0

    def test_no_match_when_candidates_empty(self, resolver: FuzzyMatchResolver) -> None:
        # Act
        result = resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=[],
        )

        # Assert
        assert result.match_type == "none"

    def test_preserves_original_candidate_casing(
        self, resolver: FuzzyMatchResolver
    ) -> None:
        # Act
        result = resolver.resolve(
            "alice smith",
            NodeType.PERSON,
            candidate_names=["ALICE  SMITH"],
        )

        # Assert
        assert result.match_type == "fuzzy"
        assert result.canonical_name == "ALICE  SMITH"

    @pytest.mark.parametrize(
        "scorer_name",
        ["token_sort_ratio", "ratio", "WRatio"],
    )
    def test_custom_scorer_can_be_selected(self, scorer_name: str) -> None:
        # Arrange
        instance = FuzzyMatchResolver(threshold=0.85, scorer_name=scorer_name)

        # Act
        result = instance.resolve(
            "alice smith",
            NodeType.PERSON,
            candidate_names=["Alice Smith"],
        )

        # Assert
        assert instance.is_available is True
        assert result.match_type == "fuzzy"
        assert result.canonical_name == "Alice Smith"

    @pytest.mark.parametrize(
        "threshold,expected_match_type",
        [
            (0.5, "fuzzy"),
            (0.99, "none"),
        ],
    )
    def test_threshold_controls_match(
        self,
        threshold: float,
        expected_match_type: str,
    ) -> None:
        # Arrange
        instance = FuzzyMatchResolver(threshold=threshold)

        # Act
        result = instance.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["alicia"],
        )

        # Assert
        assert result.match_type == expected_match_type

    def test_resolve_batch_returns_one_result_per_input_in_order(
        self, resolver: FuzzyMatchResolver
    ) -> None:
        # Arrange
        inputs = [
            ("alice smith", NodeType.PERSON),
            ("totally unrelated xyzzy", NodeType.PERSON),
        ]

        # Act
        results: list[Any] = resolver.resolve_batch(
            inputs,
            candidate_names=["Alice Smith"],
        )

        # Assert
        assert len(results) == 2
        assert results[0].match_type == "fuzzy"
        assert results[0].canonical_name == "Alice Smith"
        assert results[1].match_type == "none"
