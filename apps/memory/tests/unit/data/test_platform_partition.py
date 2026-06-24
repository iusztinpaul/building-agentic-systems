"""Unit tests for ``_partition_sources_by_platform`` — the data Orchestrator's shard map.

Pure decision logic (no DB, no Prefect): given the typed configured ``SourceEntry``
list, it returns the FULL list of shards the orchestrator dispatches — one HOMOGENEOUS
shard per non-HuggingFace **Platform** bucket present (substack / youtube / custom), plus
one single-entry shard per HuggingFace offset-**Window** (``num_workers`` windows per HF
entry). The orchestrator then ``model_dump()``s each shard and hands it to the unchanged
``_fan_out_data``. These assert platform bucketing, HF window expansion, order-stability,
and the homogeneous-shard invariant.
"""

from __future__ import annotations

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
from tree.data.offline_pipeline import _partition_sources_by_platform


def _platform_of(entry: SourceEntry) -> str:
    return {
        "substack_rss": "substack",
        "substack_article": "substack",
        "youtube_rss": "youtube",
        "youtube_video": "youtube",
        "web": "custom",
        "huggingface_dataset": "huggingface",
    }[entry.type]


def test_empty_sources_yields_no_shards() -> None:
    assert _partition_sources_by_platform([]) == []


def test_one_homogeneous_shard_per_non_hf_platform() -> None:
    """Substack variants → one substack shard; YouTube → one; web → one custom."""

    sources: list[SourceEntry] = [
        SubstackRssSource(uri="https://a.example/feed"),
        SubstackRssSource(uri="https://b.example/feed"),
        SubstackArticleSource(uri="https://c.example/p/post"),
        YouTubeRssSource(uri="https://youtube.com/feeds/videos.xml?channel_id=x"),
        YouTubeVideoSource(uri="https://youtu.be/abc"),
        WebSource(uri="https://d.example/page"),
        WebSource(uri="https://e.example/page"),
    ]

    shards = _partition_sources_by_platform(sources)

    # 3 platform buckets present ⇒ 3 shards.
    assert len(shards) == 3

    # Every shard is homogeneous to ONE platform.
    for shard in shards:
        platforms = {_platform_of(e) for e in shard}
        assert len(platforms) == 1

    by_platform = {_platform_of(shard[0]): shard for shard in shards}
    assert {e.uri for e in by_platform["substack"]} == {
        "https://a.example/feed",
        "https://b.example/feed",
        "https://c.example/p/post",
    }
    assert len(by_platform["substack"]) == 3
    assert len(by_platform["youtube"]) == 2
    assert len(by_platform["custom"]) == 2


def test_substack_variants_land_in_the_same_shard() -> None:
    sources: list[SourceEntry] = [
        SubstackRssSource(uri="https://a.example/feed"),
        SubstackArticleSource(uri="https://b.example/p/post"),
    ]

    shards = _partition_sources_by_platform(sources)

    assert len(shards) == 1
    assert {e.type for e in shards[0]} == {"substack_rss", "substack_article"}


def test_youtube_variants_land_in_the_same_shard() -> None:
    sources: list[SourceEntry] = [
        YouTubeRssSource(uri="https://youtube.com/feeds/videos.xml?channel_id=x"),
        YouTubeVideoSource(uri="https://youtu.be/abc"),
    ]

    shards = _partition_sources_by_platform(sources)

    assert len(shards) == 1
    assert {e.type for e in shards[0]} == {"youtube_rss", "youtube_video"}


def test_non_hf_union_preserves_configured_order() -> None:
    """The in-order union of non-HF shards equals the configured non-HF sources."""

    sources: list[SourceEntry] = [
        SubstackRssSource(uri="https://a.example/feed"),
        YouTubeVideoSource(uri="https://youtu.be/abc"),
        WebSource(uri="https://d.example/page"),
        SubstackArticleSource(uri="https://b.example/p/post"),
        YouTubeRssSource(uri="https://youtube.com/feeds/videos.xml?channel_id=x"),
        WebSource(uri="https://e.example/page"),
    ]

    shards = _partition_sources_by_platform(sources)

    flat = [e for shard in shards for e in shard]
    # Same multiset of entries; per-platform internal order preserved.
    assert sorted(e.uri for e in flat) == sorted(e.uri for e in sources)
    substack = next(s for s in shards if _platform_of(s[0]) == "substack")
    assert [e.uri for e in substack] == [
        "https://a.example/feed",
        "https://b.example/p/post",
    ]


def test_hf_entry_fans_into_num_workers_single_entry_shards() -> None:
    """A HF entry with num_workers=N ⇒ N single-entry shards, one per window."""

    sources: list[SourceEntry] = [
        HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=4),
    ]

    shards = _partition_sources_by_platform(sources)

    assert len(shards) == 4
    assert all(len(shard) == 1 for shard in shards)
    windows = [(shard[0].offset, shard[0].max_samples) for shard in shards]
    assert windows == [(0, 250), (250, 250), (500, 250), (750, 250)]


def test_hf_single_worker_yields_one_full_window() -> None:
    sources: list[SourceEntry] = [
        HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=1),
    ]

    shards = _partition_sources_by_platform(sources)

    assert len(shards) == 1
    assert len(shards[0]) == 1
    assert shards[0][0].offset is None
    assert shards[0][0].max_samples == 1000


def test_hf_max_samples_zero_emits_no_shard() -> None:
    sources: list[SourceEntry] = [
        HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, max_samples=0, num_workers=4),
    ]

    assert _partition_sources_by_platform(sources) == []


def test_multiple_hf_entries_fan_out_independently() -> None:
    sources: list[SourceEntry] = [
        HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, max_samples=100, num_workers=2),
        HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, max_samples=30, num_workers=3),
    ]

    shards = _partition_sources_by_platform(sources)

    # 2 windows + 3 windows = 5 single-entry shards.
    assert len(shards) == 5
    assert all(len(shard) == 1 for shard in shards)
    windows = [(shard[0].offset, shard[0].max_samples) for shard in shards]
    assert windows == [(0, 50), (50, 50), (0, 10), (10, 10), (20, 10)]


def test_mixed_config_dispatches_platform_buckets_plus_hf_windows() -> None:
    """Non-HF platform buckets come first, then each HF entry's windows."""

    sources: list[SourceEntry] = [
        SubstackRssSource(uri="https://a.example/feed"),
        SubstackArticleSource(uri="https://b.example/p/post"),
        YouTubeRssSource(uri="https://youtube.com/feeds/videos.xml?channel_id=x"),
        YouTubeVideoSource(uri="https://youtu.be/abc"),
        WebSource(uri="https://d.example/page"),
        HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=4),
    ]

    shards = _partition_sources_by_platform(sources)

    # substack + youtube + custom = 3 non-HF shards; + 4 HF windows = 7.
    assert len(shards) == 7

    # Every shard is homogeneous to one platform.
    for shard in shards:
        platforms = {_platform_of(e) for e in shard}
        assert len(platforms) == 1

    platforms = [_platform_of(shard[0]) for shard in shards]
    # Non-HF platform buckets dispatched first, then HF windows.
    assert platforms[:3] == ["substack", "youtube", "custom"]
    assert platforms[3:] == ["huggingface"] * 4


def test_partition_is_deterministic_and_order_stable() -> None:
    sources: list[SourceEntry] = [
        WebSource(uri="https://d.example/page"),
        SubstackRssSource(uri="https://a.example/feed"),
        HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, max_samples=100, num_workers=2),
    ]

    first = _partition_sources_by_platform(sources)
    second = _partition_sources_by_platform(sources)

    assert [[e.model_dump() for e in s] for s in first] == [
        [e.model_dump() for e in s] for s in second
    ]
