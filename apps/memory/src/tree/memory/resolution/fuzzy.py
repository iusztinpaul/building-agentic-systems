"""Fuzzy-match resolver backed by ``rapidfuzz``.

The dependency is loaded lazily so the resolver constructs in environments
where ``rapidfuzz`` is absent (e.g. a slim CI image). ``is_available``
reports whether scoring is possible; when ``False``, :meth:`resolve` returns
``match_type="none"`` so the upstream composite chain can skip fuzzy
gracefully.

Scoring uses ``token_sort_ratio`` by default (configurable via
``scorer_name``). Scores are divided by 100 to live in ``[0, 1]``; the
highest score above ``threshold`` wins (not the first).
"""

from collections.abc import Iterable, Mapping

from tree.entities.knowledge_graph import NodeType

from tree.memory.resolution.base import AbstractResolver
from tree.memory.resolution.types import ResolvedEntity


class FuzzyMatchResolver(AbstractResolver):
    """Approximate-match resolver. Highest score above ``threshold`` wins."""

    def __init__(
        self,
        *,
        threshold: float = 0.85,
        scorer_name: str = "token_sort_ratio",
    ) -> None:
        self._threshold = threshold
        self._scorer_name = scorer_name

        try:
            from rapidfuzz import fuzz

            self._fuzz = fuzz
            self._scorer = getattr(fuzz, scorer_name)
            self._is_available = True
        except ImportError:
            self._fuzz = None
            self._scorer = None
            self._is_available = False

    @property
    def is_available(self) -> bool:
        """Whether ``rapidfuzz`` was importable at construction time."""

        return self._is_available

    def resolve(
        self,
        name: str,
        entity_type: NodeType,
        candidate_names: Iterable[str],
        existing_aliases: Mapping[str, list[str]] | None = None,
    ) -> ResolvedEntity:
        if not self._is_available or self._scorer is None:
            return self._no_match(name, entity_type)

        normalized_input = self._normalize(name)
        best_candidate: str | None = None
        best_score = 0.0
        for candidate in candidate_names:
            score = self._scorer(normalized_input, self._normalize(candidate)) / 100.0
            if score > best_score and score >= self._threshold:
                best_score = score
                best_candidate = candidate

        if best_candidate is None:
            return self._no_match(name, entity_type)

        return ResolvedEntity(
            original_name=name,
            canonical_name=best_candidate,
            entity_type=entity_type,
            confidence=best_score,
            match_type="fuzzy",
        )
