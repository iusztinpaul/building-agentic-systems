"""Unit tests for the neutral, pipeline-agnostic shard-partitioning helpers.

#066 relocated the pure partitioning math (``_partition_into_shards`` /
``_resolve_num_shards``) into :mod:`tree.sharding` so BOTH the memory-extraction
coordinator and the data coordinator (#068) import the IDENTICAL helpers without
copy-paste (ADR-002 §3 Amendment #066).

The memory document-shard partitioning (``list[str]``) is exhaustively covered by
``tests/unit/memory/extraction/test_fanout.py`` (which imports the SAME functions
through the ``tree.memory.extraction.sharding`` re-export). These tests pin the
NEW reuse contract directly against ``tree.sharding``:

* the partitioning math is GENERIC over the element type (the data coordinator
  shards a list of arbitrary source items, not just ``str`` doc ids), and
* the canonical import home is ``tree.sharding`` — the path #068 will import from.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tree.sharding import _partition_into_shards, _resolve_num_shards


@dataclass(frozen=True)
class _FakeSource:
    """Stand-in for the data coordinator's source items (#068).

    Proves ``_partition_into_shards`` is generic over ``T`` and never touches the
    element's contents — it shards arbitrary objects, not just ``str`` doc ids.
    """

    uri: str


# ---------------------------------------------------------------------------
# Generic-element partitioning (the #068 data-coordinator reuse path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n,num_shards,expected_sizes",
    [
        (8, 4, [2, 2, 2, 2]),
        (6, 4, [2, 2, 1, 1]),
        (7, 3, [3, 2, 2]),
        (3, 4, [1, 1, 1]),
        (1, 4, [1]),
        (5, 1, [5]),
    ],
)
def test_partition_is_generic_over_element_type(n, num_shards, expected_sizes) -> None:
    """Same balanced-contiguous math for non-``str`` (source) elements."""

    sources = [_FakeSource(uri=f"https://example.com/{i}") for i in range(n)]

    shards = _partition_into_shards(sources, num_shards)

    assert [len(s) for s in shards] == expected_sizes
    # In-order union reconstructs the input exactly — contiguous, ordered, disjoint.
    flat = [src for shard in shards for src in shard]
    assert flat == sources


def test_partition_empty_returns_no_shards() -> None:
    assert _partition_into_shards([], 4) == []


def test_partition_preserves_object_identity() -> None:
    """Sharding never copies/mutates elements — the data coordinator gets the
    SAME source objects back, just regrouped."""

    sources = [_FakeSource(uri=f"s{i}") for i in range(5)]

    shards = _partition_into_shards(sources, 2)

    flat = [src for shard in shards for src in shard]
    for original, sharded in zip(sources, flat, strict=True):
        assert sharded is original


# ---------------------------------------------------------------------------
# Effective-shard-count resolution / clamp (type-agnostic int -> int)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -3])
def test_resolve_num_shards_clamps_nonpositive_to_one(bad) -> None:
    assert _resolve_num_shards(bad) == 1


@pytest.mark.parametrize("good", [1, 2, 4, 9])
def test_resolve_num_shards_positive_is_unchanged(good) -> None:
    assert _resolve_num_shards(good) == good
