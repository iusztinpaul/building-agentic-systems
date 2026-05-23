"""Neutral, pipeline-agnostic shard-partitioning helpers (ADR-002 §3).

These two PURE functions are the single home for the balanced-contiguous
partitioning math shared by BOTH orchestrators:

* the memory-extraction orchestrator, which shards a ``list[str]`` of pending
  document ids (``tree.memory.extraction``), and
* the data orchestrator (#068), which shards the configured ``sources:`` list.

They depend only on ``len()`` and slicing, so they are generic over the element
type (``list[T] -> list[list[T]]``). Living at the ``tree`` top level — not under
``memory/`` or ``data/`` — lets both orchestrators import the IDENTICAL math with
zero copy-paste and no cross-module (memory↔data) dependency.

There is NO Prefect ``@flow`` and NO deployment here — these are pure decision
helpers, unit-tested directly.
"""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def _resolve_num_shards(num_shards: int) -> int:
    """Resolve the effective shard count, clamped to ``>= 1``.

    The shard count is always an explicit per-run choice
    (``NUM_SHARDS`` / ``--num-shards`` / ``-p num_shards``). A non-positive value
    — 0 or negative, only reachable via a DIRECT Prefect API/UI trigger that
    bypasses the guarded ``--num-shards`` script path — clamps to ``1`` so the
    run shards everything into a single shard instead of becoming a silent
    zero-shard no-op (``_partition_into_shards``'s ``min(num_shards, N)`` would
    otherwise go non-positive on a truthy negative).
    """

    return max(1, num_shards)


def _partition_into_shards(items: list[T], num_shards: int) -> list[list[T]]:
    """Split ``items`` into exactly ``min(num_shards, N)`` shards.

    Generic over the element type ``T`` (relies only on ``len()`` and slicing),
    so it shards a ``list[str]`` of document ids (memory) or any other element
    list (e.g. the data orchestrator's source list) with identical math.

    The shards are contiguous (preserve input order), disjoint, and balanced:
    sizes differ by at most one, with the larger (``ceil(N / shards)``-sized)
    shards leading. The in-order union reconstructs the input exactly.

    * ``N == 0`` ⇒ ``[]`` (no shards, no-op upstream).
    * ``N < num_shards`` ⇒ ``N`` singleton shards (we never emit empty shards).

    Example: ``N=6, num_shards=4`` ⇒ sizes ``2, 2, 1, 1`` (4 shards), NOT
    ``2, 2, 2`` (a fixed ``ceil``-chunk would under-shard).
    """

    n = len(items)
    if n == 0:
        return []

    effective = min(num_shards, n)
    # Balanced contiguous split into exactly ``effective`` shards: the first
    # ``remainder`` shards get the larger ``ceil`` size, the rest the floor.
    base, remainder = divmod(n, effective)

    shards: list[list[T]] = []
    start = 0
    for i in range(effective):
        size = base + (1 if i < remainder else 0)
        shards.append(items[start : start + size])
        start += size
    return shards
