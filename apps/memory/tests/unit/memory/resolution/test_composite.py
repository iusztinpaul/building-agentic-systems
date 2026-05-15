"""Tests for :class:`CompositeResolver`."""

from __future__ import annotations

import logging

import pytest
from pytest_mock import MockerFixture

from tree.entities.knowledge_graph import NodeType
from tree.memory.resolution import CompositeResolver
from tree.models.base import BaseEmbeddingModel


class _ScriptedEmbeddingModel(BaseEmbeddingModel):
    """Same as the helper in ``test_semantic`` — duplicated to keep the
    composite tests free of cross-file imports."""

    def __init__(self, scripted: dict[str, list[float]]) -> None:
        self._scripted = scripted
        self.embed_call_count = 0

    @property
    def dimensions(self) -> int:
        return next(iter(self._scripted.values())).__len__() if self._scripted else 2

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_call_count += 1
        out: list[list[float]] = []
        for text in texts:
            if text not in self._scripted:
                raise KeyError(f"unscripted embedding request: {text!r}")
            out.append(self._scripted[text])
        return out


class TestCompositeResolverConstruction:
    def test_constructs_with_all_four_stages_when_embedding_model_passed(
        self,
    ) -> None:
        # Arrange & Act
        model = _ScriptedEmbeddingModel({})
        resolver = CompositeResolver(embedding_model=model)

        # Assert — internal stages are wired.
        assert resolver._alias is not None
        assert resolver._exact is not None
        assert resolver._fuzzy is not None
        assert resolver._semantic is not None

    def test_constructs_without_semantic_when_no_embedding_model(self) -> None:
        # Arrange & Act
        resolver = CompositeResolver(embedding_model=None)

        # Assert
        assert resolver._semantic is None
        assert resolver._fuzzy is not None

    def test_skips_fuzzy_when_rapidfuzz_unavailable_and_logs_once(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Arrange — break rapidfuzz BEFORE construction.
        mocker.patch.dict("sys.modules", {"rapidfuzz": None})

        # Act
        with caplog.at_level(logging.INFO, logger="tree.memory.resolution.composite"):
            resolver = CompositeResolver(embedding_model=None)

        # Assert
        assert resolver._fuzzy is None
        info_logs = [r for r in caplog.records if r.levelno == logging.INFO]
        rapidfuzz_logs = [r for r in info_logs if "rapidfuzz" in r.getMessage()]
        assert len(rapidfuzz_logs) == 1


class TestCompositeResolverChainOrder:
    async def test_alias_wins_over_fuzzy_candidate(self) -> None:
        """Alias short-circuits the chain — even if a fuzzy match exists
        under a different canonical, the alias canonical is returned."""

        # Arrange — "alice" is an alias under canonical "X". The candidate
        # list contains "Alice Smith" (would fuzzy-match) under canonical "Y".
        resolver = CompositeResolver(embedding_model=None)
        existing_aliases = {"X": ["alice"]}
        candidates = ["Alice Smith"]  # canonical "Y" — but alias should win

        # Act
        result = await resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=candidates,
            existing_aliases=existing_aliases,
        )

        # Assert
        assert result.canonical_name == "X"
        assert result.match_type == "alias"

    async def test_alias_short_circuit_skips_semantic(self) -> None:
        """If alias hits, the semantic resolver must never be called."""

        # Arrange
        model = _ScriptedEmbeddingModel({})  # any embed call would KeyError
        resolver = CompositeResolver(embedding_model=model)
        existing_aliases = {"IBM": ["ibm"]}

        # Act
        result = await resolver.resolve(
            "ibm",
            NodeType.PERSON,
            candidate_names=[],
            existing_aliases=existing_aliases,
        )

        # Assert
        assert result.match_type == "alias"
        assert result.canonical_name == "IBM"
        assert model.embed_call_count == 0

    async def test_falls_through_to_exact_when_alias_misses(self) -> None:
        # Arrange
        resolver = CompositeResolver(embedding_model=None)

        # Act — no aliases, but exact candidate matches.
        result = await resolver.resolve(
            "alice smith",
            NodeType.PERSON,
            candidate_names=["Alice Smith"],
            existing_aliases=None,
        )

        # Assert
        assert result.match_type == "exact"
        assert result.canonical_name == "Alice Smith"

    async def test_falls_through_to_fuzzy_when_exact_misses(self) -> None:
        # Arrange
        resolver = CompositeResolver(embedding_model=None, fuzzy_threshold=0.6)

        # Act
        result = await resolver.resolve(
            "alice smyth",
            NodeType.PERSON,
            candidate_names=["Alice Smith"],
            existing_aliases=None,
        )

        # Assert
        assert result.match_type == "fuzzy"
        assert result.canonical_name == "Alice Smith"

    async def test_falls_through_to_semantic_when_fuzzy_misses(self) -> None:
        # Arrange — vectors aligned closely so semantic fires; fuzzy won't
        # match because surface forms are very different.
        model = _ScriptedEmbeddingModel(
            {
                "machine learning": [1.0, 0.0],
                "ML": [0.95, 0.05],
            }
        )
        resolver = CompositeResolver(
            embedding_model=model,
            fuzzy_threshold=0.99,
            semantic_threshold=0.80,
        )

        # Act
        result = await resolver.resolve(
            "machine learning",
            NodeType.PERSON,
            candidate_names=["ML"],
            existing_aliases=None,
        )

        # Assert
        assert result.match_type == "semantic"
        assert result.canonical_name == "ML"


class TestCompositeResolverNoMatch:
    async def test_returns_none_when_no_chain_member_matches(self) -> None:
        # Arrange
        resolver = CompositeResolver(embedding_model=None)

        # Act
        result = await resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["xyzzy plugh"],
            existing_aliases=None,
        )

        # Assert
        assert result.match_type == "none"
        assert result.canonical_name == "alice"
        assert result.confidence == 0.0

    async def test_empty_chain_with_no_candidates_returns_none(self) -> None:
        # Arrange
        resolver = CompositeResolver(embedding_model=None)

        # Act
        result = await resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=[],
            existing_aliases=None,
        )

        # Assert
        assert result.match_type == "none"
        assert result.confidence == 0.0
        assert result.canonical_name == "alice"


class TestCompositeResolverWithTypes:
    async def test_type_strict_blocks_cross_type_match(self) -> None:
        """A PERSON named "Alice" must not match a TASK named "Alice"."""

        # Arrange
        resolver = CompositeResolver(embedding_model=None, type_strict=True)

        # Act
        results = await resolver.resolve_with_types(
            entities=[("Alice", NodeType.PERSON)],
            existing_entities={
                NodeType.PERSON: [],
                NodeType.TASK: ["Alice"],
            },
            existing_aliases={},
        )

        # Assert
        assert len(results) == 1
        assert results[0].match_type == "none"
        assert results[0].canonical_name == "Alice"

    async def test_type_strict_off_unions_across_types(self) -> None:
        # Arrange
        resolver = CompositeResolver(embedding_model=None, type_strict=False)

        # Act
        results = await resolver.resolve_with_types(
            entities=[("Alice", NodeType.PERSON)],
            existing_entities={
                NodeType.PERSON: [],
                NodeType.TASK: ["Alice"],
            },
            existing_aliases={},
        )

        # Assert — non-strict allows the TASK candidate into PERSON's pool.
        assert results[0].match_type == "exact"
        assert results[0].canonical_name == "Alice"

    async def test_within_type_match_still_works(self) -> None:
        # Arrange
        resolver = CompositeResolver(embedding_model=None, type_strict=True)

        # Act
        results = await resolver.resolve_with_types(
            entities=[("alice", NodeType.PERSON)],
            existing_entities={NodeType.PERSON: ["Alice"]},
            existing_aliases={},
        )

        # Assert
        assert results[0].match_type == "exact"
        assert results[0].canonical_name == "Alice"


class TestCompositeResolverBatchIdempotency:
    async def test_repeated_inputs_yield_identical_canonicals(self) -> None:
        # Arrange
        resolver = CompositeResolver(embedding_model=None)
        candidates = ["Alice Smith", "Bob"]
        inputs = [
            ("alice smith", NodeType.PERSON),
            ("bob", NodeType.PERSON),
            ("alice smith", NodeType.PERSON),  # repeat
            ("bob", NodeType.PERSON),  # repeat
        ]

        # Act
        first = await resolver.resolve_batch(inputs, candidates)
        second = await resolver.resolve_batch(inputs, candidates)

        # Assert
        assert [r.canonical_name for r in first] == [r.canonical_name for r in second]
        assert first[0].canonical_name == first[2].canonical_name
        assert first[1].canonical_name == first[3].canonical_name


class TestCompositeResolverFindMatchesStub:
    def test_find_matches_raises_with_section_ref(self) -> None:
        # Arrange
        resolver = CompositeResolver(embedding_model=None)

        # Act / Assert
        with pytest.raises(NotImplementedError) as excinfo:
            resolver.find_matches("alice", NodeType.PERSON)
        assert "RESOLUTION_MODULE.md §7.4" in str(excinfo.value)
