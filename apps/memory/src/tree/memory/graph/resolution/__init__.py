"""Resolver chain for the unified memory.

Public surface — see :mod:`tree.memory.graph.resolution.types` for the result
types, :mod:`tree.memory.graph.resolution.base` for the protocol/ABC, and the
concrete modules (``alias``, ``exact``, ``fuzzy``, ``semantic``) for
individual resolvers. The :class:`CompositeResolver` wires them into the
canonical Alias → Exact → Fuzzy → Semantic chain.
"""

from tree.memory.graph.resolution.alias import AliasMatchResolver
from tree.memory.graph.resolution.base import AbstractResolver, BaseResolver
from tree.memory.graph.resolution.composite import CompositeResolver
from tree.memory.graph.resolution.exact import ExactMatchResolver
from tree.memory.graph.resolution.fuzzy import FuzzyMatchResolver
from tree.memory.graph.resolution.semantic import SemanticMatchResolver
from tree.memory.graph.resolution.types import (
    ResolutionMatch,
    ResolvedEntity,
    _normalize,
)

__all__ = [
    "AbstractResolver",
    "AliasMatchResolver",
    "BaseResolver",
    "CompositeResolver",
    "ExactMatchResolver",
    "FuzzyMatchResolver",
    "ResolutionMatch",
    "ResolvedEntity",
    "SemanticMatchResolver",
    "_normalize",
]
