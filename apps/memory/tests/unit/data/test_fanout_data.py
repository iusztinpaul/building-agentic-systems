"""Unit tests for the data source-shard fan-out core (#068, ADR-002 §3 amended #066).

#068 split data ingestion into an orchestrator flow (``data-etl-orchestrator``) that
dispatches a DISTINCT worker deployment (``data-etl-worker``) per shard — there is NO
recursion and NO trailing index. These exercise the PURE fan-out core with no Prefect
server and ``run_deployment`` mocked:

* ``_partition_into_shards`` / ``_resolve_num_shards`` are re-exported from the neutral
  ``tree.sharding`` module (#066) and exhaustively covered by ``tests/unit/test_sharding.py``;
* ``_fan_out_data`` — the gather + failure-isolation core. Each child dispatch targets
  the WORKER deployment and carries ``{user_id, sources}``. Critically, the data path
  fires NO trailing/index run (the data pipeline only produces ``documents``).

The orchestrator flow ``data_etl_orchestrator(...)`` is covered in
``test_orchestrator_data.py`` (config reading + partition + no-op) and by integration
tests.
"""

from __future__ import annotations

from beanie import PydanticObjectId

from tree.data.sharding import (
    DataFanOutStats,
    _fan_out_data,
    _partition_into_shards,
)


def _shard(uri: str) -> dict:
    return {"type": "web", "uri": uri}


async def test_fan_out_issues_one_run_per_shard(mocker) -> None:
    """Each shard fans out exactly one worker run — and nothing else."""

    user_id = PydanticObjectId()
    serialized = [_shard(f"https://e{i}.example/post") for i in range(6)]
    shards = _partition_into_shards(serialized, 4)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    stats = await _fan_out_data(user_id=user_id, shards=shards, run_deployment=runner)

    # One worker call per shard; NO other calls.
    assert len(calls) == len(shards)
    assert all("worker" in name for name, _p in calls)

    assert stats.shards_total == len(shards)
    assert stats.succeeded == len(shards)
    assert stats.failed == 0
    assert stats.failures == {}


async def test_fan_out_fires_no_trailing_index_run(mocker) -> None:
    """The data fan-out NEVER issues an indexing/trailing run (AC).

    The data pipeline only produces ``documents``; there is no index. Assert that
    zero dispatched deployment names mention ``indexing`` and the call count equals
    exactly the shard count.
    """

    user_id = PydanticObjectId()
    shards = _partition_into_shards(
        [_shard(f"https://e{i}.example") for i in range(5)], 4
    )

    calls: list[str] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append(name)

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_data(user_id=user_id, shards=shards, run_deployment=runner)

    assert len(calls) == len(shards)
    assert all("indexing" not in name for name in calls)
    assert all("index" not in name for name in calls)


async def test_fan_out_dispatches_the_worker_deployment(mocker) -> None:
    """Every shard dispatch targets the WORKER deployment (no recursion/self)."""

    user_id = PydanticObjectId()
    shards = _partition_into_shards(
        [_shard(f"https://e{i}.example") for i in range(6)], 4
    )

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_data(user_id=user_id, shards=shards, run_deployment=runner)

    dispatch_names = [name for name, _p in calls]
    assert len(dispatch_names) == len(shards)
    assert all("worker" in name for name in dispatch_names)
    assert all("orchestrator" not in name for name in dispatch_names)


async def test_fan_out_passes_user_and_shard_sources(mocker) -> None:
    """Each worker run carries str(user_id) + its shard's sources."""

    user_id = PydanticObjectId()
    serialized = [_shard(f"https://e{i}.example/post") for i in range(6)]
    shards = _partition_into_shards(serialized, 4)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_data(user_id=user_id, shards=shards, run_deployment=runner)

    worker_params = [p for _name, p in calls]
    assert all(p["user_id"] == str(user_id) for p in worker_params)
    # The union (in order) of every shard's sources equals the input.
    flat = [src for p in worker_params for src in p["sources"]]
    assert flat == serialized


async def test_fan_out_children_carry_only_user_and_sources(mocker) -> None:
    """Every worker dispatch carries ONLY ``{user_id, sources}`` — no num_shards."""

    user_id = PydanticObjectId()
    shards = _partition_into_shards(
        [_shard(f"https://e{i}.example") for i in range(6)], 4
    )

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_data(user_id=user_id, shards=shards, run_deployment=runner)

    worker_params = [p for _name, p in calls]
    assert len(worker_params) == len(shards)
    assert all("num_shards" not in p for p in worker_params)
    assert all(set(p) == {"user_id", "sources"} for p in worker_params)


async def test_fan_out_isolates_one_shard_failure(mocker) -> None:
    """One shard raising does not abort the batch; others still run, no index fires."""

    user_id = PydanticObjectId()
    serialized = [_shard(f"https://e{i}.example/post") for i in range(8)]
    shards = _partition_into_shards(serialized, 4)

    calls: list[tuple[str, dict]] = []
    failing_shard = [s["uri"] for s in shards[1]]

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        params = parameters or {}
        if [s["uri"] for s in params.get("sources", [])] == failing_shard:
            raise RuntimeError("shard blew up")

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    stats = await _fan_out_data(user_id=user_id, shards=shards, run_deployment=runner)

    # Every shard was attempted; the failure is isolated and recorded.
    assert len(calls) == len(shards)
    assert stats.shards_total == len(shards)
    assert stats.succeeded == len(shards) - 1
    assert stats.failed == 1
    assert len(stats.failures) == 1
    assert "shard blew up" in next(iter(stats.failures.values()))

    # No index/trailing run despite the partial failure.
    assert all("indexing" not in name for name, _p in calls)


async def test_fan_out_no_shards_is_noop(mocker) -> None:
    """Zero shards ⇒ no run_deployment calls at all and a zero report."""

    runner = mocker.AsyncMock()

    stats = await _fan_out_data(
        user_id=PydanticObjectId(), shards=[], run_deployment=runner
    )

    runner.assert_not_called()
    assert stats == DataFanOutStats(shards_total=0)
    assert stats.succeeded == 0
    assert stats.failed == 0


async def test_fan_out_data_forwards_trace_headers(mocker) -> None:
    """When the data orchestrator owns a trace, its headers are forwarded to
    every worker so the orchestrated run renders as ONE Opik trace."""

    user_id = PydanticObjectId()
    shards = _partition_into_shards(
        [_shard(f"https://e{i}.example") for i in range(4)], 2
    )
    headers = {"opik_trace_id": "t1", "opik_parent_span_id": "s1"}

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_data(
        user_id=user_id,
        shards=shards,
        run_deployment=runner,
        opik_trace_headers=headers,
    )

    assert len(calls) == len(shards)
    assert all(p["opik_trace_headers"] == headers for _name, p in calls)
    assert all(
        set(p) == {"user_id", "sources", "opik_trace_headers"} for _name, p in calls
    )


async def test_fan_out_data_omits_headers_when_none(mocker) -> None:
    """No active trace ⇒ no ``opik_trace_headers`` key on any worker dispatch."""

    user_id = PydanticObjectId()
    shards = _partition_into_shards([_shard("https://e.example")], 1)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    runner = mocker.AsyncMock(side_effect=_fake_run_deployment)

    await _fan_out_data(
        user_id=user_id, shards=shards, run_deployment=runner, opik_trace_headers=None
    )

    assert all("opik_trace_headers" not in p for _name, p in calls)
