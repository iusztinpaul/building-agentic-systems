"""Tests for :class:`AliasMatchResolver`."""

import pytest

from tree.entities.knowledge_graph import NodeType
from tree.memory.resolution import AliasMatchResolver, ResolvedEntity


@pytest.fixture
def resolver() -> AliasMatchResolver:
    return AliasMatchResolver()


class TestAliasMatchResolver:
    def test_returns_canonical_when_alias_matches(
        self, resolver: AliasMatchResolver
    ) -> None:
        # Arrange
        existing_aliases = {"Alice Smith": ["alice", "as"]}

        # Act
        result = resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["Alice Smith"],
            existing_aliases=existing_aliases,
        )

        # Assert
        assert isinstance(result, ResolvedEntity)
        assert result.canonical_name == "Alice Smith"
        assert result.original_name == "alice"
        assert result.confidence == 1.0
        assert result.match_type == "alias"
        assert result.entity_type == NodeType.PERSON

    def test_alias_match_is_case_and_whitespace_insensitive(
        self, resolver: AliasMatchResolver
    ) -> None:
        # Arrange
        existing_aliases = {"International Machines": ["  IBM ", "ibm corp"]}

        # Act
        result = resolver.resolve(
            "ibm",
            NodeType.PERSON,
            candidate_names=[],
            existing_aliases=existing_aliases,
        )

        # Assert
        assert result.canonical_name == "International Machines"
        assert result.match_type == "alias"

    def test_alias_wins_over_exact_candidate_with_different_canonical(
        self, resolver: AliasMatchResolver
    ) -> None:
        """When ``name`` is also an exact candidate under a different
        canonical, AliasMatchResolver still returns the alias canonical.
        The chain in #009 calls alias first; this test guards against a
        regression where alias would defer to exact-name shape."""

        # Arrange: "alice" is an alias under "Alice Smith", and also appears
        # as a literal candidate name "alice" — but Alias resolves to the
        # canonical key, NOT the candidate.
        existing_aliases = {"Alice Smith": ["alice"]}

        # Act
        result = resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["alice"],
            existing_aliases=existing_aliases,
        )

        # Assert
        assert result.canonical_name == "Alice Smith"
        assert result.match_type == "alias"

    @pytest.mark.parametrize("aliases", [None, {}])
    def test_no_match_when_aliases_empty_or_none(
        self,
        resolver: AliasMatchResolver,
        aliases: dict[str, list[str]] | None,
    ) -> None:
        # Act
        result = resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["Alice Smith"],
            existing_aliases=aliases,
        )

        # Assert
        assert result.match_type == "none"
        assert result.confidence == 0.0
        assert result.canonical_name == "alice"

    def test_no_match_when_name_not_in_any_alias_list(
        self, resolver: AliasMatchResolver
    ) -> None:
        # Arrange
        existing_aliases = {"Alice Smith": ["alice", "as"]}

        # Act
        result = resolver.resolve(
            "bob",
            NodeType.PERSON,
            candidate_names=[],
            existing_aliases=existing_aliases,
        )

        # Assert
        assert result.match_type == "none"
        assert result.canonical_name == "bob"

    def test_resolve_batch_returns_one_result_per_input_in_order(
        self, resolver: AliasMatchResolver
    ) -> None:
        # Arrange
        existing_aliases = {"Alice Smith": ["alice"], "Bob Jones": ["bj"]}
        inputs = [
            ("alice", NodeType.PERSON),
            ("bj", NodeType.PERSON),
            ("unknown", NodeType.PERSON),
        ]

        # Act
        results = resolver.resolve_batch(
            inputs,
            candidate_names=[],
            existing_aliases=existing_aliases,
        )

        # Assert
        assert [r.canonical_name for r in results] == [
            "Alice Smith",
            "Bob Jones",
            "unknown",
        ]
        assert [r.match_type for r in results] == ["alias", "alias", "none"]
