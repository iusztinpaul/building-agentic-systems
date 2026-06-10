"""Unit tests for the ``data-etl-orchestrator`` flow (#068, ADR-002 §3 amended #066).

These drive ``data_etl_orchestrator`` directly with ``tree.data.pipeline.app_config``
patched to a known ``sources:`` list and ``tree.data.pipeline.run_deployment`` mocked,
so nothing touches a real Prefect server. They assert the orchestrator:

* reads the configured sources and partitions them into ``min(num_shards, N)`` balanced
  shards (one ``data-etl-worker`` dispatch per shard);
* serializes each shard as ``list[dict]`` so it round-trips through ``run_deployment``;
* reconstructs the full configured source list as the in-order union of the shards;
* fires NO trailing/index run (the data pipeline only produces ``documents``);
* treats an empty configured sources list as a clean no-op (``shards_total=0``);
* isolates one shard's failure (recorded in ``failures``) while the others proceed.

The per-source-type worker dispatch is covered in ``test_pipeline.py``; the pure
fan-out core in ``test_fanout_data.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from beanie import PydanticObjectId

from tree.config.app_config import (
    HuggingFaceDatasetSource,
    SourceEntry,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
    YouTubeVideoSource,
)
from tree.data.pipeline import data_etl_orchestrator

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


def _patch_config(mocker, sources: list[SourceEntry]) -> None:
    mock_config = MagicMock()
    mock_config.sources.sources = sources
    mocker.patch("tree.data.pipeline.app_config", mock_config)


def _capture_run_deployment(mocker) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    mocker.patch(
        "tree.data.pipeline.run_deployment",
        new=mocker.AsyncMock(side_effect=_fake_run_deployment),
    )
    return calls


def _sample_sources() -> list[SourceEntry]:
    return [
        SubstackRssSource(uri="https://a.example/feed"),
        SubstackArticleSource(uri="https://b.example/p/post"),
        YouTubeVideoSource(uri="https://youtu.be/abc"),
        WebSource(uri="https://c.example/page"),
        HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot"),
        WebSource(uri="https://d.example/page"),
    ]


async def test_dispatches_one_worker_per_shard(mocker) -> None:
    sources = _sample_sources()
    _patch_config(mocker, sources)
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_orchestrator(_USER_ID, num_shards=2)

    # 6 sources, num_shards=2 → 2 balanced shards → 2 worker dispatches.
    assert len(calls) == 2
    assert all("data-etl-worker" in name for name, _p in calls)
    assert stats.shards_total == 2
    assert stats.succeeded == 2
    assert stats.failed == 0


async def test_shards_are_balanced_and_union_reconstructs_sources(mocker) -> None:
    sources = _sample_sources()
    _patch_config(mocker, sources)
    calls = _capture_run_deployment(mocker)

    await data_etl_orchestrator(_USER_ID, num_shards=4)

    # 6 sources, num_shards=4 → balanced sizes 2,2,1,1.
    shard_sizes = [len(p["sources"]) for _name, p in calls]
    assert shard_sizes == [2, 2, 1, 1]

    # The in-order union of every shard's sources equals the configured list
    # (serialized to dicts).
    flat = [src for _name, p in calls for src in p["sources"]]
    assert flat == [s.model_dump() for s in sources]


async def test_each_worker_carries_user_id_and_serialized_sources(mocker) -> None:
    sources = _sample_sources()
    _patch_config(mocker, sources)
    calls = _capture_run_deployment(mocker)

    await data_etl_orchestrator(_USER_ID, num_shards=2)

    for _name, params in calls:
        assert params["user_id"] == str(_USER_ID)
        # Sources are plain JSON-safe dicts (carry the ``type`` discriminator).
        assert all(isinstance(s, dict) and "type" in s for s in params["sources"])
        # ``user_id`` + ``sources`` are always present. ``opik_trace_headers`` is
        # forwarded only when Opik is active (a real OPIK_API_KEY in the env);
        # it must be a JSON-safe dict when present so it round-trips as a
        # Prefect flow-run parameter across the worker process hop.
        assert {"user_id", "sources"} <= set(params)
        assert set(params) <= {"user_id", "sources", "opik_trace_headers"}
        if "opik_trace_headers" in params:
            assert isinstance(params["opik_trace_headers"], dict)


async def test_default_num_shards_dispatches_single_worker_with_all_sources(
    mocker,
) -> None:
    sources = _sample_sources()
    _patch_config(mocker, sources)
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_orchestrator(_USER_ID)  # num_shards defaults to 1

    assert len(calls) == 1
    _name, params = calls[0]
    assert params["sources"] == [s.model_dump() for s in sources]
    assert stats.shards_total == 1


async def test_fires_no_trailing_index_run(mocker) -> None:
    sources = _sample_sources()
    _patch_config(mocker, sources)
    calls = _capture_run_deployment(mocker)

    await data_etl_orchestrator(_USER_ID, num_shards=3)

    dispatched = [name for name, _p in calls]
    assert all("data-etl-worker" in name for name in dispatched)
    assert all("indexing" not in name for name in dispatched)
    assert all("orchestrator" not in name for name in dispatched)


async def test_empty_sources_is_a_clean_noop(mocker) -> None:
    _patch_config(mocker, [])
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_orchestrator(_USER_ID, num_shards=4)

    assert calls == []
    assert stats.shards_total == 0
    assert stats.succeeded == 0
    assert stats.failed == 0


async def test_one_shard_failure_is_isolated(mocker) -> None:
    sources = _sample_sources()
    _patch_config(mocker, sources)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        params = parameters or {}
        # Fail whichever shard contains the YouTube video entry.
        if any(s.get("type") == "youtube_video" for s in params.get("sources", [])):
            raise RuntimeError("bright data fetch error")

    mocker.patch(
        "tree.data.pipeline.run_deployment",
        new=mocker.AsyncMock(side_effect=_fake_run_deployment),
    )

    stats = await data_etl_orchestrator(_USER_ID, num_shards=3)

    # Every shard was attempted; one failed and is recorded; the rest succeeded.
    assert len(calls) == stats.shards_total
    assert stats.failed == 1
    assert stats.succeeded == stats.shards_total - 1
    assert len(stats.failures) == 1
    assert "bright data fetch error" in next(iter(stats.failures.values()))
    # No index run despite the partial failure.
    assert all("indexing" not in name for name, _p in calls)
