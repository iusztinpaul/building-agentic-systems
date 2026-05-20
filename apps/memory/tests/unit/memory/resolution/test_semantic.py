"""Tests for :class:`SemanticMatchResolver`."""

from __future__ import annotations

import pytest

from tree.entities.knowledge_graph import NodeType
from tree.memory.resolution import ResolvedEntity, SemanticMatchResolver
from tree.models.base import BaseEmbeddingModel


class _ScriptedEmbeddingModel(BaseEmbeddingModel):
    """Returns a pre-scripted embedding per input text.

    Lets us test cosine-similarity outcomes deterministically.
    """

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


class _CountingEmbeddingModel(BaseEmbeddingModel):
    """Returns a vector derived from the text so we can count unique inputs.

    The vector depends only on text identity, so cache hits return identical
    embeddings to misses — the goal here is to exercise the LRU, not the
    similarity math.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return 2

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        # Stable, distinct, finite-norm vector per text.
        return [[float(len(t)), float(sum(ord(c) for c in t))] for t in texts]


class TestSemanticMatchResolverBasic:
    async def test_returns_highest_above_threshold(self) -> None:
        # Arrange
        model = _ScriptedEmbeddingModel(
            {
                "alice": [1.0, 0.0],
                "Alice Smith": [0.95, 0.05],  # very close
                "Bob": [0.0, 1.0],  # orthogonal
            }
        )
        resolver = SemanticMatchResolver(model, threshold=0.80)

        # Act
        result = await resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["Alice Smith", "Bob"],
        )

        # Assert
        assert isinstance(result, ResolvedEntity)
        assert result.match_type == "semantic"
        assert result.canonical_name == "Alice Smith"
        assert result.confidence >= 0.80
        assert result.confidence <= 1.0
        assert result.entity_type == NodeType.PERSON

    async def test_no_match_when_all_below_threshold(self) -> None:
        # Arrange — orthogonal vectors → cosine = 0.0, below 0.80.
        model = _ScriptedEmbeddingModel(
            {
                "alice": [1.0, 0.0],
                "completely unrelated": [0.0, 1.0],
            }
        )
        resolver = SemanticMatchResolver(model, threshold=0.80)

        # Act
        result = await resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["completely unrelated"],
        )

        # Assert
        assert result.match_type == "none"
        assert result.canonical_name == "alice"
        assert result.confidence == 0.0

    async def test_no_match_when_candidate_list_empty(self) -> None:
        # Arrange
        model = _ScriptedEmbeddingModel({"alice": [1.0, 0.0]})
        resolver = SemanticMatchResolver(model, threshold=0.80)

        # Act
        result = await resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=[],
        )

        # Assert
        assert result.match_type == "none"
        # The embed should not have been invoked when there are no candidates.
        assert model.embed_call_count == 0

    async def test_resolve_batch_returns_one_per_input(self) -> None:
        # Arrange
        model = _ScriptedEmbeddingModel(
            {
                "alice": [1.0, 0.0],
                "bob": [0.0, 1.0],
                "Alice Smith": [0.95, 0.05],
                "Robert": [0.05, 0.95],
            }
        )
        resolver = SemanticMatchResolver(model, threshold=0.80)

        # Act
        results = await resolver.resolve_batch(
            entities=[
                ("alice", NodeType.PERSON),
                ("bob", NodeType.PERSON),
            ],
            candidate_names=["Alice Smith", "Robert"],
        )

        # Assert
        assert [r.canonical_name for r in results] == ["Alice Smith", "Robert"]
        assert all(r.match_type == "semantic" for r in results)


class TestSemanticMatchResolverClamping:
    async def test_clamps_negative_floating_point_artifact_to_zero(self) -> None:
        """Cosine on these vectors is mathematically -0.0 but numerically
        may produce a tiny negative — resolver must not propagate it."""

        # Arrange — make the math produce sub-zero with a high-threshold model.
        # We deliberately pass vectors whose dot product is negative.
        model = _ScriptedEmbeddingModel(
            {
                "alice": [1.0, 0.0],
                "weird": [-1.0e-9, 1.0],  # dot ≈ -1e-9 → cosine ≈ -1e-9
            }
        )
        resolver = SemanticMatchResolver(model, threshold=0.0)

        # Act
        result = await resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["weird"],
        )

        # Assert — must NOT raise, and the score (whether matched or not)
        # must be in [0.0, 1.0]. Since the math goes negative and threshold
        # is 0.0, the candidate is rejected (we only accept score > best_score
        # which starts at 0.0). The important behavior is no crash, no
        # negative confidence.
        assert result.confidence >= 0.0
        assert result.confidence <= 1.0

    async def test_clamp_via_static_helper(self) -> None:
        # Arrange — small negative cosine should clamp to 0.0.
        a = [1.0, 0.0]
        b = [-1.0e-9, 1.0]

        # Act
        score = SemanticMatchResolver._cosine_similarity(a, b)

        # Assert
        assert score == 0.0


class TestSemanticMatchResolverLRU:
    async def test_cache_size_capped_after_overflow(self) -> None:
        # Arrange
        cache_max = 50
        model = _CountingEmbeddingModel()
        resolver = SemanticMatchResolver(
            model, threshold=0.80, cache_max_size=cache_max
        )

        # Act — exercise the cache directly so we test eviction, not
        # whole-resolve plumbing.
        for i in range(cache_max + 1000):
            await resolver._embed_cached(f"name_{i}")

        # Assert
        assert len(resolver._cache) == cache_max

    async def test_lru_eviction_keeps_recently_accessed(self) -> None:
        """Insert k0..kN, touch k0 to refresh it, insert one more, assert
        the OLDEST non-touched key (k1) was evicted — not k0."""

        # Arrange
        cache_max = 4  # tiny for clarity
        model = _CountingEmbeddingModel()
        resolver = SemanticMatchResolver(
            model, threshold=0.80, cache_max_size=cache_max
        )

        # Act
        for i in range(cache_max):
            await resolver._embed_cached(f"k{i}")  # fills the cache
        assert list(resolver._cache.keys()) == ["k0", "k1", "k2", "k3"]
        await resolver._embed_cached("k0")  # k0 → most-recently-used
        await resolver._embed_cached("k_new")  # forces one eviction

        # Assert — k1 was the oldest after k0 was bumped, so it goes.
        assert "k0" in resolver._cache
        assert "k1" not in resolver._cache
        assert "k_new" in resolver._cache
        assert len(resolver._cache) == cache_max

    async def test_clear_cache_empties_and_forces_recompute(self) -> None:
        # Arrange
        model = _CountingEmbeddingModel()
        resolver = SemanticMatchResolver(model, threshold=0.80, cache_max_size=10)
        await resolver._embed_cached("alice")
        await resolver._embed_cached("bob")
        assert len(resolver._cache) == 2
        calls_before = len(model.calls)

        # Act
        resolver.clear_cache()
        assert len(resolver._cache) == 0
        await resolver._embed_cached("alice")  # should recompute now

        # Assert
        assert len(model.calls) == calls_before + 1
        assert "alice" in resolver._cache

    async def test_cache_key_is_normalized(self) -> None:
        """Case/whitespace variants share a cache slot."""

        # Arrange
        model = _CountingEmbeddingModel()
        resolver = SemanticMatchResolver(model, threshold=0.80, cache_max_size=10)

        # Act
        await resolver._embed_cached("Alice")
        calls_after_first = len(model.calls)
        await resolver._embed_cached("  alice  ")
        await resolver._embed_cached("ALICE")

        # Assert
        assert len(model.calls) == calls_after_first  # no extra calls
        assert list(resolver._cache.keys()) == ["alice"]


class TestSemanticMatchResolverPrewarm:
    """#044 — ``prewarm_cache`` batches all uncached names into ONE request
    and seeds the LRU, so the subsequent cosine loop is pure cache hits."""

    async def test_prewarm_embeds_uncached_names_in_one_batched_request(
        self,
    ) -> None:
        # Arrange
        model = _CountingEmbeddingModel()
        resolver = SemanticMatchResolver(model, threshold=0.80, cache_max_size=100)

        # Act — pre-warm five candidate names.
        await resolver.prewarm_cache(["alice", "bob", "carol", "dave", "erin"])

        # Assert — exactly ONE embed() call carrying all five names (not five
        # separate one-name calls).
        assert len(model.calls) == 1
        assert model.calls[0] == ["alice", "bob", "carol", "dave", "erin"]
        # All five seeded into the cache.
        assert set(resolver._cache.keys()) == {
            "alice",
            "bob",
            "carol",
            "dave",
            "erin",
        }

    async def test_prewarm_then_resolve_makes_no_further_requests(self) -> None:
        # Arrange
        model = _CountingEmbeddingModel()
        resolver = SemanticMatchResolver(model, threshold=0.0, cache_max_size=100)

        # Act — warm input + candidates, then resolve against the warmed cache.
        await resolver.prewarm_cache(["alice", "Alice Smith", "Bob"])
        calls_after_prewarm = len(model.calls)
        await resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["Alice Smith", "Bob"],
        )

        # Assert — the cosine loop hit the cache only; no extra embed() calls.
        assert len(model.calls) == calls_after_prewarm == 1

    async def test_prewarm_skips_already_cached_names(self) -> None:
        # Arrange — seed "alice" via a first prewarm.
        model = _CountingEmbeddingModel()
        resolver = SemanticMatchResolver(model, threshold=0.80, cache_max_size=100)
        await resolver.prewarm_cache(["alice"])
        assert len(model.calls) == 1

        # Act — a second prewarm overlapping the cached name.
        await resolver.prewarm_cache(["alice", "bob"])

        # Assert — only the uncached "bob" was embedded in the second call.
        assert len(model.calls) == 2
        assert model.calls[1] == ["bob"]

    async def test_prewarm_dedups_normalized_keys(self) -> None:
        # Arrange — case/whitespace variants share one normalized slot.
        model = _CountingEmbeddingModel()
        resolver = SemanticMatchResolver(model, threshold=0.80, cache_max_size=100)

        # Act
        await resolver.prewarm_cache(["Alice", "  alice ", "ALICE"])

        # Assert — embedded once.
        assert len(model.calls) == 1
        assert len(model.calls[0]) == 1
        assert list(resolver._cache.keys()) == ["alice"]

    async def test_prewarm_respects_lru_bound(self) -> None:
        # Arrange — warm more names than the cache can hold.
        cache_max = 3
        model = _CountingEmbeddingModel()
        resolver = SemanticMatchResolver(
            model, threshold=0.80, cache_max_size=cache_max
        )

        # Act
        await resolver.prewarm_cache(["a", "b", "c", "d", "e"])

        # Assert — cache trimmed to the bound (oldest evicted first).
        assert len(resolver._cache) == cache_max

    async def test_prewarm_empty_is_noop(self) -> None:
        # Arrange
        model = _CountingEmbeddingModel()
        resolver = SemanticMatchResolver(model, threshold=0.80, cache_max_size=10)

        # Act
        await resolver.prewarm_cache([])

        # Assert
        assert model.calls == []
        assert len(resolver._cache) == 0


class TestSemanticMatchResolverWithMockEmbeddingModel:
    """One smoke test exercising the bundled MockEmbeddingModel.

    The MockEmbeddingModel returns random vectors so we can't assert on
    canonical_name, but we can assert the resolver doesn't crash and emits
    a well-formed ResolvedEntity.
    """

    async def test_with_mock_embedding_model_returns_well_formed_result(
        self,
    ) -> None:
        # Arrange — local import keeps the test independent of MockEmbeddingModel
        # construction failures.
        from tree.models.fake_model import MockEmbeddingModel

        resolver = SemanticMatchResolver(
            MockEmbeddingModel(dimensions=8),
            threshold=0.0,  # accept anything so we test the plumbing
        )

        # Act
        result = await resolver.resolve(
            "alice",
            NodeType.PERSON,
            candidate_names=["Alice Smith", "Bob"],
        )

        # Assert
        assert isinstance(result, ResolvedEntity)
        assert result.match_type in {"semantic", "none"}
        assert 0.0 <= result.confidence <= 1.0
        assert result.entity_type == NodeType.PERSON


@pytest.mark.parametrize(
    "vec_a,vec_b,expected",
    [
        ([1.0, 0.0], [1.0, 0.0], 1.0),
        ([1.0, 0.0], [0.0, 1.0], 0.0),
        ([0.0, 0.0], [1.0, 0.0], 0.0),  # zero norm → defined as 0
    ],
)
def test_cosine_similarity_edge_cases(
    vec_a: list[float], vec_b: list[float], expected: float
) -> None:
    # Act
    score = SemanticMatchResolver._cosine_similarity(vec_a, vec_b)

    # Assert
    assert score == pytest.approx(expected, abs=1e-9)
