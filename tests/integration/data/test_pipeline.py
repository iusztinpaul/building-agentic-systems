from unittest.mock import AsyncMock, MagicMock

from prefect import tags as prefect_tags

from twin.data.pipeline import ingest_all_data
from twin.entities.documents import Document, SourceType


FAKE_RSS_ENTRIES = [
    {
        "title": f"Test Post {i}",
        "link": f"https://blog.example.com/p/test-post-{i}",
        "summary": f"<p>Summary {i}</p>",
        "published": "Mon, 01 Jan 2024 12:00:00 GMT",
        "authors": [{"name": "Author"}],
        "content": [{"value": f"<p>Full content {i}</p>"}],
    }
    for i in range(2)
]

FAKE_ARXIV_ENTRIES = [
    {
        "id": f"2401.0000{i}",
        "title": f"Arxiv Paper {i}",
        "abstract": f"Abstract for paper {i}",
        "authors": f"Author {i}",
        "categories": "cs.AI",
        "update_date": "2024-01-15",
        "doi": None,
        "journal-ref": None,
        "submitter": f"Submitter {i}",
        "comments": None,
        "report-no": None,
        "license": None,
        "versions": [],
        "authors_parsed": [],
    }
    for i in range(2)
]

FAKE_ARTICLE_HTML = """
<html>
<head>
    <meta property="og:title" content="Test Article" />
    <meta property="og:description" content="A test article summary" />
    <meta name="author" content="Test Author" />
    <meta property="article:published_time" content="2024-06-15T10:00:00+00:00" />
</head>
<body>
    <div class="body markup">
        <p>This is the article body content.</p>
    </div>
</body>
</html>
"""


def _make_mock_rss_client(mocker) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "<rss></rss>"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


def _make_parsed_feed(entries: list[dict]) -> MagicMock:
    feed = MagicMock()
    feed.entries = [MagicMock(**entry) for entry in entries]
    for entry_mock, entry_dict in zip(feed.entries, entries):
        entry_mock.get = entry_dict.get
        entry_mock.__contains__ = lambda self, key: key in entry_dict
    return feed


def _make_mock_article_response() -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = FAKE_ARTICLE_HTML
    mock_response.raise_for_status = MagicMock()
    return mock_response


def _make_mock_article_client(mocker) -> AsyncMock:
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=_make_mock_article_response())
    return mock_client


def _make_full_config(
    mocker,
    *,
    substack_feeds: list[str] | None = None,
    substack_articles: list[str] | None = None,
    arxiv_max_samples: int = 2,
) -> MagicMock:
    mock_config = MagicMock()
    mock_config.sources.substack = substack_feeds or []
    mock_config.sources.substack_articles = substack_articles or []
    mock_config.sources.huggingface_arxiv_dataset.max_samples = arxiv_max_samples
    mock_config.sources.huggingface_arxiv_dataset.fetch_content = False
    mock_config.sources.huggingface_arxiv_dataset.batch_size = 50
    mock_config.sources.huggingface_arxiv_dataset.concurrency = 10
    mocker.patch("twin.data.pipeline.app_config", mock_config)
    mocker.patch("twin.data.huggingface.arxiv_dataset_pipeline.app_config", mock_config)
    return mock_config


def _mock_init_mongodb(mocker, mongo_client) -> None:
    mocker.patch("twin.data.pipeline.init_mongodb", return_value=mongo_client)
    mocker.patch(
        "twin.data.substack.substack_rss_pipeline.init_mongodb",
        return_value=mongo_client,
    )
    mocker.patch(
        "twin.data.substack.substack_article_pipeline.init_mongodb",
        return_value=mongo_client,
    )
    mocker.patch(
        "twin.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
        return_value=mongo_client,
    )


def _mock_rss_source(mocker) -> None:
    mocker.patch(
        "twin.data.substack.substack_rss.httpx.AsyncClient",
        return_value=_make_mock_rss_client(mocker),
    )
    mocker.patch(
        "twin.data.substack.substack_rss.feedparser.parse",
        return_value=_make_parsed_feed(FAKE_RSS_ENTRIES),
    )


def _mock_article_source(mocker) -> None:
    mocker.patch(
        "twin.data.substack.substack_article.httpx.AsyncClient",
        return_value=_make_mock_article_client(mocker),
    )


def _mock_arxiv_source(mocker) -> None:
    def batch_gen(max_samples, batch_size):
        yield FAKE_ARXIV_ENTRIES

    mocker.patch(
        "twin.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
        side_effect=batch_gen,
    )


class TestIngestAllData:
    async def test_runs_all_three_pipelines(self, mongo_client, mocker) -> None:
        _mock_init_mongodb(mocker, mongo_client)
        _mock_rss_source(mocker)
        _mock_article_source(mocker)
        _mock_arxiv_source(mocker)
        _make_full_config(
            mocker,
            substack_feeds=["https://blog.example.com/feed"],
            substack_articles=[
                "https://blog.example.com/p/article-1",
                "https://blog.example.com/p/article-2",
            ],
        )

        with prefect_tags("tests"):
            result = await ingest_all_data()

        substack_docs = [d for d in result if d.source_type == SourceType.SUBSTACK]
        hf_docs = [d for d in result if d.source_type == SourceType.HUGGINGFACE]
        assert len(substack_docs) >= 2
        assert len(hf_docs) == 2
        assert len(result) >= 4

        db_docs = await Document.find_all().to_list()
        assert len(db_docs) >= 4

    async def test_runs_only_rss_and_arxiv_when_no_articles(
        self, mongo_client, mocker
    ) -> None:
        _mock_init_mongodb(mocker, mongo_client)
        _mock_rss_source(mocker)
        _mock_arxiv_source(mocker)
        _make_full_config(
            mocker,
            substack_feeds=["https://blog.example.com/feed"],
            substack_articles=[],
        )

        with prefect_tags("tests"):
            result = await ingest_all_data()

        substack_docs = [d for d in result if d.source_type == SourceType.SUBSTACK]
        hf_docs = [d for d in result if d.source_type == SourceType.HUGGINGFACE]
        assert len(substack_docs) == 2
        assert len(hf_docs) == 2

    async def test_runs_only_articles_and_arxiv_when_no_feeds(
        self, mongo_client, mocker
    ) -> None:
        _mock_init_mongodb(mocker, mongo_client)
        _mock_article_source(mocker)
        _mock_arxiv_source(mocker)
        _make_full_config(
            mocker,
            substack_feeds=[],
            substack_articles=["https://blog.example.com/p/article-1"],
        )

        with prefect_tags("tests"):
            result = await ingest_all_data()

        substack_docs = [d for d in result if d.source_type == SourceType.SUBSTACK]
        hf_docs = [d for d in result if d.source_type == SourceType.HUGGINGFACE]
        assert len(substack_docs) >= 1
        assert len(hf_docs) == 2

    async def test_runs_only_arxiv_when_no_substack(self, mongo_client, mocker) -> None:
        _mock_init_mongodb(mocker, mongo_client)
        _mock_arxiv_source(mocker)
        _make_full_config(
            mocker,
            substack_feeds=[],
            substack_articles=[],
        )

        with prefect_tags("tests"):
            result = await ingest_all_data()

        assert all(d.source_type == SourceType.HUGGINGFACE for d in result)
        assert len(result) == 2
