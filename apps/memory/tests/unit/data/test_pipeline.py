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
from tree.data.pipeline import data_etl_worker

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


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
        mock_ingest_url = _make_mock_pipeline(mocker, "ingest_url")

        doc_a, doc_b, doc_c, doc_d = (
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        mock_rss.return_value = [doc_a]
        mock_articles.return_value = [doc_b]
        mock_arxiv.return_value = [doc_c]
        mock_ingest_url.return_value = doc_d

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
            user_id=_USER_ID, max_samples=5, fetch_content=True
        )
        mock_ingest_url.assert_awaited_once_with(
            "https://martinfowler.com/articles/microservices.html", _USER_ID
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

    async def test_skips_web_when_no_web_entries(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_ingest_url = _make_mock_pipeline(mocker, "ingest_url")

        sources: list[SourceEntry] = [
            SubstackRssSource(uri="https://example.com/feed"),
        ]

        await data_etl_worker(_USER_ID, sources)

        mock_ingest_url.assert_not_awaited()

    async def test_dispatches_each_web_entry_via_ingest_url(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_ingest_url = _make_mock_pipeline(mocker, "ingest_url")

        substack_doc, web_doc = MagicMock(), MagicMock()
        mock_ingest_url.side_effect = [substack_doc, web_doc]

        sources: list[SourceEntry] = [
            WebSource(uri="https://www.decodingai.com/p/example"),
            WebSource(uri="https://martinfowler.com/articles/microservices.html"),
        ]

        result = await data_etl_worker(_USER_ID, sources)

        assert mock_ingest_url.await_count == 2
        awaited_urls = [call.args[0] for call in mock_ingest_url.await_args_list]
        assert awaited_urls == [
            "https://www.decodingai.com/p/example",
            "https://martinfowler.com/articles/microservices.html",
        ]
        assert substack_doc in result
        assert web_doc in result

    async def test_filters_none_results_from_web_dispatcher(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_ingest_url = _make_mock_pipeline(mocker, "ingest_url")

        kept = MagicMock()
        mock_ingest_url.side_effect = [None, kept]

        sources: list[SourceEntry] = [
            WebSource(uri="https://dup.example/post"),
            WebSource(uri="https://new.example/post"),
        ]

        result = await data_etl_worker(_USER_ID, sources)

        assert kept in result
        assert None not in result

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
        # to ``ingest_arxiv_dataset``.
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
            user_id=_USER_ID, max_samples=42, fetch_content=True
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
            user_id=_USER_ID, max_samples=7, fetch_content=True
        )
