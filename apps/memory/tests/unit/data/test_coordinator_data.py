"""Unit tests for the ``data-etl-coordinator`` flow (#072, ADR-002 §3 amend #070–#074;
source selection per ADR-003 / #086).

These drive ``data_etl_coordinator`` directly with the coordinator's source
resolution patched (``default_configured_sources`` / ``load_sources`` in
``tree.data.offline_pipeline``) and ``tree.data.offline_pipeline.run_deployment``
mocked, so nothing touches a real Prefect server. They assert the coordinator
partitions by PLATFORM (not by count) and resolves its source set by CONCATENATION
(``source_files`` ++ inline ``sources``, else the backfill+listen default — there is
NO ``scheduled_only`` filter):

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

import pytest
from beanie import PydanticObjectId

from tests.prefect_doubles import completed_flow_run
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
from tree.data.offline_pipeline import _resolve_source_set, data_etl_coordinator

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


def _patch_default_sources(mocker, sources: list[SourceEntry]) -> None:
    """Patch the coordinator's DEFAULT source set (the no-flags resolution path)."""

    mocker.patch(
        "tree.data.offline_pipeline.default_configured_sources",
        return_value=sources,
    )


def _capture_run_deployment(mocker) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        # #095: the fan-out counts a shard succeeded only on a COMPLETED run, so
        # the double returns what ``run_deployment`` really returns.
        return completed_flow_run()

    mocker.patch(
        "tree.data.offline_pipeline.run_deployment",
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


def test_coordinator_signature_is_user_id_source_files_sources() -> None:
    """The signature is ``(user_id, source_files, sources)`` — no ``num_shards``/``scheduled_only``."""

    params = inspect.signature(data_etl_coordinator).parameters
    assert "num_shards" not in params
    assert "scheduled_only" not in params
    assert list(params) == ["user_id", "source_files", "sources"]


async def test_passing_num_shards_raises_type_error(mocker) -> None:
    """Calling with the dropped ``num_shards`` knob raises ``TypeError``."""

    _patch_default_sources(mocker, _non_hf_sources())
    _capture_run_deployment(mocker)

    with pytest.raises(TypeError):
        await data_etl_coordinator(_USER_ID, num_shards=2)  # type: ignore[call-arg]


# --- group-by-platform (non-HF) ---------------------------------------------


async def test_one_homogeneous_worker_per_non_hf_platform(mocker) -> None:
    """3 Platform buckets present ⇒ exactly 3 homogeneous worker dispatches."""

    _patch_default_sources(mocker, _non_hf_sources())
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(_USER_ID)

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

    _patch_default_sources(mocker, _non_hf_sources())
    calls = _capture_run_deployment(mocker)

    await data_etl_coordinator(_USER_ID)

    shards_by_type = [_types_of(p) for _name, p in calls]
    assert {"substack_rss", "substack_article"} in shards_by_type
    assert {"youtube_rss", "youtube_video"} in shards_by_type
    assert {"web"} in shards_by_type


async def test_non_hf_shard_union_reconstructs_configured_sources(mocker) -> None:
    """The in-order union of all non-HF shards equals the configured non-HF sources."""

    sources = _non_hf_sources()
    _patch_default_sources(mocker, sources)
    calls = _capture_run_deployment(mocker)

    await data_etl_coordinator(_USER_ID)

    flat_uris = sorted(s["uri"] for _name, p in calls for s in p["sources"])
    assert flat_uris == sorted(s.uri for s in sources)


# --- HuggingFace offset-window sub-fan-out ----------------------------------


async def test_hf_fans_out_into_disjoint_offset_windows(mocker) -> None:
    """num_workers=4 over max_samples=1000 ⇒ 4 windows tiling [0, 1000)."""

    _patch_default_sources(
        mocker,
        [
            HuggingFaceDatasetSource(
                uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=4
            )
        ],
    )
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(_USER_ID)

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

    _patch_default_sources(
        mocker,
        [
            HuggingFaceDatasetSource(
                uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=3
            )
        ],
    )
    calls = _capture_run_deployment(mocker)

    await data_etl_coordinator(_USER_ID)

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

    _patch_default_sources(
        mocker,
        [
            HuggingFaceDatasetSource(
                uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=1
            )
        ],
    )
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(_USER_ID)

    assert len(calls) == 1
    _name, params = calls[0]
    assert len(params["sources"]) == 1
    assert params["sources"][0]["offset"] is None
    assert params["sources"][0]["max_samples"] == 1000
    assert stats.shards_total == 1


async def test_hf_window_shard_roundtrips_offset_through_run_deployment(mocker) -> None:
    """A windowed HF shard serializes with its offset/max_samples (round-trip ready)."""

    _patch_default_sources(
        mocker,
        [
            HuggingFaceDatasetSource(
                uri=ARXIV_DATASET_ID, max_samples=500, num_workers=2
            )
        ],
    )
    calls = _capture_run_deployment(mocker)

    await data_etl_coordinator(_USER_ID)

    # Second window carries offset=250, max_samples=250 as JSON-safe dict fields.
    second = calls[1][1]["sources"][0]
    assert isinstance(second, dict)
    assert second["type"] == "huggingface_dataset"
    assert second["offset"] == 250
    assert second["max_samples"] == 250


async def test_multiple_hf_entries_fan_out_independently(mocker) -> None:
    _patch_default_sources(
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

    stats = await data_etl_coordinator(_USER_ID)

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
    _patch_default_sources(mocker, sources)
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(_USER_ID)

    assert len(calls) == 7
    assert stats.shards_total == 7
    hf_shards = [p for _name, p in calls if _types_of(p) == {"huggingface_dataset"}]
    assert len(hf_shards) == 4


async def test_each_worker_carries_user_id_and_serialized_sources(mocker) -> None:
    _patch_default_sources(mocker, _non_hf_sources())
    calls = _capture_run_deployment(mocker)

    await data_etl_coordinator(_USER_ID)

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
    _patch_default_sources(mocker, sources)
    calls = _capture_run_deployment(mocker)

    await data_etl_coordinator(_USER_ID)

    dispatched = [name for name, _p in calls]
    assert dispatched, "expected at least one dispatch"
    assert all("data-etl-worker" in name for name in dispatched)
    assert all("indexing" not in name for name in dispatched)
    assert all("coordinator" not in name for name in dispatched)


async def test_empty_sources_is_a_clean_noop(mocker) -> None:
    _patch_default_sources(mocker, [])
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(_USER_ID)

    assert calls == []
    assert stats.shards_total == 0
    assert stats.succeeded == 0
    assert stats.failed == 0


async def test_one_shard_failure_is_isolated(mocker) -> None:
    sources = _non_hf_sources() + [
        HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, max_samples=1000, num_workers=2)
    ]
    _patch_default_sources(mocker, sources)

    calls: list[tuple[str, dict]] = []

    async def _fake_run_deployment(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))
        params = parameters or {}
        # Fail whichever shard contains the YouTube entries.
        if any(s.get("type") == "youtube_video" for s in params.get("sources", [])):
            raise RuntimeError("bright data fetch error")
        return completed_flow_run()

    mocker.patch(
        "tree.data.offline_pipeline.run_deployment",
        new=mocker.AsyncMock(side_effect=_fake_run_deployment),
    )

    stats = await data_etl_coordinator(_USER_ID)

    # Every shard was attempted; one failed and is recorded; the rest succeeded.
    assert len(calls) == stats.shards_total
    assert stats.failed == 1
    assert stats.succeeded == stats.shards_total - 1
    assert len(stats.failures) == 1
    assert "bright data fetch error" in next(iter(stats.failures.values()))
    # No index run despite the partial failure.
    assert all("indexing" not in name for name, _p in calls)


# --- source-set resolution (file / inline / default, concatenated) ----------


def test_resolve_source_set_defaults_when_both_absent(mocker) -> None:
    """No ``source_files`` and no ``sources`` ⇒ the cached backfill+listen default."""

    default = [SubstackRssSource(uri="https://default.example/feed")]
    mocker.patch(
        "tree.data.offline_pipeline.default_configured_sources", return_value=default
    )
    load = mocker.patch("tree.data.offline_pipeline.load_sources")

    resolved = _resolve_source_set(None, None)

    assert resolved is default
    load.assert_not_called()


def test_resolve_source_set_uses_only_the_listed_files(mocker) -> None:
    """``source_files`` given ⇒ ``load_sources`` over them; the default is never touched."""

    file_entries = [SubstackArticleSource(uri="https://file.example/p/x")]
    load = mocker.patch(
        "tree.data.offline_pipeline.load_sources", return_value=file_entries
    )
    default = mocker.patch("tree.data.offline_pipeline.default_configured_sources")

    resolved = _resolve_source_set(["sources/backfill.yaml"], None)

    load.assert_called_once_with(["sources/backfill.yaml"])
    default.assert_not_called()
    assert resolved == file_entries


def test_resolve_source_set_coerces_inline_dicts(mocker) -> None:
    """Inline ``sources`` dicts are coerced to typed entries via the union adapter."""

    default = mocker.patch("tree.data.offline_pipeline.default_configured_sources")
    load = mocker.patch("tree.data.offline_pipeline.load_sources")

    resolved = _resolve_source_set(
        None, [{"uri": "https://inline.example/feed", "type": "substack_rss"}]
    )

    assert [type(s) for s in resolved] == [SubstackRssSource]
    assert resolved[0].uri == "https://inline.example/feed"
    load.assert_not_called()
    default.assert_not_called()


def test_resolve_source_set_concatenates_files_then_inline(mocker) -> None:
    """BOTH given ⇒ file entries FIRST, then the coerced inline entries — in that order."""

    file_entry = SubstackArticleSource(uri="https://file.example/p/x")
    mocker.patch("tree.data.offline_pipeline.load_sources", return_value=[file_entry])

    resolved = _resolve_source_set(
        ["sources/backfill.yaml"],
        [{"uri": "https://inline.example/page", "type": "web"}],
    )

    assert [type(s) for s in resolved] == [SubstackArticleSource, WebSource]
    assert resolved[0].uri == "https://file.example/p/x"
    assert resolved[1].uri == "https://inline.example/page"


async def test_default_run_dispatches_the_default_source_set(mocker) -> None:
    """The no-flags coordinator path fans out the backfill+listen default."""

    load = mocker.patch("tree.data.offline_pipeline.load_sources")
    _patch_default_sources(mocker, [SubstackRssSource(uri="https://a.example/feed")])
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(_USER_ID)

    load.assert_not_called()
    assert stats.shards_total == 1
    assert _types_of(calls[0][1]) == {"substack_rss"}


async def test_source_files_run_dispatches_the_loaded_files(mocker) -> None:
    """``source_files`` ⇒ the coordinator fans out exactly what ``load_sources`` returns."""

    mocker.patch(
        "tree.data.offline_pipeline.load_sources",
        return_value=[SubstackRssSource(uri="https://listen.example/feed")],
    )
    default = mocker.patch("tree.data.offline_pipeline.default_configured_sources")
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(_USER_ID, source_files=["sources/listen.yaml"])

    default.assert_not_called()
    assert stats.shards_total == 1
    assert _types_of(calls[0][1]) == {"substack_rss"}
    assert calls[0][1]["sources"][0]["uri"] == "https://listen.example/feed"


async def test_inline_sources_run_dispatches_the_coerced_entries(mocker) -> None:
    """Inline ``sources`` dicts ⇒ the coordinator fans out the coerced typed entries."""

    mocker.patch("tree.data.offline_pipeline.load_sources")
    default = mocker.patch("tree.data.offline_pipeline.default_configured_sources")
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(
        _USER_ID, sources=[{"uri": "https://inline.example/page", "type": "web"}]
    )

    default.assert_not_called()
    assert stats.shards_total == 1
    assert _types_of(calls[0][1]) == {"web"}


async def test_both_files_and_inline_are_concatenated_for_dispatch(mocker) -> None:
    """BOTH ⇒ the file's sources PLUS the built inline sources are all dispatched."""

    mocker.patch(
        "tree.data.offline_pipeline.load_sources",
        return_value=[SubstackArticleSource(uri="https://file.example/p/x")],
    )
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(
        _USER_ID,
        source_files=["sources/backfill.yaml"],
        sources=[{"uri": "https://inline.example/page", "type": "web"}],
    )

    # One substack shard (from the file) + one custom shard (from the inline URL).
    assert stats.shards_total == 2
    dispatched = {next(iter(_types_of(p))) for _n, p in calls}
    assert dispatched == {"substack_article", "web"}


async def test_empty_resolved_set_from_explicit_flags_is_a_noop(mocker) -> None:
    """Explicit-but-empty selectors resolve to nothing ⇒ clean no-op, no dispatch."""

    mocker.patch("tree.data.offline_pipeline.load_sources", return_value=[])
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(_USER_ID, source_files=[], sources=[])

    assert calls == []
    assert stats.shards_total == 0


# --- multi-tenant (scheduled / all-users) fan-out ---------------------------


async def test_user_id_none_fans_out_per_active_user(mocker) -> None:
    """``user_id=None`` ingests the sources once per active user (multi-tenant)."""

    user_a = PydanticObjectId("507f1f77bcf86cd799439011")
    user_b = PydanticObjectId("507f1f77bcf86cd799439012")
    mocker.patch(
        "tree.data.offline_pipeline._resolve_target_user_ids",
        new=mocker.AsyncMock(return_value=[user_a, user_b]),
    )
    _patch_default_sources(mocker, [SubstackRssSource(uri="https://a.example/feed")])
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(user_id=None)

    # One shard (substack) dispatched for EACH of the two tenants.
    dispatched_users = {p["user_id"] for _n, p in calls}
    assert dispatched_users == {str(user_a), str(user_b)}
    assert stats.shards_total == 2  # 1 shard x 2 users
    assert stats.succeeded == 2


async def test_explicit_user_id_does_not_enumerate_active_users(mocker) -> None:
    """An explicit ``user_id`` resolves to just that tenant without a DB read."""

    boom = mocker.patch(
        "tree.data.offline_pipeline.select_active_user_ids",
        new=mocker.AsyncMock(side_effect=AssertionError("should not enumerate")),
    )
    _patch_default_sources(mocker, [SubstackRssSource(uri="https://a.example/feed")])
    calls = _capture_run_deployment(mocker)

    stats = await data_etl_coordinator(_USER_ID)

    boom.assert_not_awaited()
    assert {p["user_id"] for _n, p in calls} == {str(_USER_ID)}
    assert stats.shards_total == 1
