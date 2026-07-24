"""Unit tests for the document-shard fan-out (#067, ADR-002 §3 amended #066).

#067 split extraction into an coordinator flow
(``memory-extract-etl-coordinator``) that dispatches a DISTINCT worker deployment
(``memory-extract-etl-worker``) per shard — there is NO recursion. These exercise
the PURE logic with no Prefect server and ``run_deployment`` mocked:

* ``_partition_into_shards`` — contiguous, disjoint, balanced shard partitioning;
* ``_resolve_num_shards`` — the non-positive→1 clamp;
* ``_fan_out_extraction`` — the gather + failure-isolation + single-index core. Each
  child dispatch targets the WORKER deployment and carries only
  ``{user_id, document_ids}`` (NO ``num_shards`` key — the worker has no such
  param), driven through a fake ``run_deployment`` so we never touch a real
  deployment.

Since #095 every fake returns a real ``FlowRun`` — the shape ``run_deployment``
actually returns — because the fan-out now counts a shard as succeeded only when
that run's terminal state is COMPLETED.

The coordinator flow ``memory_extract_etl_coordinator(...)`` and pending-doc
resolution against Mongo are covered by the integration test
(``tests/integration/memory/test_extraction_fanout.py``).
"""

from __future__ import annotations

import pytest
from beanie import PydanticObjectId
from prefect.client.schemas.objects import StateType

from tests.prefect_doubles import completed_flow_run, flow_run_in_state
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

    assert _resolve_num_shards(bad) == 1


@pytest.mark.parametrize("good", [1, 2, 4, 7])
def test_resolve_num_shards_positive_is_unchanged(good) -> None:
    assert _resolve_num_shards(good) == good


@pytest.mark.parametrize("bad", [0, -3])
def test_clamped_nonpositive_shards_into_one_shard_with_all_ids(bad) -> None:
    """End-to-end: a 0 / negative direct-trigger value runs everything in ONE
    shard (NOT a no-op) — the resolved count feeds ``_partition_into_shards``."""

    ids = [str(PydanticObjectId()) for _ in range(6)]

    effective = _resolve_num_shards(bad)
    shards = _partition_into_shards(ids, effective)

    # One shard, containing every id in order — not a zero-shard no-op.
    assert len(shards) == 1
    assert shards[0] == ids


# ---------------------------------------------------------------------------
# Fan-out core (run_deployment mocked)
# ---------------------------------------------------------------------------


async def test_fan_out_issues_one_run_per_shard_then_single_index(mocker) -> None:
    """Each shard fans out one worker run; exactly one index run follows."""

    user_id = PydanticObjectId()
    ids = [str(PydanticObjectId()) for _ in range(6)]
    shards = _partition_into_shards(ids, 4)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        return completed_flow_run()

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    stats = await _fan_out_extraction(
        user_id=user_id, shards=shards, run_deployment=runner
    )

    worker_calls = [c for c in calls if "worker" in c[0]]
    indexing_calls = [c for c in calls if "indexing" in c[0]]

    # One worker call per shard; exactly one indexing call.
    assert len(worker_calls) == len(shards)
    assert len(indexing_calls) == 1

    # The single index run is the LAST call (fired after the gather).
    assert "indexing" in calls[-1][0]

    # Report accounting.
    assert stats.shards_total == len(shards)
    assert stats.succeeded == len(shards)
    assert stats.failed == 0
    assert stats.failures == {}


async def test_fan_out_dispatches_the_worker_deployment(mocker) -> None:
    """Every shard dispatch targets the WORKER deployment (no recursion/self).

    The dispatched name contains ``worker`` and NOT ``coordinator`` — the #067
    split replaced #061's recursive self-dispatch with a distinct worker deployment.
    """

    user_id = PydanticObjectId()
    shards = _partition_into_shards([str(PydanticObjectId()) for _ in range(6)], 4)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        return completed_flow_run()

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_extraction(user_id=user_id, shards=shards, run_deployment=runner)

    # Every non-index dispatch targets the worker; none target an coordinator.
    dispatch_names = [name for name, _p in calls if "indexing" not in name]
    assert len(dispatch_names) == len(shards)
    assert all("worker" in name for name in dispatch_names)
    assert all("coordinator" not in name for name in dispatch_names)


async def test_fan_out_extraction_passes_user_and_shard_ids(mocker) -> None:
    """Each worker run carries str(user_id) + its shard's document_ids."""

    user_id = PydanticObjectId()
    ids = [str(PydanticObjectId()) for _ in range(6)]
    shards = _partition_into_shards(ids, 4)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        return completed_flow_run()

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_extraction(user_id=user_id, shards=shards, run_deployment=runner)

    worker_params = [p for name, p in calls if "worker" in name]
    assert all(p["user_id"] == str(user_id) for p in worker_params)
    # The union of every shard's document_ids equals the input ids.
    flat = [doc_id for p in worker_params for doc_id in p["document_ids"]]
    assert flat == ids


async def test_fan_out_children_carry_no_num_shards_key(mocker) -> None:
    """Every worker dispatch carries ONLY ``{user_id, document_ids}``.

    The worker has no ``num_shards`` param — passing one would be a Prefect
    parameter error. There is no recursion (a distinct worker deployment), so the
    ``num_shards=1`` child key from #061 is gone.
    """

    user_id = PydanticObjectId()
    ids = [str(PydanticObjectId()) for _ in range(6)]
    shards = _partition_into_shards(ids, 4)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        return completed_flow_run()

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_extraction(user_id=user_id, shards=shards, run_deployment=runner)

    worker_params = [p for name, p in calls if "worker" in name]
    assert len(worker_params) == len(shards)
    assert all("num_shards" not in p for p in worker_params)
    assert all(set(p) == {"user_id", "document_ids"} for p in worker_params)


async def test_fan_out_indexing_carries_user_id_only(mocker) -> None:
    """The single indexing run is scoped to the user, with NO document_ids."""

    user_id = PydanticObjectId()
    shards = _partition_into_shards([str(PydanticObjectId()) for _ in range(4)], 4)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        return completed_flow_run()

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
    # The 2nd worker shard blows up.
    failing_shard_ids = tuple(shards[1])

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        params = parameters or {}
        if "worker" in name and tuple(params.get("document_ids", [])) == (
            failing_shard_ids
        ):
            raise RuntimeError("shard blew up")
        return completed_flow_run()

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    stats = await _fan_out_extraction(
        user_id=user_id, shards=shards, run_deployment=runner
    )

    worker_calls = [c for c in calls if "worker" in c[0]]
    indexing_calls = [c for c in calls if "indexing" in c[0]]

    # Every shard was attempted; the failure is isolated and recorded.
    assert len(worker_calls) == len(shards)
    assert stats.shards_total == len(shards)
    assert stats.succeeded == len(shards) - 1
    assert stats.failed == 1
    assert len(stats.failures) == 1
    assert "shard blew up" in next(iter(stats.failures.values()))

    # The single indexing run still fires AFTER the gather.
    assert len(indexing_calls) == 1
    assert "indexing" in calls[-1][0]


class TestNonCompletedWorkerRuns:
    """A worker that hard-FAILS is counted as failed, never as succeeded (#095).

    The data coordinator's twin defect: ``run_deployment`` RETURNS the finished
    flow run for a Failed / Crashed / Cancelled worker instead of raising, so
    counting "it returned" as success let the fan-out summary read green while
    extraction output was missing.
    """

    @pytest.mark.parametrize(
        "state_type",
        [StateType.FAILED, StateType.CRASHED, StateType.CANCELLED],
        ids=["Failed", "Crashed", "Cancelled"],
    )
    async def test_terminal_non_completed_state_counts_as_failed(
        self, mocker, state_type: StateType
    ) -> None:
        user_id = PydanticObjectId()
        shards = _partition_into_shards([str(PydanticObjectId())], 1)

        async def _fake_run_deployment(name, parameters=None, **kwargs):
            if "worker" in name:
                return flow_run_in_state(state_type, "worker exploded")
            return completed_flow_run()

        runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

        stats = await _fan_out_extraction(
            user_id=user_id, shards=shards, run_deployment=runner
        )

        assert stats.shards_total == 1
        assert stats.succeeded == 0
        assert stats.failed == 1
        assert "worker exploded" in stats.failures["0"]

    async def test_index_run_still_fires_after_a_failed_worker_state(
        self, mocker
    ) -> None:
        """A state-failed shard is a PARTIAL extraction — still indexed, as before."""

        user_id = PydanticObjectId()
        ids = [str(PydanticObjectId()) for _ in range(2)]
        shards = _partition_into_shards(ids, 2)
        calls: list[str] = []

        async def _fake_run_deployment(name, parameters=None, **kwargs):
            calls.append(name)
            params = parameters or {}
            if params.get("document_ids") == [ids[0]]:
                return flow_run_in_state(StateType.FAILED)
            return completed_flow_run()

        runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

        stats = await _fan_out_extraction(
            user_id=user_id, shards=shards, run_deployment=runner
        )

        assert stats.succeeded == 1
        assert stats.failed == 1
        assert len([name for name in calls if "indexing" in name]) == 1
        assert "indexing" in calls[-1]


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


async def test_fan_out_all_worker_runs_precede_the_index_run(mocker) -> None:
    """Call ORDER: every worker run is issued before the indexing run."""

    user_id = PydanticObjectId()
    shards = _partition_into_shards([str(PydanticObjectId()) for _ in range(5)], 4)

    order: list[str] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        order.append("index" if "indexing" in name else "worker")
        return completed_flow_run()

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_extraction(user_id=user_id, shards=shards, run_deployment=runner)

    # All worker runs come first, exactly one index last.
    assert order.count("index") == 1
    assert order[-1] == "index"
    assert order[:-1] == ["worker"] * len(shards)


# ---------------------------------------------------------------------------
# Distributed-trace header forwarding (observability monitoring fix)
# ---------------------------------------------------------------------------


async def test_fan_out_forwards_trace_headers_to_workers_and_index(mocker) -> None:
    """When the coordinator owns a trace, its headers are forwarded to every
    worker AND the trailing indexing run, so the whole run is ONE Opik trace."""

    user_id = PydanticObjectId()
    shards = _partition_into_shards([str(PydanticObjectId()) for _ in range(6)], 3)
    headers = {"opik_trace_id": "t1", "opik_parent_span_id": "s1"}

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        return completed_flow_run()

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_extraction(
        user_id=user_id,
        shards=shards,
        run_deployment=runner,
        opik_trace_headers=headers,
    )

    worker_params = [p for name, p in calls if "worker" in name]
    assert len(worker_params) == len(shards)
    assert all(p["opik_trace_headers"] == headers for p in worker_params)
    assert all(
        set(p) == {"user_id", "document_ids", "opik_trace_headers"}
        for p in worker_params
    )

    index_name, index_params = calls[-1]
    assert "indexing" in index_name
    assert index_params["opik_trace_headers"] == headers
    assert index_params["user_id"] == str(user_id)


async def test_fan_out_omits_headers_when_none(mocker) -> None:
    """No active trace (Opik off) ⇒ no ``opik_trace_headers`` key on any child,
    so the worker/indexing flow-run parameter validation stays exactly as before."""

    user_id = PydanticObjectId()
    shards = _partition_into_shards([str(PydanticObjectId()) for _ in range(4)], 2)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        return completed_flow_run()

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_extraction(
        user_id=user_id,
        shards=shards,
        run_deployment=runner,
        opik_trace_headers=None,
    )

    assert all("opik_trace_headers" not in p for _name, p in calls)
