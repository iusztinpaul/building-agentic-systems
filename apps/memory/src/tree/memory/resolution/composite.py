"""Composite resolver chain.

Chains the per-strategy resolvers in fixed order:

    Alias → Exact → Fuzzy → Semantic

Short-circuits on the first non-``"none"`` result. The fuzzy stage is dropped
from the active chain (with a one-shot INFO log) when ``rapidfuzz`` is not
installed; the semantic stage is skipped entirely if no embedding model is
supplied to the constructor.

The composite enforces ``type_strict`` at this layer: callers pass an
``existing_entities`` mapping keyed by :class:`NodeType`, and a PERSON named
"Alice" can never match against a TASK named "Alice" — even if the leaf
resolvers would otherwise score them as identical.

See ``RESOLUTION_MODULE.md`` §6–§7 for the chain semantics and §7.4 for the
``find_matches`` review-tooling API that this module stubs out.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from tree.entities.knowledge_graph import NodeType
from tree.memory.resolution.alias import AliasMatchResolver
from tree.memory.resolution.exact import ExactMatchResolver
from tree.memory.resolution.fuzzy import FuzzyMatchResolver
from tree.memory.resolution.semantic import SemanticMatchResolver
from tree.memory.resolution.types import ResolutionMatch, ResolvedEntity
from tree.models.base import BaseEmbeddingModel

logger = logging.getLogger(__name__)


class CompositeResolver:
    """Run the four resolvers in chain order and return the first hit.

    Construction:
        - Always builds Alias, Exact, and Fuzzy resolvers.
        - Builds the Semantic resolver only when ``embedding_model`` is set.
        - Drops Fuzzy from the active chain (and emits exactly one INFO log)
          when ``rapidfuzz`` is unavailable at construction time.
    """

    def __init__(
        self,
        embedding_model: BaseEmbeddingModel | None = None,
        *,
        fuzzy_threshold: float = 0.85,
        semantic_threshold: float = 0.80,
        type_strict: bool = True,
        embedding_cache_max_size: int = 10_000,
    ) -> None:
        self._type_strict = type_strict
        self._alias = AliasMatchResolver()
        self._exact = ExactMatchResolver()

        fuzzy = FuzzyMatchResolver(threshold=fuzzy_threshold)
        if fuzzy.is_available:
            self._fuzzy: FuzzyMatchResolver | None = fuzzy
        else:
            self._fuzzy = None
            logger.info(
                "rapidfuzz not installed; CompositeResolver will skip the fuzzy stage"
            )

        if embedding_model is not None:
            self._semantic: SemanticMatchResolver | None = SemanticMatchResolver(
                embedding_model,
                threshold=semantic_threshold,
                cache_max_size=embedding_cache_max_size,
            )
        else:
            self._semantic = None

    async def resolve(
        self,
        name: str,
        entity_type: NodeType,
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> ResolvedEntity:
        """Run the chain in order; return the first non-``"none"`` result.

        Materializes ``candidate_names`` once because each resolver iterates
        it independently.
        """

        candidates = list(candidate_names)

        # Alias.
        result = self._alias.resolve(name, entity_type, candidates, existing_aliases)
        if result.match_type != "none":
            return result

        # Exact.
        result = self._exact.resolve(name, entity_type, candidates, existing_aliases)
        if result.match_type != "none":
            return result

        # Fuzzy (may be disabled).
        if self._fuzzy is not None:
            result = self._fuzzy.resolve(
                name, entity_type, candidates, existing_aliases
            )
            if result.match_type != "none":
                return result

        # Semantic (may be disabled).
        if self._semantic is not None:
            result = await self._semantic.resolve(
                name, entity_type, candidates, existing_aliases
            )
            if result.match_type != "none":
                return result

        return ResolvedEntity(
            original_name=name,
            canonical_name=name,
            entity_type=entity_type,
            confidence=0.0,
            match_type="none",
        )

    async def resolve_batch(
        self,
        entities: Iterable[tuple[str, NodeType]],
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> list[ResolvedEntity]:
        """Resolve each ``(name, type)`` against a shared candidate pool."""

        candidates = list(candidate_names)
        results: list[ResolvedEntity] = []
        for name, entity_type in entities:
            results.append(
                await self.resolve(name, entity_type, candidates, existing_aliases)
            )
        return results

    async def resolve_with_types(
        self,
        entities: Iterable[tuple[str, NodeType]],
        existing_entities: Mapping[NodeType, list[str]],
        existing_aliases: Mapping[str, list[str]],
    ) -> list[ResolvedEntity]:
        """Preferred entry point for the pipeline.

        ``existing_entities`` is pre-grouped by type. With
        ``type_strict=True`` (the default), the resolver only ever sees the
        candidates for the entity's own type — so a PERSON "Alice" can't
        bleed across into a TASK "Alice".
        """

        entity_list = list(entities)

        # #044: pre-warm the semantic resolver's embedding cache with ONE
        # batched request covering every input name AND every candidate name,
        # instead of letting ``SemanticMatchResolver._embed_cached`` issue a
        # separate Voyage request per name during the cosine loop. Only the
        # semantic stage uses embeddings, so this is a no-op when the semantic
        # resolver is disabled (no embedding model supplied). The alias / exact
        # / fuzzy stages short-circuit ahead of semantic, so most names are
        # never actually compared — but pre-warming the full set keeps the
        # request count to ``ceil(total_names / cap)`` regardless of which
        # stage wins, which is the rate-limit win the operator asked for.
        if self._semantic is not None:
            prewarm_names: list[str] = [name for name, _ in entity_list]
            for bucket in existing_entities.values():
                prewarm_names.extend(bucket)
            await self._semantic.prewarm_cache(prewarm_names)

        results: list[ResolvedEntity] = []
        for name, entity_type in entity_list:
            if self._type_strict:
                type_candidates = list(existing_entities.get(entity_type, []))
            else:
                # Non-strict: union over all types. Preserves order/dedup.
                seen: set[str] = set()
                type_candidates = []
                for bucket in existing_entities.values():
                    for candidate in bucket:
                        if candidate not in seen:
                            seen.add(candidate)
                            type_candidates.append(candidate)
            results.append(
                await self.resolve(
                    name,
                    entity_type,
                    type_candidates,
                    existing_aliases,
                )
            )
        return results

    def find_matches(
        self,
        name: str,
        entity_type: NodeType,
        top_k: int = 5,
    ) -> list[ResolutionMatch]:
        """Reserved for future human-review tooling.

        Intentionally unimplemented: the review surface in #014 hasn't been
        designed yet, and we don't want to commit to semantics that may need
        to change. The signature is fixed so #014 can drop in an
        implementation without API churn.
        """

        raise NotImplementedError(
            "Reserved for future review tooling — see RESOLUTION_MODULE.md §7.4"
        )
