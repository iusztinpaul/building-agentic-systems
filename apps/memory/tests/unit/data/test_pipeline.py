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
from tree.data.pipeline import data_pipeline

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


def _make_mock_pipeline(mocker, name: str) -> AsyncMock:
    mock = mocker.patch(f"tree.data.pipeline.{name}", new_callable=AsyncMock)
    mock.return_value = []
    return mock


def _make_config(mocker, sources: list[SourceEntry] | None = None) -> MagicMock:
    """Patch ``tree.data.pipeline.app_config`` with a flat list of source entries."""

    mock_config = MagicMock()
    mock_config.sources.sources = sources or []
    mocker.patch("tree.data.pipeline.app_config", mock_config)
    return mock_config


class TestDataPipeline:
    @pytest.fixture(autouse=True)
    def _stub_index_dim_check(self, mocker) -> None:
        """Skip the live-mongot-vs-settings dim check in unit tests.

        The flow now hard-fails at boot when ``settings.embedding_dim``
        disagrees with the mongot index (#016 + #020). Unit tests don't
        run mongot, so we stub the assertion out.
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

        _make_config(
            mocker,
            sources=[
                SubstackRssSource(uri="https://example.com/feed"),
                SubstackArticleSource(uri="https://example.com/p/article"),
                HuggingFaceDatasetSource(
                    uri="librarian-bots/arxiv-metadata-snapshot",
                    max_samples=5,
                    fetch_content=True,
                ),
                WebSource(uri="https://martinfowler.com/articles/microservices.html"),
            ],
        )

        result = await data_pipeline(_USER_ID)

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

        _make_config(
            mocker,
            sources=[
                SubstackArticleSource(uri="https://example.com/p/article"),
                HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot"),
            ],
        )

        await data_pipeline(_USER_ID)

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

        _make_config(
            mocker,
            sources=[
                SubstackRssSource(uri="https://example.com/feed"),
                HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot"),
            ],
        )

        await data_pipeline(_USER_ID)

        mock_rss.assert_awaited_once()
        mock_articles.assert_not_awaited()
        mock_arxiv.assert_awaited_once()

    async def test_skips_all_substack_variants_when_absent(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        mock_rss = _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        mock_articles = _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        _make_config(
            mocker,
            sources=[
                HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot"),
            ],
        )

        await data_pipeline(_USER_ID)

        mock_rss.assert_not_awaited()
        mock_articles.assert_not_awaited()
        mock_arxiv.assert_awaited_once()

    async def test_skips_arxiv_when_no_huggingface_dataset_entries(
        self, mocker
    ) -> None:
        # Behaviour change vs. the legacy ``ingest_all_data``: the new flow
        # runs the arxiv connector iff the flat sources list contains at
        # least one ``HuggingFaceDatasetSource``. With none configured, arxiv
        # is skipped entirely.
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        _make_config(
            mocker,
            sources=[
                SubstackRssSource(uri="https://example.com/feed"),
            ],
        )

        await data_pipeline(_USER_ID)

        mock_arxiv.assert_not_awaited()

    async def test_initializes_mongodb(self, mocker) -> None:
        mock_init = mocker.patch(
            "tree.data.pipeline.init_mongodb", new_callable=AsyncMock
        )
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        _make_config(mocker, sources=[])

        await data_pipeline(_USER_ID)

        mock_init.assert_awaited_once()

    async def test_skips_web_when_no_web_entries(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_ingest_url = _make_mock_pipeline(mocker, "ingest_url")

        _make_config(
            mocker,
            sources=[
                SubstackRssSource(uri="https://example.com/feed"),
            ],
        )

        await data_pipeline(_USER_ID)

        mock_ingest_url.assert_not_awaited()

    async def test_dispatches_each_web_entry_via_ingest_url(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_ingest_url = _make_mock_pipeline(mocker, "ingest_url")

        substack_doc, web_doc = MagicMock(), MagicMock()
        mock_ingest_url.side_effect = [substack_doc, web_doc]

        _make_config(
            mocker,
            sources=[
                WebSource(uri="https://www.decodingai.com/p/example"),
                WebSource(uri="https://martinfowler.com/articles/microservices.html"),
            ],
        )

        result = await data_pipeline(_USER_ID)

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

        _make_config(
            mocker,
            sources=[
                WebSource(uri="https://dup.example/post"),
                WebSource(uri="https://new.example/post"),
            ],
        )

        result = await data_pipeline(_USER_ID)

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
        _make_config(
            mocker,
            sources=[SubstackRssSource(uri=uri) for uri in feeds],
        )

        await data_pipeline(_USER_ID)

        mock_rss.assert_awaited_once_with(feeds, _USER_ID)

    async def test_groups_substack_article_entries_into_single_batch_call(
        self, mocker
    ) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        mock_articles = _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        articles = [f"https://blog.example.com/p/post-{i}" for i in range(10)]
        _make_config(
            mocker,
            sources=[SubstackArticleSource(uri=uri) for uri in articles],
        )

        await data_pipeline(_USER_ID)

        mock_articles.assert_awaited_once_with(articles, _USER_ID)

    async def test_passes_huggingface_dataset_overrides(self, mocker) -> None:
        # Per-entry ``max_samples`` and ``fetch_content`` must be forwarded
        # to ``ingest_arxiv_dataset``.
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        _make_config(
            mocker,
            sources=[
                HuggingFaceDatasetSource(
                    uri="librarian-bots/arxiv-metadata-snapshot",
                    max_samples=42,
                    fetch_content=True,
                ),
            ],
        )

        await data_pipeline(_USER_ID)

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
        _make_config(
            mocker,
            sources=[YouTubeRssSource(uri=uri) for uri in feeds],
        )

        result = await data_pipeline(_USER_ID)

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
        _make_config(
            mocker,
            sources=[YouTubeVideoSource(uri=uri) for uri in urls],
        )

        result = await data_pipeline(_USER_ID)

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

        _make_config(
            mocker,
            sources=[SubstackRssSource(uri="https://example.com/feed")],
        )

        with caplog.at_level(logging.INFO, logger="tree.data.pipeline"):
            await data_pipeline(_USER_ID)

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

        _make_config(
            mocker,
            sources=[
                HuggingFaceDatasetSource(uri="someone/unregistered-dataset"),
            ],
        )

        with pytest.raises(ValueError, match="someone/unregistered-dataset"):
            await data_pipeline(_USER_ID)
