from unittest.mock import AsyncMock, MagicMock

from tree.data.pipeline import ingest_all_data


def _make_mock_pipeline(mocker, name: str) -> AsyncMock:
    mock = mocker.patch(f"tree.data.pipeline.{name}", new_callable=AsyncMock)
    mock.return_value = []
    return mock


def _make_config(
    mocker,
    *,
    substack_feeds: list[str] | None = None,
    substack_articles: list[str] | None = None,
    arxiv_max_samples: int = 10,
    urls: list[str] | None = None,
) -> MagicMock:
    mock_config = MagicMock()
    mock_config.sources.substack = substack_feeds or []
    mock_config.sources.substack_articles = substack_articles or []
    mock_config.sources.huggingface_arxiv_dataset.max_samples = arxiv_max_samples
    mock_config.sources.urls = urls or []
    mocker.patch("tree.data.pipeline.app_config", mock_config)
    return mock_config


class TestIngestAllData:
    async def test_runs_all_three_pipelines(self, mocker) -> None:
        mock_init = mocker.patch(
            "tree.data.pipeline.init_mongodb", new_callable=AsyncMock
        )
        mock_rss = _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        mock_articles = _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        doc_a, doc_b, doc_c = MagicMock(), MagicMock(), MagicMock()
        mock_rss.return_value = [doc_a]
        mock_articles.return_value = [doc_b]
        mock_arxiv.return_value = [doc_c]

        _make_config(
            mocker,
            substack_feeds=["https://example.com/feed"],
            substack_articles=["https://example.com/p/article"],
        )

        result = await ingest_all_data()

        assert len(result) == 3
        mock_init.assert_awaited_once()
        mock_rss.assert_awaited_once_with(["https://example.com/feed"])
        mock_articles.assert_awaited_once_with(["https://example.com/p/article"])
        mock_arxiv.assert_awaited_once()

    async def test_skips_rss_when_no_feeds(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        mock_rss = _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        mock_articles = _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        _make_config(
            mocker,
            substack_feeds=[],
            substack_articles=["https://example.com/p/article"],
        )

        await ingest_all_data()

        mock_rss.assert_not_awaited()
        mock_articles.assert_awaited_once()
        mock_arxiv.assert_awaited_once()

    async def test_skips_articles_when_none_configured(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        mock_rss = _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        mock_articles = _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        _make_config(
            mocker,
            substack_feeds=["https://example.com/feed"],
            substack_articles=[],
        )

        await ingest_all_data()

        mock_rss.assert_awaited_once()
        mock_articles.assert_not_awaited()
        mock_arxiv.assert_awaited_once()

    async def test_skips_all_substack_when_empty(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        mock_rss = _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        mock_articles = _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        _make_config(mocker, substack_feeds=[], substack_articles=[])

        await ingest_all_data()

        mock_rss.assert_not_awaited()
        mock_articles.assert_not_awaited()
        mock_arxiv.assert_awaited_once()

    async def test_always_runs_arxiv(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        mock_arxiv = _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_arxiv.return_value = [MagicMock()]

        _make_config(mocker, substack_feeds=[], substack_articles=[])

        result = await ingest_all_data()

        assert len(result) == 1
        mock_arxiv.assert_awaited_once()

    async def test_initializes_mongodb(self, mocker) -> None:
        mock_init = mocker.patch(
            "tree.data.pipeline.init_mongodb", new_callable=AsyncMock
        )
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")

        _make_config(mocker, substack_feeds=[], substack_articles=[])

        await ingest_all_data()

        mock_init.assert_awaited_once()

    async def test_skips_urls_when_empty(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_ingest_url = _make_mock_pipeline(mocker, "ingest_url")

        _make_config(mocker, urls=[])

        await ingest_all_data()

        mock_ingest_url.assert_not_awaited()

    async def test_dispatches_each_url_via_dispatcher(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_ingest_url = _make_mock_pipeline(mocker, "ingest_url")

        substack_doc, web_doc = MagicMock(), MagicMock()
        mock_ingest_url.side_effect = [substack_doc, web_doc]

        _make_config(
            mocker,
            urls=[
                "https://www.decodingai.com/p/example",
                "https://martinfowler.com/articles/microservices.html",
            ],
        )

        result = await ingest_all_data()

        assert mock_ingest_url.await_count == 2
        awaited_urls = [call.args[0] for call in mock_ingest_url.await_args_list]
        assert awaited_urls == [
            "https://www.decodingai.com/p/example",
            "https://martinfowler.com/articles/microservices.html",
        ]
        assert substack_doc in result
        assert web_doc in result

    async def test_filters_none_results_from_url_dispatcher(self, mocker) -> None:
        mocker.patch("tree.data.pipeline.init_mongodb", new_callable=AsyncMock)
        _make_mock_pipeline(mocker, "ingest_substack_rss_feed_batch")
        _make_mock_pipeline(mocker, "ingest_substack_article_batch")
        _make_mock_pipeline(mocker, "ingest_arxiv_dataset")
        mock_ingest_url = _make_mock_pipeline(mocker, "ingest_url")

        kept = MagicMock()
        mock_ingest_url.side_effect = [None, kept]

        _make_config(
            mocker,
            urls=["https://dup.example/post", "https://new.example/post"],
        )

        result = await ingest_all_data()

        assert kept in result
        assert None not in result
