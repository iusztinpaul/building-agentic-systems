"""Unit tests for the ``data-etl-orchestrator`` flow (#072, ADR-002 §3 amend #070–#074).

These drive ``data_etl_orchestrator`` directly with ``tree.data.pipeline.app_config``
patched to a known ``sources:`` list and ``tree.data.pipeline.run_deployment`` mocked,
so nothing touches a real Prefect server. They assert the orchestrator now partitions by
PLATFORM (not by count):

* ONE homogeneous ``data-etl-worker`` dispatch per non-HuggingFace Platform bucket
  present (substack / youtube / custom), each carrying all that Platform's entries;
* ``num_workers`` dispatches per ``HuggingFaceDatasetSource``, one per disjoint
  offset-Window (the last window takes the remainder so windows tile ``[0, max_samples)``);
* ``num_workers=1`` HF ⇒ one dispatch with the full ``max_samples`` and ``offset`` unset;
* the ``num_shards`` parameter is GONE (passing it raises ``TypeError``);
* serializes each shard as ``list[dict]`` so it round-trips through ``run_deployment``;
* fires NO trailing/index run (the data pipeline only produces ``documents``);
* treats an empty configured sources list as a clean no-op (``shards_total=0``);
* isolates one shard's failure (recorded in ``failures``) while the others proceed.

The pure partition math is covered in ``test_platform_partition.py`` /
``test_arxiv_window_entries.py``; the pure fan-out core in ``test_fanout_data.py``.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest
from beanie import PydanticObjectId

from tree.config.app_config import (
    HuggingFaceDatasetSource,
    SourceEntry,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
    YouTubeRssSource,
    YouTubeVideoSource,
)
from tree.data.huggingface.arxiv_dataset_pipeline import ARXIV_DATASET_ID
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


def _types_of(params: dict) -> set[str]:
    return {s["type"] for s in params["sources"]}


def _non_hf_sources() -> list[SourceEntry]:
    """3 Substack feeds, 2 Substack articles, 1 YouTube RSS, 1 YouTube video, 2 web."""

    return [
        SubstackRssSource(uri="https://a.example/feed"),
        SubstackRssSource(uri="https://b.example/feed"),
        SubstackRssSource(uri="https://c.example/feed"),
        SubstackArticleSource(uri="https://d.example/p/post"),
        SubstackArticleSource(uri="https://e.example/p/post"),
        YouTubeRssSource(uri="https://youtube.com/feeds/videos.xml?channel_id=x"),
        YouTubeVideoSource(uri="https://youtu.be/abc"),
        WebSource(uri="https://f.example/page"),
        WebSource(uri="https://g.example/page"),
    ]


# --- num_shards is gone -----------------------------------------------------


def test_orchestrator_signature_has_no_num_shards() -> None:
    """The ``num_shards`` parameter is removed from the orchestrator signature."""

    params = inspect.signature(data_etl_orchestrator).parameters
    assert "num_shards" not in params
    assert list(params) == ["user_id"]


async def test_passing_num_shards_raises_type_error(mocker) -> None:
    """Calling with the dropped ``num_shards`` knob raises ``TypeError``."""

    _patch_config(mocker, _non_hf_sources())
    _capture_run_deployment(mocker)

    with pytest.raises(TypeError):
        await data_etl_orchestrator(_USER_ID, num_shards=2)  # type: ignore[call-arg]


# --- group-by-platform (non-HF) ---------------------------------------------


async def test_one_homogeneous_worker_per_non_hf_platform(mocker) -> None:
    """3 Platform buckets present ⇒ exactly 3 homogeneous worker dispatches."""

    _patch_config(mocker, _non_hf_sources())
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_orchestrator(_USER_ID)

    assert len(calls) == 3
    assert all("data-etl-worker" in name for name, _p in calls)
    assert stats.shards_total == 3
    assert stats.succeeded == 3
    assert stats.failed == 0

    # Every shard is homogeneous to one Platform.
    platform_of = {
        "substack_rss": "substack",
        "substack_article": "substack",
        "youtube_rss": "youtube",
        "youtube_video": "youtube",
        "web": "custom",
    }
    for _name, params in calls:
        platforms = {platform_of[s["type"]] for s in params["sources"]}
        assert len(platforms) == 1


async def test_substack_and_youtube_variants_share_one_shard(mocker) -> None:
    """RSS + article land in the same substack shard; RSS + video in one youtube."""

    _patch_config(mocker, _non_hf_sources())
    calls = _capture_run_deployment(mocker)

    await data_etl_orchestrator(_USER_ID)

    shards_by_type = [_types_of(p) for _name, p in calls]
    assert {"substack_rss", "substack_article"} in shards_by_type
    assert {"youtube_rss", "youtube_video"} in shards_by_type
    assert {"web"} in shards_by_type


async def test_non_hf_shard_union_reconstructs_configured_sources(mocker) -> None:
    """The in-order union of all non-HF shards equals the configured non-HF sources."""

    sources = _non_hf_sources()
    _patch_config(mocker, sources)
    calls = _capture_run_deployment(mocker)

    await data_etl_orchestrator(_USER_ID)

    flat_uris = sorted(s["uri"] for _name, p in calls for s in p["sources"])
    assert flat_uris == sorted(s.uri for s in sources)


# --- HuggingFace offset-window sub-fan-out ----------------------------------


async def test_hf_fans_out_into_disjoint_offset_windows(mocker) -> None:
    """num_workers=4 over max_samples=1000 ⇒ 4 windows tiling [0, 1000)."""

    _patch_config(
        mocker,
        [
            HuggingFaceDatasetSource(
                uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=4
            )
        ],
    )
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_orchestrator(_USER_ID)

    assert len(calls) == 4
    assert stats.shards_total == 4
    windows = [
        (p["sources"][0]["offset"], p["sources"][0]["max_samples"])
        for _name, p in calls
    ]
    assert windows == [(0, 250), (250, 250), (500, 250), (750, 250)]
    # Each HF shard is a single windowed entry.
    assert all(len(p["sources"]) == 1 for _name, p in calls)
    assert all(p["sources"][0]["type"] == "huggingface_dataset" for _name, p in calls)


async def test_hf_remainder_goes_to_the_last_window(mocker) -> None:
    """num_workers=3 over max_samples=1000 ⇒ (0,333),(333,333),(666,334)."""

    _patch_config(
        mocker,
        [
            HuggingFaceDatasetSource(
                uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=3
            )
        ],
    )
    calls = _capture_run_deployment(mocker)

    await data_etl_orchestrator(_USER_ID)

    windows = [
        (p["sources"][0]["offset"], p["sources"][0]["max_samples"])
        for _name, p in calls
    ]
    assert windows == [(0, 333), (333, 333), (666, 334)]
    # The union tiles [0, 1000) with no gap/overlap.
    covered: list[int] = []
    for offset, size in windows:
        covered.extend(range(offset, offset + size))
    assert covered == list(range(1000))


async def test_hf_single_worker_is_byte_identical_to_today(mocker) -> None:
    """num_workers=1 ⇒ ONE dispatch, full max_samples, offset unset/None."""

    _patch_config(
        mocker,
        [
            HuggingFaceDatasetSource(
                uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=1
            )
        ],
    )
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_orchestrator(_USER_ID)

    assert len(calls) == 1
    _name, params = calls[0]
    assert len(params["sources"]) == 1
    assert params["sources"][0]["offset"] is None
    assert params["sources"][0]["max_samples"] == 1000
    assert stats.shards_total == 1


async def test_hf_window_shard_roundtrips_offset_through_run_deployment(mocker) -> None:
    """A windowed HF shard serializes with its offset/max_samples (round-trip ready)."""

    _patch_config(
        mocker,
        [
            HuggingFaceDatasetSource(
                uri=ARXIV_DATASET_ID, max_samples=500, num_workers=2
            )
        ],
    )
    calls = _capture_run_deployment(mocker)

    await data_etl_orchestrator(_USER_ID)

    # Second window carries offset=250, max_samples=250 as JSON-safe dict fields.
    second = calls[1][1]["sources"][0]
    assert isinstance(second, dict)
    assert second["type"] == "huggingface_dataset"
    assert second["offset"] == 250
    assert second["max_samples"] == 250


async def test_multiple_hf_entries_fan_out_independently(mocker) -> None:
    _patch_config(
        mocker,
        [
            HuggingFaceDatasetSource(
                uri=ARXIV_DATASET_ID, max_samples=100, num_workers=2
            ),
            HuggingFaceDatasetSource(
                uri=ARXIV_DATASET_ID, max_samples=30, num_workers=3
            ),
        ],
    )
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_orchestrator(_USER_ID)

    # 2 windows + 3 windows = 5 dispatches.
    assert len(calls) == 5
    assert stats.shards_total == 5


# --- mixed config -----------------------------------------------------------


async def test_mixed_config_dispatches_platform_buckets_plus_hf_windows(mocker) -> None:
    """substack + youtube + web + HF(num_workers=4) ⇒ 3 + 4 = 7 dispatches."""

    sources: list[SourceEntry] = [
        SubstackRssSource(uri="https://a.example/feed"),
        SubstackArticleSource(uri="https://b.example/p/post"),
        YouTubeVideoSource(uri="https://youtu.be/abc"),
        WebSource(uri="https://c.example/page"),
        HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=4),
    ]
    _patch_config(mocker, sources)
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_orchestrator(_USER_ID)

    assert len(calls) == 7
    assert stats.shards_total == 7
    hf_shards = [p for _name, p in calls if _types_of(p) == {"huggingface_dataset"}]
    assert len(hf_shards) == 4


async def test_each_worker_carries_user_id_and_serialized_sources(mocker) -> None:
    _patch_config(mocker, _non_hf_sources())
    calls = _capture_run_deployment(mocker)

    await data_etl_orchestrator(_USER_ID)

    for _name, params in calls:
        assert params["user_id"] == str(_USER_ID)
        # Sources are plain JSON-safe dicts carrying the ``type`` discriminator.
        assert all(isinstance(s, dict) and "type" in s for s in params["sources"])
        # ``user_id`` + ``sources`` always present; ``opik_trace_headers`` only when
        # Opik is active (a real OPIK_API_KEY), and must be a JSON-safe dict then.
        assert {"user_id", "sources"} <= set(params)
        assert set(params) <= {"user_id", "sources", "opik_trace_headers"}
        if "opik_trace_headers" in params:
            assert isinstance(params["opik_trace_headers"], dict)


# --- inherited _fan_out_data behaviors --------------------------------------


async def test_fires_no_trailing_index_run(mocker) -> None:
    sources = _non_hf_sources() + [
        HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=2)
    ]
    _patch_config(mocker, sources)
    calls = _capture_run_deployment(mocker)

    await data_etl_orchestrator(_USER_ID)

    dispatched = [name for name, _p in calls]
    assert dispatched, "expected at least one dispatch"
    assert all("data-etl-worker" in name for name in dispatched)
    assert all("indexing" not in name for name in dispatched)
    assert all("orchestrator" not in name for name in dispatched)


async def test_empty_sources_is_a_clean_noop(mocker) -> None:
    _patch_config(mocker, [])
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_orchestrator(_USER_ID)

    assert calls == []
    assert stats.shards_total == 0
    assert stats.succeeded == 0
    assert stats.failed == 0


async def test_one_shard_failure_is_isolated(mocker) -> None:
    sources = _non_hf_sources() + [
        HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=2)
    ]
    _patch_config(mocker, sources)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        params = parameters or {}
        # Fail whichever shard contains the YouTube entries.
        if any(s.get("type") == "youtube_video" for s in params.get("sources", [])):
            raise RuntimeError("bright data fetch error")

    mocker.patch(
        "tree.data.pipeline.run_deployment",
        new=mocker.AsyncMock(side_effect=_fake_run_deployment),
    )

    stats = await data_etl_orchestrator(_USER_ID)

    # Every shard was attempted; one failed and is recorded; the rest succeeded.
    assert len(calls) == stats.shards_total
    assert stats.failed == 1
    assert stats.succeeded == stats.shards_total - 1
    assert len(stats.failures) == 1
    assert "bright data fetch error" in next(iter(stats.failures.values()))
    # No index run despite the partial failure.
    assert all("indexing" not in name for name, _p in calls)
