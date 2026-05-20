"""Semantic resolver backed by an :class:`~tree.models.base.BaseEmbeddingModel`.

Computes cosine similarity between the input name's embedding and each
candidate's embedding; returns the highest-similarity candidate above
``threshold``. Embeddings are cached per-instance in a bounded LRU keyed on
the normalized name string, so repeated lookups for hot names stay cheap
without unbounded memory growth across long-running pipelines.

See ``RESOLUTION_MODULE.md`` §6 and ``RESOLUTION_DEDUP_ALGORITHM.md`` §3 for
the chain placement (alias → exact → fuzzy → semantic).
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Iterable, Mapping

from tree.entities.knowledge_graph import NodeType
from tree.memory.embedding_text import embed_in_batches
from tree.memory.resolution.base import AbstractResolver
from tree.memory.resolution.types import ResolvedEntity
from tree.models.base import BaseEmbeddingModel


class SemanticMatchResolver(AbstractResolver):
    """Resolve via cosine similarity over learned embeddings.

    The cache is per-instance and bounded by ``cache_max_size``; eviction is
    least-recently-used (oldest-by-access). The cache key is the normalized
    form of the name, so ``"Alice"``, ``"  alice "``, and ``"ALICE"`` all
    share one slot.
    """

    def __init__(
        self,
        embedding_model: BaseEmbeddingModel,
        *,
        threshold: float = 0.80,
        cache_max_size: int = 10_000,
    ) -> None:
        self._embedding_model = embedding_model
        self._threshold = threshold
        self._cache_max_size = cache_max_size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()

    def clear_cache(self) -> None:
        """Empty the embedding cache. Subsequent lookups recompute."""

        self._cache.clear()

    async def prewarm_cache(self, names: Iterable[str]) -> None:
        """Batch-embed every uncached ``name`` in ONE request, then populate
        the LRU (#044).

        Pre-#044 the semantic resolver embedded the input name and each
        candidate name one-at-a-time inside :meth:`_embed_cached` — for a
        type with ``C`` candidates resolved against ``E`` entities that is up
        to ``C + E`` separate Voyage requests, the resolution-stage analogue
        of the indexing 429 hotspot. This pre-warm packs all the
        not-yet-cached names into as few synchronous requests as the Voyage
        per-request caps allow (via
        :func:`tree.memory.embedding_text.embed_in_batches`) and seeds the
        cache, so the subsequent cosine loop is pure cache hits.

        The LRU and its eviction semantics are preserved: each warmed name is
        inserted through the same normalized-key path as :meth:`_embed_cached`
        and the cache is trimmed to ``cache_max_size`` (LRU/oldest-first)
        afterward. Already-cached names are skipped (no re-embed) and their
        recency is left untouched. Names that collapse to the same normalized
        key are embedded once.
        """

        # Collect the surface forms whose normalized key is not already
        # cached, de-duplicated by normalized key, preserving first-seen order
        # so the embed request order is deterministic for tests.
        to_embed: list[str] = []
        seen_keys: set[str] = set()
        for name in names:
            key = self._normalize(name)
            if key in self._cache or key in seen_keys:
                continue
            seen_keys.add(key)
            to_embed.append(name)

        if not to_embed:
            return

        vectors = await embed_in_batches(to_embed, self._embedding_model)
        for name, embedding in zip(to_embed, vectors, strict=True):
            self._cache[self._normalize(name)] = embedding

        # Apply the same LRU bound as _embed_cached.
        while len(self._cache) > self._cache_max_size:
            self._cache.popitem(last=False)

    async def _embed_cached(self, name: str) -> list[float]:
        """Return the embedding for ``name``, using/updating the LRU cache."""

        key = self._normalize(name)
        if key in self._cache:
            # LRU hit: move to most-recently-used end.
            self._cache.move_to_end(key)
            return self._cache[key]

        vectors = await self._embedding_model.embed([name])
        embedding = vectors[0]
        self._cache[key] = embedding
        # Evict the least-recently-used entry if we are over budget.
        while len(self._cache) > self._cache_max_size:
            self._cache.popitem(last=False)
        return embedding

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Return cosine similarity clamped to ``[0.0, 1.0]``.

        Small negative values (~ -1e-9) are common with floating-point math
        even when vectors are mathematically non-negative — clamping keeps
        the resolver from returning sub-zero confidences.
        """

        dot = 0.0
        norm_a = 0.0
        norm_b = 0.0
        for x, y in zip(a, b, strict=True):
            dot += x * y
            norm_a += x * x
            norm_b += y * y
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        score = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
        # Defensive clamp: floating-point can produce -1e-9 or 1 + 1e-9.
        if score < 0.0:
            return 0.0
        if score > 1.0:
            return 1.0
        return score

    async def resolve(  # type: ignore[override]
        self,
        name: str,
        entity_type: NodeType,
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> ResolvedEntity:
        candidates = list(candidate_names)
        if not candidates:
            return self._no_match(name, entity_type)

        name_embedding = await self._embed_cached(name)

        best_candidate: str | None = None
        best_score = 0.0
        for candidate in candidates:
            candidate_embedding = await self._embed_cached(candidate)
            score = self._cosine_similarity(name_embedding, candidate_embedding)
            if score >= self._threshold and score > best_score:
                best_score = score
                best_candidate = candidate

        if best_candidate is None:
            return self._no_match(name, entity_type)

        return ResolvedEntity(
            original_name=name,
            canonical_name=best_candidate,
            entity_type=entity_type,
            confidence=best_score,
            match_type="semantic",
        )

    async def resolve_batch(  # type: ignore[override]
        self,
        entities: Iterable[tuple[str, NodeType]],
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> list[ResolvedEntity]:
        """Resolve each ``(name, type)`` sequentially.

        Sequential rather than gather()'d so the per-instance LRU cache is
        warmed deterministically — important for the cache-eviction tests.
        """

        candidates = list(candidate_names)
        results: list[ResolvedEntity] = []
        for name, entity_type in entities:
            results.append(
                await self.resolve(name, entity_type, candidates, existing_aliases)
            )
        return results
