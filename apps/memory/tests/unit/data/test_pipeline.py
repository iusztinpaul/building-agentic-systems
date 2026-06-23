"""Unit tests for the data worker's per-source-type dispatch (#068).

#068 split the data pipeline into an orchestrator
(``data-etl-orchestrator``) that shards the configured ``sources:`` list and a worker
(``data-etl-worker``) that ingests one shard. The per-source-type batch logic — the
grouping / batched-call / skip-when-absent / unknown-HF-id behavior that the old
single ``data_pipeline`` flow owned — now lives in the WORKER, so this suite exercises
``data_etl_worker`` directly. ``sources`` is passed as typed ``SourceEntry`` objects
(which pass through ``_coerce_sources`` unchanged); a separate test covers the
serialized-dict round-trip the orchestrator actually dispatches.

The orchestrator fan-out (partition → dispatch → no-index) is covered in
``test_orchestrator_data.py``; the pure fan-out core in ``test_fanout_data.py``.
"""

from unittest.mock import AsyncMock, MagicMock

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
from tree.data.pipeline import _BATCHED_VARIANTS, data_etl_worker

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


def test_every_batched_variant_resolves_without_mocks() -> None:
    """Each ``_BatchedVariant.batch_fn`` resolves to a real callable WITHOUT mocks.

    ``batch_fn`` looks the sub-flow up by name in the module namespace
    (``globals()[batch_fn_name]``), so the ``ingest_*_batch`` functions MUST be
    imported at module top to be present. Dropping those imports makes every
    resolution raise ``KeyError`` at runtime — a production crash on the first
    configured Substack/YouTube source. The mock-based dispatch tests can't catch
    this because ``mocker.patch`` installs the missing name for the test's
    duration; this guard deliberately uses NO mocks so the missing import surfaces.
    """

    for variant in _BATCHED_VARIANTS:
        assert callable(variant.batch_fn), (
            f"{variant.batch_fn_name} is not importable as a module global"
        )


def _make_mock_pipeline(mocker, name: str) -> AsyncMock:
    mock = mocker.patch(f"tree.data.pipeline.{name}", new_callable=AsyncMock)
    mock.return_value = []
    return mock


class TestDataWorker:
    @pytest.fixture(autouse=True)
    def _stub_index_dim_check(self, mocker) -> None:
        """Skip the live-mongot-vs-settings dim check in unit tests.

        The worker hard-fails at boot when
        ``app_config.models.search_embedding.dimensions`` disagrees with the mongot
        index (#016 + #020 + #034). Unit tests don't run mongot, so we stub the
        assertion out.
        """

        mocker.patch(
            "tree.data.pipeline.assert_settings_match_live_vector_index",
            new_callable=AsyncMock,
        )

    async def test_dispatches_each_variant(self, mocker) -> None:
        mock_init = mocker.patch(
            "tree.data.pipeline.init_mongodb", new_callable=AsyncMock
        )
        mock_rss = _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        mock_articles = _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_web = _make_mock_pipeline(mocker, "ingest_web_url_batch")

        doc_a, doc_b, doc_c, doc_d = (
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        mock_rss.return_value = [doc_a]
        mock_articles.return_value = [doc_b]
        mock_arxiv.return_value = [doc_c]
        mock_web.return_value = [doc_d]

        sources: list[SourceEntry] = [
            SubstackRssSource(uri="https://example.com/feed"),
            SubstackArticleSource(uri="https://example.com/p/article"),
            HuggingFaceDatasetSource(
                uri="librarian-bots/arxiv-metadata-snapshot",
                max_samples=5,
                fetch_content=True,
            ),
            WebSource(uri="https://martinfowler.com/articles/microservices.html"),
        ]

        result = await data_etl_worker(_USER_ID, sources)

        assert len(result) == 4
        mock_init.assert_awaited_once()
        mock_rss.assert_awaited_once_with(["https://example.com/feed"], _USER_ID)
        mock_articles.assert_awaited_once_with(
            ["https://example.com/p/article"], _USER_ID
        )
        mock_arxiv.assert_awaited_once_with(
            user_id=_USER_ID, max_samples=5, fetch_content=True, offset=None
        )
        mock_web.assert_awaited_once_with(
            ["https://martinfowler.com/articles/microservices.html"], _USER_ID
        )

    async def test_skips_rss_when_no_substack_rss_entries(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        mock_rss = _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        mock_articles = _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        sources: list[SourceEntry] = [
            SubstackArticleSource(uri="https://example.com/p/article"),
            HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot"),
        ]

        await data_etl_worker(_USER_ID, sources)

        mock_rss.assert_not_awaited()
        mock_articles.assert_awaited_once()
        mock_arxiv.assert_awaited_once()

    async def test_skips_articles_when_no_substack_article_entries(
        self, mocker
    ) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        mock_rss = _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        mock_articles = _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        sources: list[SourceEntry] = [
            SubstackRssSource(uri="https://example.com/feed"),
            HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot"),
        ]

        await data_etl_worker(_USER_ID, sources)

        mock_rss.assert_awaited_once()
        mock_articles.assert_not_awaited()
        mock_arxiv.assert_awaited_once()

    async def test_skips_all_substack_variants_when_absent(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        mock_rss = _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        mock_articles = _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        sources: list[SourceEntry] = [
            HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot"),
        ]

        await data_etl_worker(_USER_ID, sources)

        mock_rss.assert_not_awaited()
        mock_articles.assert_not_awaited()
        mock_arxiv.assert_awaited_once()

    async def test_skips_arxiv_when_no_huggingface_dataset_entries(
        self, mocker
    ) -> None:
        # The worker runs the arxiv connector iff the shard contains at least one
        # ``HuggingFaceDatasetSource``. With none, arxiv is skipped entirely.
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        sources: list[SourceEntry] = [
            SubstackRssSource(uri="https://example.com/feed"),
        ]

        await data_etl_worker(_USER_ID, sources)

        mock_arxiv.assert_not_awaited()

    async def test_initializes_mongodb(self, mocker) -> None:
        mock_init = mocker.patch(
            "tree.data.pipeline.init_mongodb", new_callable=AsyncMock
        )
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        await data_etl_worker(_USER_ID, [])

        mock_init.assert_awaited_once()

    async def test_skips_web_when_no_web_entries(self, mocker, caplog) -> None:
        import logging

        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_web = _make_mock_pipeline(mocker, "ingest_web_url_batch")

        sources: list[SourceEntry] = [
            SubstackRssSource(uri="https://example.com/feed"),
        ]

        with caplog.at_level(logging.INFO, logger="tree.data.pipeline"):
            await data_etl_worker(_USER_ID, sources)

        mock_web.assert_not_awaited()
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "Web pipeline skipped: no web entries configured" in m for m in messages
        )

    async def test_batches_web_entries_into_single_call(self, mocker) -> None:
        # Multiple web entries produce ONE call to the batch flow with all URLs as
        # a single list — not one per-URL call (mirrors the substack-article batch).
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_web = _make_mock_pipeline(mocker, "ingest_web_url_batch")

        web_doc = MagicMock()
        mock_web.return_value = [web_doc]

        urls = [
            "https://www.decodingai.com/p/example",
            "https://martinfowler.com/articles/microservices.html",
        ]
        sources: list[SourceEntry] = [WebSource(uri=uri) for uri in urls]

        result = await data_etl_worker(_USER_ID, sources)

        mock_web.assert_awaited_once_with(urls, _USER_ID)
        assert web_doc in result

    async def test_web_is_dispatched_after_youtube_video(self, mocker) -> None:
        # Web is the LAST batched variant: its batch call must be awaited AFTER the
        # YouTube-video batch call (ingestion + log order preserved).
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        _make_mock_pipeline(mocker, "ingest_youtube_rss_feed_batch")
        manager = mocker.MagicMock()
        mock_yt_video = _make_mock_pipeline(mocker, "ingest_youtube_video_batch")
        mock_web = _make_mock_pipeline(mocker, "ingest_web_url_batch")
        manager.attach_mock(mock_yt_video, "youtube_video")
        manager.attach_mock(mock_web, "web")

        sources: list[SourceEntry] = [
            WebSource(uri="https://martinfowler.com/articles/microservices.html"),
            YouTubeVideoSource(uri="https://youtu.be/eYaWxljC4sA"),
        ]

        await data_etl_worker(_USER_ID, sources)

        ordered_calls = [c[0] for c in manager.mock_calls]
        assert ordered_calls == ["youtube_video", "web"]

    async def test_returns_batch_docs_without_double_filtering(self, mocker) -> None:
        # ``ingest_web_url_batch`` already filters ``None`` internally and returns a
        # ``list[Document]``; the worker just extends with that list — it must NOT
        # re-filter or otherwise transform the returned docs.
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_web = _make_mock_pipeline(mocker, "ingest_web_url_batch")

        kept = MagicMock()
        mock_web.return_value = [kept]

        sources: list[SourceEntry] = [
            WebSource(uri="https://dup.example/post"),
            WebSource(uri="https://new.example/post"),
        ]

        result = await data_etl_worker(_USER_ID, sources)

        assert result == [kept]

    async def test_groups_substack_rss_entries_into_single_batch_call(
        self, mocker
    ) -> None:
        # Five RSS entries should produce ONE call to the batch flow with
        # all five URIs as a single list — not five separate calls.
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        mock_rss = _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        feeds = [f"https://blog{i}.example.com/feed" for i in range(5)]
        sources: list[SourceEntry] = [SubstackRssSource(uri=uri) for uri in feeds]

        await data_etl_worker(_USER_ID, sources)

        mock_rss.assert_awaited_once_with(feeds, _USER_ID)

    async def test_groups_substack_article_entries_into_single_batch_call(
        self, mocker
    ) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        mock_articles = _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        articles = [f"https://blog.example.com/p/post-{i}" for i in range(10)]
        sources: list[SourceEntry] = [
            SubstackArticleSource(uri=uri) for uri in articles
        ]

        await data_etl_worker(_USER_ID, sources)

        mock_articles.assert_awaited_once_with(articles, _USER_ID)

    async def test_passes_huggingface_dataset_overrides(self, mocker) -> None:
        # Per-entry ``max_samples`` and ``fetch_content`` must be forwarded
        # to ``ingest_arxiv_dataset``. A non-windowed entry forwards ``offset=None``.
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        sources: list[SourceEntry] = [
            HuggingFaceDatasetSource(
                uri="librarian-bots/arxiv-metadata-snapshot",
                max_samples=42,
                fetch_content=True,
            ),
        ]

        await data_etl_worker(_USER_ID, sources)

        mock_arxiv.assert_awaited_once_with(
            user_id=_USER_ID, max_samples=42, fetch_content=True, offset=None
        )

    async def test_forwards_huggingface_dataset_offset_window(self, mocker) -> None:
        # A windowed entry (``offset`` stamped by the #072 orchestrator) forwards
        # that ``offset`` to ``ingest_arxiv_dataset`` so the worker ingests exactly
        # rows ``[offset, offset + max_samples)``.
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        sources: list[SourceEntry] = [
            HuggingFaceDatasetSource(
                uri="librarian-bots/arxiv-metadata-snapshot",
                max_samples=250,
                offset=250,
            ),
        ]

        await data_etl_worker(_USER_ID, sources)

        mock_arxiv.assert_awaited_once_with(
            user_id=_USER_ID, max_samples=250, fetch_content=False, offset=250
        )

    async def test_dispatches_youtube_rss_entries(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_yt_rss = _make_mock_pipeline(mocker, "ingest_youtube_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_youtube_video_batch")

        doc = MagicMock()
        mock_yt_rss.return_value = [doc]

        feeds = [
            "https://www.youtube.com/feeds/videos.xml?channel_id=UC1",
            "https://www.youtube.com/feeds/videos.xml?channel_id=UC2",
        ]
        sources: list[SourceEntry] = [YouTubeRssSource(uri=uri) for uri in feeds]

        result = await data_etl_worker(_USER_ID, sources)

        mock_yt_rss.assert_awaited_once_with(feeds, _USER_ID)
        assert doc in result

    async def test_dispatches_youtube_video_entries(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        _make_mock_pipeline(mocker, "ingest_youtube_rss_feed_batch")
        mock_yt_video = _make_mock_pipeline(mocker, "ingest_youtube_video_batch")

        doc = MagicMock()
        mock_yt_video.return_value = [doc]

        urls = [
            "https://www.youtube.com/watch?v=eYaWxljC4sA",
            "https://youtu.be/eYaWxljC4sA",
        ]
        sources: list[SourceEntry] = [YouTubeVideoSource(uri=uri) for uri in urls]

        result = await data_etl_worker(_USER_ID, sources)

        mock_yt_video.assert_awaited_once_with(urls, _USER_ID)
        assert doc in result

    async def test_skips_youtube_branches_when_absent(self, mocker, caplog) -> None:
        import logging

        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_yt_rss = _make_mock_pipeline(mocker, "ingest_youtube_rss_feed_batch")
        mock_yt_video = _make_mock_pipeline(mocker, "ingest_youtube_video_batch")

        sources: list[SourceEntry] = [SubstackRssSource(uri="https://example.com/feed")]

        with caplog.at_level(logging.INFO, logger="tree.data.pipeline"):
            await data_etl_worker(_USER_ID, sources)

        mock_yt_rss.assert_not_awaited()
        mock_yt_video.assert_not_awaited()
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "YouTube RSS pipeline skipped: no youtube_rss entries configured" in m
            for m in messages
        )
        assert any(
            "YouTube video pipeline skipped: no youtube_video entries configured" in m
            for m in messages
        )

    async def test_raises_for_unknown_huggingface_dataset_id(self, mocker) -> None:
        # Unknown HF dataset ids must fail loudly so an operator notices the
        # missing registration rather than silently skipping ingestion.
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        sources: list[SourceEntry] = [
            HuggingFaceDatasetSource(uri="someone/unregistered-dataset"),
        ]

        with pytest.raises(ValueError, match="someone/unregistered-dataset"):
            await data_etl_worker(_USER_ID, sources)

    async def test_reconstructs_sources_from_serialized_dicts(self, mocker) -> None:
        """A shard arrives serialized (``list[dict]``) and is re-parsed to typed
        ``SourceEntry`` objects before grouping — the round-trip the orchestrator
        actually dispatches.
        """

        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        mock_rss = _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        # The dispatched shape: plain dicts (as Prefect JSON-serializes parameters).
        serialized = [
            SubstackRssSource(uri="https://example.com/feed").model_dump(),
            HuggingFaceDatasetSource(
                uri="librarian-bots/arxiv-metadata-snapshot",
                max_samples=7,
                fetch_content=True,
            ).model_dump(),
        ]

        await data_etl_worker(_USER_ID, serialized)

        mock_rss.assert_awaited_once_with(["https://example.com/feed"], _USER_ID)
        mock_arxiv.assert_awaited_once_with(
            user_id=_USER_ID, max_samples=7, fetch_content=True, offset=None
        )
