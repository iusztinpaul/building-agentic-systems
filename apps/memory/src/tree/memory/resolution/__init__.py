"""Resolver chain for the unified memory.

Public surface — see :mod:`tree.memory.resolution.types` for the result
types, :mod:`tree.memory.resolution.base` for the protocol/ABC, and the
concrete modules (``alias``, ``exact``, ``fuzzy``) for individual resolvers.
The semantic resolver and composite chain land in #009.
"""

from tree.memory.resolution.alias import AliasMatchResolver
from tree.memory.resolution.base import AbstractResolver, BaseResolver
from tree.memory.resolution.exact import ExactMatchResolver
from tree.memory.resolution.fuzzy import FuzzyMatchResolver
from tree.memory.resolution.types import (
    ResolutionMatch,
    ResolvedEntity,
    _normalize,
)

__all__ = [
    "AbstractResolver",
    "AliasMatchResolver",
    "BaseResolver",
    "ExactMatchResolver",
    "FuzzyMatchResolver",
    "ResolutionMatch",
    "ResolvedEntity",
    "_normalize",
]
