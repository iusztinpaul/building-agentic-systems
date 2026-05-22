"""Unit tests for the in-flow document-shard fan-out (#061, ADR-002 §3 amended).

#061 folded the #056 standalone fan-out parent flow into the single
``memory_extraction`` flow via a ``num_shards`` parameter using
recursive self-dispatch. These exercise the PURE logic with no Prefect server and
``run_deployment`` mocked:

* ``_partition_into_shards`` — contiguous, disjoint, balanced shard partitioning;
* ``_resolve_num_shards`` — the non-positive→1 clamp;
* ``_fan_out_extraction`` — the gather + failure-isolation + single-index core,
  with each child carrying ``num_shards=1`` (recursion terminates after one
  level), driven through a fake ``run_deployment`` so we never touch a real
  deployment.

The orchestrator-vs-worker BRANCH inside ``memory_extraction(num_shards=…)`` and
pending-doc resolution against Mongo are covered by the integration test
(``tests/integration/memory/test_extraction_fanout.py``).
"""

from __future__ import annotations

import pytest
from beanie import PydanticObjectId

from tree.memory.extraction.sharding import (
    FanOutStats,
    _fan_out_extraction,
    _partition_into_shards,
    _resolve_num_shards,
)


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n,num_shards,expected_sizes",
    [
        # Even split.
        (8, 4, [2, 2, 2, 2]),
        # Remainder distributed onto the leading shards; sizes differ by ≤1
        # and we still emit exactly min(num_shards, N) shards.
        (6, 4, [2, 2, 1, 1]),
        (7, 3, [3, 2, 2]),
        # N < num_shards collapses to N singleton shards.
        (3, 4, [1, 1, 1]),
        (1, 4, [1]),
        # Single shard.
        (5, 1, [5]),
    ],
)
def test_partition_sizes(n, num_shards, expected_sizes) -> None:
    ids = [str(PydanticObjectId()) for _ in range(n)]

    shards = _partition_into_shards(ids, num_shards)

    assert [len(s) for s in shards] == expected_sizes


@pytest.mark.parametrize(
    "n,num_shards",
    [(8, 4), (6, 4), (7, 3), (3, 4), (1, 4), (5, 1), (10, 4)],
)
def test_partition_union_equals_input_and_is_contiguous(n, num_shards) -> None:
    ids = [str(PydanticObjectId()) for _ in range(n)]

    shards = _partition_into_shards(ids, num_shards)

    # Union (in order) reconstructs the input exactly — contiguous + ordered.
    flat = [doc_id for shard in shards for doc_id in shard]
    assert flat == ids


@pytest.mark.parametrize(
    "n,num_shards",
    [(8, 4), (6, 4), (7, 3), (3, 4), (10, 4)],
)
def test_partition_shards_are_disjoint(n, num_shards) -> None:
    ids = [str(PydanticObjectId()) for _ in range(n)]

    shards = _partition_into_shards(ids, num_shards)

    seen: set[str] = set()
    for shard in shards:
        for doc_id in shard:
            assert doc_id not in seen
            seen.add(doc_id)
    assert len(seen) == n


def test_partition_empty_returns_no_shards() -> None:
    assert _partition_into_shards([], 4) == []


def test_partition_collapses_to_n_shards_when_fewer_ids() -> None:
    ids = [str(PydanticObjectId()) for _ in range(3)]

    shards = _partition_into_shards(ids, 4)

    assert len(shards) == 3
    assert all(len(s) == 1 for s in shards)


def test_explicit_six_ids_four_shards_sizes_2_2_1_1() -> None:
    ids = [str(PydanticObjectId()) for _ in range(6)]

    shards = _partition_into_shards(ids, 4)

    assert [len(s) for s in shards] == [2, 2, 1, 1]
    assert [doc_id for shard in shards for doc_id in shard] == ids


# ---------------------------------------------------------------------------
# Effective-shard-count resolution / clamp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -3])
def test_resolve_num_shards_clamps_nonpositive_to_one(bad) -> None:
    """A DIRECT-trigger 0 / negative ``num_shards`` clamps to a single shard.

    Guards the silent zero-shard no-op: ``_partition_into_shards``'s
    ``min(num_shards, N)`` would go non-positive on a truthy negative.
    """

    assert _resolve_num_shards(bad, default=4) == 1


def test_resolve_num_shards_none_falls_back_to_default() -> None:
    assert _resolve_num_shards(None, default=4) == 4


@pytest.mark.parametrize("good", [1, 2, 7])
def test_resolve_num_shards_positive_is_unchanged(good) -> None:
    assert _resolve_num_shards(good, default=4) == good


@pytest.mark.parametrize("bad", [0, -3])
def test_clamped_nonpositive_shards_into_one_shard_with_all_ids(bad) -> None:
    """End-to-end: a 0 / negative direct-trigger value runs everything in ONE
    shard (NOT a no-op) — the resolved count feeds ``_partition_into_shards``."""

    ids = [str(PydanticObjectId()) for _ in range(6)]

    effective = _resolve_num_shards(bad, default=4)
    shards = _partition_into_shards(ids, effective)

    # One shard, containing every id in order — not a zero-shard no-op.
    assert len(shards) == 1
    assert shards[0] == ids


# ---------------------------------------------------------------------------
# Fan-out core (run_deployment mocked)
# ---------------------------------------------------------------------------


async def test_fan_out_issues_one_run_per_shard_then_single_index(mocker) -> None:
    """Each shard fans out one extraction run; exactly one index run follows."""

    user_id = PydanticObjectId()
    ids = [str(PydanticObjectId()) for _ in range(6)]
    shards = _partition_into_shards(ids, 4)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    stats = await _fan_out_extraction(
        user_id=user_id, shards=shards, run_deployment=runner
    )

    extraction_calls = [c for c in calls if "extraction" in c[0]]
    indexing_calls = [c for c in calls if "indexing" in c[0]]

    # One extraction call per shard; exactly one indexing call.
    assert len(extraction_calls) == len(shards)
    assert len(indexing_calls) == 1

    # The single index run is the LAST call (fired after the gather).
    assert "indexing" in calls[-1][0]

    # Report accounting.
    assert stats.shards_total == len(shards)
    assert stats.succeeded == len(shards)
    assert stats.failed == 0
    assert stats.failures == {}


async def test_fan_out_extraction_passes_user_and_shard_ids(mocker) -> None:
    """Each extraction run carries str(user_id) + its shard's document_ids."""

    user_id = PydanticObjectId()
    ids = [str(PydanticObjectId()) for _ in range(6)]
    shards = _partition_into_shards(ids, 4)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_extraction(user_id=user_id, shards=shards, run_deployment=runner)

    extraction_params = [p for name, p in calls if "extraction" in name]
    assert all(p["user_id"] == str(user_id) for p in extraction_params)
    # The union of every shard's document_ids equals the input ids.
    flat = [doc_id for p in extraction_params for doc_id in p["document_ids"]]
    assert flat == ids


async def test_fan_out_children_carry_num_shards_one(mocker) -> None:
    """Every extraction self-dispatch carries ``num_shards == 1``.

    This is what makes children take the WORKER path — recursion terminates
    after exactly one level (no infinite self-dispatch).
    """

    user_id = PydanticObjectId()
    ids = [str(PydanticObjectId()) for _ in range(6)]
    shards = _partition_into_shards(ids, 4)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_extraction(user_id=user_id, shards=shards, run_deployment=runner)

    extraction_params = [p for name, p in calls if "extraction" in name]
    assert len(extraction_params) == len(shards)
    assert all(p["num_shards"] == 1 for p in extraction_params)


async def test_fan_out_indexing_carries_user_id_only(mocker) -> None:
    """The single indexing run is scoped to the user, with NO document_ids."""

    user_id = PydanticObjectId()
    shards = _partition_into_shards([str(PydanticObjectId()) for _ in range(4)], 4)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_extraction(user_id=user_id, shards=shards, run_deployment=runner)

    name, params = calls[-1]
    assert "indexing" in name
    assert params == {"user_id": str(user_id)}


async def test_fan_out_isolates_one_shard_failure(mocker) -> None:
    """One shard raising does not abort the batch; the index run STILL fires."""

    user_id = PydanticObjectId()
    ids = [str(PydanticObjectId()) for _ in range(8)]
    shards = _partition_into_shards(ids, 4)

    calls: list[tuple[str, dict]] = []
    # The 2nd extraction shard blows up.
    failing_shard_ids = tuple(shards[1])

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        params = parameters or {}
        if "extraction" in name and tuple(params.get("document_ids", [])) == (
            failing_shard_ids
        ):
            raise RuntimeError("shard blew up")

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    stats = await _fan_out_extraction(
        user_id=user_id, shards=shards, run_deployment=runner
    )

    extraction_calls = [c for c in calls if "extraction" in c[0]]
    indexing_calls = [c for c in calls if "indexing" in c[0]]

    # Every shard was attempted; the failure is isolated and recorded.
    assert len(extraction_calls) == len(shards)
    assert stats.shards_total == len(shards)
    assert stats.succeeded == len(shards) - 1
    assert stats.failed == 1
    assert len(stats.failures) == 1
    assert "shard blew up" in next(iter(stats.failures.values()))

    # The single indexing run still fires AFTER the gather.
    assert len(indexing_calls) == 1
    assert "indexing" in calls[-1][0]


async def test_fan_out_no_shards_is_noop(mocker) -> None:
    """Zero shards ⇒ no run_deployment calls at all and a zero report."""

    runner = mocker.AsyncMock()

    stats = await _fan_out_extraction(
        user_id=PydanticObjectId(), shards=[], run_deployment=runner
    )

    runner.assert_not_called()
    assert stats == FanOutStats(shards_total=0)
    assert stats.succeeded == 0
    assert stats.failed == 0


async def test_fan_out_all_extraction_runs_precede_the_index_run(mocker) -> None:
    """Call ORDER: every extraction run is issued before the indexing run."""

    user_id = PydanticObjectId()
    shards = _partition_into_shards([str(PydanticObjectId()) for _ in range(5)], 4)

    order: list[str] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        order.append("index" if "indexing" in name else "extract")

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_extraction(user_id=user_id, shards=shards, run_deployment=runner)

    # All extracts come first, exactly one index last.
    assert order.count("index") == 1
    assert order[-1] == "index"
    assert order[:-1] == ["extract"] * len(shards)
