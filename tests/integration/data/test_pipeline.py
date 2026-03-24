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


class TestIngestAllData:
    async def test_runs_all_enabled_pipelines(self, mongo_client, mocker) -> None:
        mocker.patch(
            "twin.data.substack.substack_rss.httpx.AsyncClient",
            return_value=_make_mock_rss_client(mocker),
        )
        mocker.patch(
            "twin.data.substack.substack_rss.feedparser.parse",
            return_value=_make_parsed_feed(FAKE_RSS_ENTRIES),
        )

        def batch_gen(max_samples, batch_size):
            yield FAKE_ARXIV_ENTRIES

        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
            side_effect=batch_gen,
        )

        mock_config = MagicMock()
        mock_config.data_pipeline.substack.enabled = True
        mock_config.data_pipeline.substack.feeds = ["https://blog.example.com/feed"]
        mock_config.data_pipeline.huggingface_arxiv_dataset.enabled = True
        mock_config.data_pipeline.huggingface_arxiv_dataset.max_samples = 2
        mock_config.data_pipeline.huggingface_arxiv_dataset.fetch_content = False
        mock_config.data_pipeline.huggingface_arxiv_dataset.batch_size = 50
        mock_config.data_pipeline.huggingface_arxiv_dataset.concurrency = 10
        mocker.patch("twin.data.pipeline.app_config", mock_config)
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline.app_config", mock_config
        )
        mocker.patch("twin.data.pipeline.init_mongodb", return_value=mongo_client)
        mocker.patch(
            "twin.data.substack.substack_rss_pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        with prefect_tags("tests"):
            result = await ingest_all_data()

        substack_docs = [d for d in result if d.source_type == SourceType.SUBSTACK]
        hf_docs = [d for d in result if d.source_type == SourceType.HUGGINGFACE]
        assert len(substack_docs) == 2
        assert len(hf_docs) == 2

        db_docs = await Document.find_all().to_list()
        assert len(db_docs) >= 4

    async def test_skips_disabled_pipelines(self, mongo_client, mocker) -> None:
        mock_config = MagicMock()
        mock_config.data_pipeline.substack.enabled = False
        mock_config.data_pipeline.substack.feeds = []
        mock_config.data_pipeline.huggingface_arxiv_dataset.enabled = False
        mocker.patch("twin.data.pipeline.app_config", mock_config)
        mocker.patch("twin.data.pipeline.init_mongodb", return_value=mongo_client)

        with prefect_tags("tests"):
            result = await ingest_all_data()

        assert result == []
        db_docs = await Document.find_all().to_list()
        assert len(db_docs) == 0
