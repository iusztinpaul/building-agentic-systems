from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.config.app_config import (
    HuggingFaceDatasetSource,
    SourceEntry,
    SubstackArticleSource,
    SubstackRssSource,
    load_app_config,
)
from tree.data.pipeline import data_etl_worker
from tree.entities.documents import Document, SourceType

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


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
    """Patch ``app_config`` with a flat list of typed source entries.

    The ``data-etl-worker`` is handed ``mock_config.sources.sources`` directly
    (#068 moved the per-variant dispatch into the worker, which takes its sources
    as an argument rather than reading ``app_config``). The arxiv connector still
    reads its defaults from ``app_config``, so that module is patched too.
    """

    sources: list[SourceEntry] = []
    for feed_url in substack_feeds or []:
        sources.append(SubstackRssSource(uri=feed_url))
    for article_url in substack_articles or []:
        sources.append(SubstackArticleSource(uri=article_url))
    sources.append(
        HuggingFaceDatasetSource(
            uri="librarian-bots/arxiv-metadata-snapshot",
            max_samples=arxiv_max_samples,
            fetch_content=False,
            batch_size=50,
            concurrency=10,
        )
    )

    mock_config = MagicMock()
    mock_config.sources.sources = sources
    mocker.patch("tree.data.pipeline.app_config", mock_config)
    mocker.patch("tree.data.huggingface.arxiv_dataset_pipeline.app_config", mock_config)
    return mock_config


def _mock_init_mongodb(mocker, mongo_client) -> None:
    mocker.patch("tree.data.pipeline.init_mongodb", return_value=mongo_client)
    mocker.patch(
        "tree.data.substack.substack_rss_pipeline.init_mongodb",
        return_value=mongo_client,
    )
    mocker.patch(
        "tree.data.substack.substack_article_pipeline.init_mongodb",
        return_value=mongo_client,
    )
    mocker.patch(
        "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
        return_value=mongo_client,
    )


def _mock_rss_source(mocker) -> None:
    mocker.patch(
        "tree.data.substack.substack_rss.httpx.AsyncClient",
        return_value=_make_mock_rss_client(mocker),
    )
    mocker.patch(
        "tree.data.substack.substack_rss.feedparser.parse",
        return_value=_make_parsed_feed(FAKE_RSS_ENTRIES),
    )


def _mock_article_source(mocker) -> None:
    mocker.patch(
        "tree.data.substack.substack_article.httpx.AsyncClient",
        return_value=_make_mock_article_client(mocker),
    )


def _mock_arxiv_source(mocker) -> None:
    def batch_gen(max_samples, batch_size, offset=None):
        yield FAKE_ARXIV_ENTRIES

    mocker.patch(
        "tree.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
        side_effect=batch_gen,
    )


class TestDataPipeline:
    @pytest.mark.slow
    async def test_runs_all_three_pipelines(self, mongo_client, mocker) -> None:
        _mock_init_mongodb(mocker, mongo_client)
        _mock_rss_source(mocker)
        _mock_article_source(mocker)
        _mock_arxiv_source(mocker)
        config = _make_full_config(
            mocker,
            substack_feeds=["https://blog.example.com/feed"],
            substack_articles=[
                "https://blog.example.com/p/article-1",
                "https://blog.example.com/p/article-2",
            ],
        )

        with prefect_tags("tests"):
            result = await data_etl_worker(_USER_ID, config.sources.sources)

        substack_docs = [d for d in result if d.source_type == SourceType.SUBSTACK]
        hf_docs = [d for d in result if d.source_type == SourceType.HUGGINGFACE]
        assert len(substack_docs) >= 2
        assert len(hf_docs) == 2
        assert len(result) >= 4

        db_docs = await Document.find_all().to_list()
        assert len(db_docs) >= 4

    @pytest.mark.slow
    async def test_runs_only_rss_and_arxiv_when_no_articles(
        self, mongo_client, mocker
    ) -> None:
        _mock_init_mongodb(mocker, mongo_client)
        _mock_rss_source(mocker)
        _mock_arxiv_source(mocker)
        config = _make_full_config(
            mocker,
            substack_feeds=["https://blog.example.com/feed"],
            substack_articles=[],
        )

        with prefect_tags("tests"):
            result = await data_etl_worker(_USER_ID, config.sources.sources)

        substack_docs = [d for d in result if d.source_type == SourceType.SUBSTACK]
        hf_docs = [d for d in result if d.source_type == SourceType.HUGGINGFACE]
        assert len(substack_docs) == 2
        assert len(hf_docs) == 2

    @pytest.mark.slow
    async def test_runs_only_articles_and_arxiv_when_no_feeds(
        self, mongo_client, mocker
    ) -> None:
        _mock_init_mongodb(mocker, mongo_client)
        _mock_article_source(mocker)
        _mock_arxiv_source(mocker)
        config = _make_full_config(
            mocker,
            substack_feeds=[],
            substack_articles=["https://blog.example.com/p/article-1"],
        )

        with prefect_tags("tests"):
            result = await data_etl_worker(_USER_ID, config.sources.sources)

        substack_docs = [d for d in result if d.source_type == SourceType.SUBSTACK]
        hf_docs = [d for d in result if d.source_type == SourceType.HUGGINGFACE]
        assert len(substack_docs) >= 1
        assert len(hf_docs) == 2

    async def test_runs_only_arxiv_when_no_substack(self, mongo_client, mocker) -> None:
        _mock_init_mongodb(mocker, mongo_client)
        _mock_arxiv_source(mocker)
        config = _make_full_config(
            mocker,
            substack_feeds=[],
            substack_articles=[],
        )

        with prefect_tags("tests"):
            result = await data_etl_worker(_USER_ID, config.sources.sources)

        assert all(d.source_type == SourceType.HUGGINGFACE for d in result)
        assert len(result) == 2

    async def test_dispatches_all_five_source_variants(
        self, mongo_client, mocker, tmp_path
    ) -> None:
        """Single ``data_etl_worker()`` invocation against a YAML fixture covering
        all five ``SourceEntry`` variants (substack_rss, substack_article,
        huggingface_dataset, explicit web, untyped → web fallback).

        Verifies:
            - The Substack RSS sub-flow is invoked once with the single feed.
            - The Substack article sub-flow is invoked once with the single
              article URL.
            - The arxiv sub-flow is invoked once with the YAML's
              ``max_samples`` / ``fetch_content`` values.
            - The web batch sub-flow (``ingest_web_url_batch``) is invoked ONCE
              with BOTH web URLs as a single list (the explicit ``web`` entry plus
              the untyped Reddit entry the load-time validator normalizes to
              ``WebSource``) — web is now the last batched variant (#075), not a
              per-URL ``ingest_url`` dispatch.
        """

        # --- YAML fixture with all 5 variants ---
        config_yaml = tmp_path / "all_variants.yaml"
        config_yaml.write_text(
            """
sources:
  - uri: https://blog.example.com/feed
    type: substack_rss
  - uri: https://blog.example.com/p/test-post
    type: substack_article
  - uri: librarian-bots/arxiv-metadata-snapshot
    type: huggingface_dataset
    max_samples: 2
    fetch_content: false
  - uri: https://www.anthropic.com/engineering/some-page
    type: web
  - uri: https://www.reddit.com/r/AI_Agents/comments/example
"""
        )

        loaded = load_app_config(config_yaml)
        # Sanity: the load-time validator normalized the untyped Reddit
        # entry into a WebSource, leaving us with exactly 5 entries.
        from tree.config.app_config import (
            HuggingFaceDatasetSource as _Hf,
            SubstackArticleSource as _SubArt,
            SubstackRssSource as _SubRss,
            WebSource as _Web,
        )

        types = [type(s) for s in loaded.sources.sources]
        assert types == [_SubRss, _SubArt, _Hf, _Web, _Web], types
        # The reddit entry must be a WebSource (untyped → web fallback).
        reddit_entry = loaded.sources.sources[4]
        assert isinstance(reddit_entry, _Web)
        assert "reddit.com" in reddit_entry.uri

        # --- Mock the four sub-flows the unified pipeline dispatches to ---

        rss_doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://blog.example.com/p/test-post-from-rss",
            user_id=PydanticObjectId(),
            title="RSS-fetched post",
            content="RSS content",
        )
        article_doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://blog.example.com/p/test-post",
            user_id=PydanticObjectId(),
            title="Article post",
            content="Article content",
        )
        arxiv_doc = Document(
            source_type=SourceType.HUGGINGFACE,
            source_uri="arxiv://2401.00001",
            user_id=PydanticObjectId(),
            title="Arxiv Paper",
            content="Arxiv abstract",
        )
        web_doc_anthropic = Document(
            source_type=SourceType.WEB,
            source_uri="https://www.anthropic.com/engineering/some-page",
            user_id=PydanticObjectId(),
            title="Anthropic page",
            content="Anthropic content",
        )
        web_doc_reddit = Document(
            source_type=SourceType.WEB,
            source_uri="https://www.reddit.com/r/AI_Agents/comments/example",
            user_id=PydanticObjectId(),
            title="Reddit thread",
            content="Reddit content",
        )

        rss_mock = mocker.patch(
            "tree.data.pipeline.ingest_substack_rss_feed_batch",
            new=AsyncMock(return_value=[rss_doc]),
        )
        article_mock = mocker.patch(
            "tree.data.pipeline.ingest_substack_article_batch",
            new=AsyncMock(return_value=[article_doc]),
        )
        arxiv_mock = mocker.patch(
            "tree.data.pipeline.ingest_arxiv_dataset",
            new=AsyncMock(return_value=[arxiv_doc]),
        )

        # Web is the last batched variant (#075): both web URLs are handed to a
        # SINGLE ``ingest_web_url_batch`` call, which returns the ingested docs
        # (the batch flow owns ``None``-filtering, so the worker just extends).
        async def _fake_ingest_web_url_batch(
            urls: list[str], user_id: PydanticObjectId
        ) -> list[Document]:
            docs: list[Document] = []
            for url in urls:
                if "anthropic.com" in url:
                    docs.append(web_doc_anthropic)
                elif "reddit.com" in url:
                    docs.append(web_doc_reddit)
                else:
                    raise AssertionError(
                        f"Unexpected URL routed to ingest_web_url_batch: {url}"
                    )
            return docs

        web_batch_mock = mocker.patch(
            "tree.data.pipeline.ingest_web_url_batch",
            new_callable=AsyncMock,
            side_effect=_fake_ingest_web_url_batch,
        )

        # Skip the real Mongo init.
        mocker.patch(
            "tree.data.pipeline.init_mongodb", new=AsyncMock(return_value=mongo_client)
        )

        # --- Run the worker against the loaded fixture's sources ---
        # #068 moved the per-variant dispatch into ``data-etl-worker``, which takes
        # its sources as an argument rather than reading ``app_config``.
        with prefect_tags("tests"):
            result = await data_etl_worker(_USER_ID, loaded.sources.sources)

        # --- Assert each sub-flow was dispatched exactly as expected ---

        rss_mock.assert_awaited_once_with(["https://blog.example.com/feed"], _USER_ID)
        article_mock.assert_awaited_once_with(
            ["https://blog.example.com/p/test-post"], _USER_ID
        )
        arxiv_mock.assert_awaited_once_with(
            user_id=_USER_ID, max_samples=2, fetch_content=False, offset=None
        )
        # Both web URLs (explicit + untyped→web) batched into ONE call as a single
        # list, in configured order — web is the last batched variant (#075).
        web_batch_mock.assert_awaited_once_with(
            [
                "https://www.anthropic.com/engineering/some-page",
                "https://www.reddit.com/r/AI_Agents/comments/example",
            ],
            _USER_ID,
        )

        # --- Aggregated result holds one doc per dispatched call ---
        assert len(result) == 5
        source_types = sorted(d.source_type.value for d in result)
        assert source_types == sorted(
            [
                SourceType.SUBSTACK.value,
                SourceType.SUBSTACK.value,
                SourceType.HUGGINGFACE.value,
                SourceType.WEB.value,
                SourceType.WEB.value,
            ]
        )
