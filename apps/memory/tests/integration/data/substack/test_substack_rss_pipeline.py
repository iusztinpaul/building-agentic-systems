"""Integration tests for the substack RSS batch flow (#079).

Drive the real Prefect ``ingest_substack_rss_feed_batch`` flow against a live Mongo
(only the network feed-fetch + feed parse are mocked) and assert it persists Documents
built from FEED-EMBEDDED content. The per-feed sub-flow is gone — every case goes through
the batch flow now. A dedicated test proves the RSS path NEVER constructs the article
scrape client (no per-article re-fetch).
"""

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

import tree.data.substack.substack_article as substack_article
from tree.data.substack.substack_rss_pipeline import ingest_substack_rss_feed_batch
from tree.entities.documents import Document, SourceType

FAKE_RSS_ENTRIES = [
    {
        "link": f"https://blog.example.com/p/post-{i}",
        "title": f"Test Post {i}",
        "author": f"Author {i}",
        "published": "Mon, 15 Jan 2024 12:00:00 GMT",
        "summary": f"Summary of post {i}.",
        "content": [
            {
                "value": (
                    f"<p>Content of post {i}. "
                    f'See <a href="https://external.com/ref-{i}">reference</a>.</p>'
                )
            }
        ],
    }
    for i in range(3)
]


class TestIngestSubstackRssFeedBatchFlow:
    async def test_ingests_documents_via_prefect_flow(
        self, mongo_client, mocker
    ) -> None:
        mocker.patch(
            "tree.data.substack.substack_rss.httpx.AsyncClient",
            return_value=_make_mock_rss_client(mocker),
        )
        mocker.patch(
            "tree.data.substack.substack_rss.feedparser.parse",
            return_value=_make_parsed_feed(FAKE_RSS_ENTRIES),
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        with prefect_tags("tests"):
            result = await ingest_substack_rss_feed_batch(
                ["https://blog.example.com/feed"], PydanticObjectId()
            )

        assert len(result) == 3
        for doc in result:
            assert doc.source_type == SourceType.SUBSTACK
            assert doc.id is not None
            assert doc.title.startswith("Test Post")

        db_docs = await Document.find(
            Document.source_type == SourceType.SUBSTACK
        ).to_list()
        assert len(db_docs) == 3

    async def test_does_not_re_fetch_articles(self, mongo_client, mocker) -> None:
        """The RSS path builds from feed-embedded content — never scrapes articles."""

        mocker.patch(
            "tree.data.substack.substack_rss.httpx.AsyncClient",
            return_value=_make_mock_rss_client(mocker),
        )
        mocker.patch(
            "tree.data.substack.substack_rss.feedparser.parse",
            return_value=_make_parsed_feed(FAKE_RSS_ENTRIES),
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.init_mongodb",
            return_value=mongo_client,
        )
        # If the RSS flow ever re-scraped an article it would call one of these
        # article-scrape entry points (note: substack_rss + substack_article share
        # the SAME ``httpx`` module, so we spy the article-specific functions rather
        # than the shared ``httpx.AsyncClient`` to avoid tripping the feed fetch).
        fetch_article_spy = mocker.spy(substack_article, "fetch_article")
        fetch_and_extract_spy = mocker.spy(substack_article, "fetch_and_extract")

        with prefect_tags("tests"):
            result = await ingest_substack_rss_feed_batch(
                ["https://blog.example.com/feed"], PydanticObjectId()
            )

        assert len(result) == 3
        # Zero article scrapes: neither article-scrape entry point is invoked.
        fetch_article_spy.assert_not_called()
        fetch_and_extract_spy.assert_not_called()

    @pytest.mark.slow
    async def test_idempotent_on_rerun(self, mongo_client, mocker) -> None:
        mocker.patch(
            "tree.data.substack.substack_rss.httpx.AsyncClient",
            return_value=_make_mock_rss_client(mocker),
        )
        mocker.patch(
            "tree.data.substack.substack_rss.feedparser.parse",
            return_value=_make_parsed_feed(FAKE_RSS_ENTRIES),
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        user_id = PydanticObjectId()
        with prefect_tags("tests"):
            first_run = await ingest_substack_rss_feed_batch(
                ["https://blog.example.com/feed"], user_id
            )
        assert len(first_run) == 3

        mocker.patch(
            "tree.data.substack.substack_rss.httpx.AsyncClient",
            return_value=_make_mock_rss_client(mocker),
        )
        mocker.patch(
            "tree.data.substack.substack_rss.feedparser.parse",
            return_value=_make_parsed_feed(FAKE_RSS_ENTRIES),
        )

        with prefect_tags("tests"):
            second_run = await ingest_substack_rss_feed_batch(
                ["https://blog.example.com/feed"], user_id
            )
        assert len(second_run) == 0

        db_docs = await Document.find(
            Document.source_type == SourceType.SUBSTACK
        ).to_list()
        assert len(db_docs) == 3

    async def test_creates_reference_documents(self, mongo_client, mocker) -> None:
        mocker.patch(
            "tree.data.substack.substack_rss.httpx.AsyncClient",
            return_value=_make_mock_rss_client(mocker),
        )
        mocker.patch(
            "tree.data.substack.substack_rss.feedparser.parse",
            return_value=_make_parsed_feed(FAKE_RSS_ENTRIES[:1]),
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        with prefect_tags("tests"):
            result = await ingest_substack_rss_feed_batch(
                ["https://blog.example.com/feed"], PydanticObjectId()
            )

        assert len(result) == 1
        assert len(result[0].references) == 1
        assert result[0].references[0].source_uri == "https://external.com/ref-0"

        latent = await Document.find_one(
            Document.source_uri == "https://external.com/ref-0"
        )
        assert latent is not None
        assert latent.source_type == SourceType.LATENT

    async def test_upgrades_latent_document(self, mongo_client, mocker) -> None:
        user_id = PydanticObjectId()
        latent = Document(
            source_type=SourceType.LATENT,
            source_uri="https://blog.example.com/p/post-0",
            user_id=user_id,
        )
        await latent.insert()

        mocker.patch(
            "tree.data.substack.substack_rss.httpx.AsyncClient",
            return_value=_make_mock_rss_client(mocker),
        )
        mocker.patch(
            "tree.data.substack.substack_rss.feedparser.parse",
            return_value=_make_parsed_feed(FAKE_RSS_ENTRIES[:1]),
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        with prefect_tags("tests"):
            result = await ingest_substack_rss_feed_batch(
                ["https://blog.example.com/feed"], user_id
            )

        assert len(result) == 1
        assert result[0].id == latent.id
        assert result[0].source_type == SourceType.SUBSTACK
        assert result[0].title == "Test Post 0"

    async def test_ingests_from_multiple_feeds(self, mongo_client, mocker) -> None:
        mocker.patch(
            "tree.data.substack.substack_rss.httpx.AsyncClient",
            return_value=_make_mock_rss_client(mocker),
        )
        mocker.patch(
            "tree.data.substack.substack_rss.feedparser.parse",
            return_value=_make_parsed_feed(FAKE_RSS_ENTRIES[:1]),
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        with prefect_tags("tests"):
            result = await ingest_substack_rss_feed_batch(
                feed_urls=[
                    "https://blog-a.example.com/feed",
                    "https://blog-b.example.com/feed",
                ],
                user_id=PydanticObjectId(),
            )

        # Both feeds return the same 1 entry with the same link, so second is a dup.
        assert len(result) == 1


def _make_mock_rss_client(mocker):
    mock_response = mocker.Mock()
    mock_response.status_code = 200
    mock_response.text = "<rss></rss>"
    mock_response.raise_for_status = mocker.Mock()

    mock_client = mocker.AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    return mock_client


def _make_parsed_feed(entries: list[dict]):
    class FakeFeed:
        bozo = False
        bozo_exception = None

    feed = FakeFeed()
    feed.entries = entries
    return feed
