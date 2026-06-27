import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.config.app_config import (
    HuggingFaceDatasetSource,
    SourceEntry,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
)
from tree.config.sources import load_sources
from tree.data.offline_pipeline import _PLATFORM_PIPELINES, data_etl_worker
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


def _make_sources(
    mocker,
    *,
    substack_feeds: list[str] | None = None,
    substack_articles: list[str] | None = None,
    arxiv_max_samples: int = 2,
) -> list[SourceEntry]:
    """Build a flat list of typed source entries to hand the worker directly.

    The ``data-etl-worker`` takes its sources as an argument (#068 moved the
    per-variant dispatch into the worker; ADR-003 removed ``AppConfig.sources``),
    so the test passes this list straight in. The arxiv connector reads its
    defaults from the shared source loader (``default_configured_sources``), so
    that is patched to the same list too.
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

    # The arxiv leaf reads the shared source loader for its defaults.
    mocker.patch(
        "tree.data.huggingface.arxiv_dataset_pipeline.default_configured_sources",
        return_value=sources,
    )
    return sources


def _mock_init_mongodb(mocker, mongo_client) -> None:
    for module in (
        "tree.data.offline_pipeline",
        "tree.data.substack.substack_pipeline_batch",
        "tree.data.youtube.youtube_pipeline_batch",
        "tree.data.web.web_pipeline",
        "tree.data.huggingface.arxiv_dataset_pipeline",
    ):
        mocker.patch(f"{module}.init_mongodb", return_value=mongo_client)


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
        sources = _make_sources(
            mocker,
            substack_feeds=["https://blog.example.com/feed"],
            substack_articles=[
                "https://blog.example.com/p/article-1",
                "https://blog.example.com/p/article-2",
            ],
        )

        with prefect_tags("tests"):
            result = await data_etl_worker(_USER_ID, sources)

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
        sources = _make_sources(
            mocker,
            substack_feeds=["https://blog.example.com/feed"],
            substack_articles=[],
        )

        with prefect_tags("tests"):
            result = await data_etl_worker(_USER_ID, sources)

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
        sources = _make_sources(
            mocker,
            substack_feeds=[],
            substack_articles=["https://blog.example.com/p/article-1"],
        )

        with prefect_tags("tests"):
            result = await data_etl_worker(_USER_ID, sources)

        substack_docs = [d for d in result if d.source_type == SourceType.SUBSTACK]
        hf_docs = [d for d in result if d.source_type == SourceType.HUGGINGFACE]
        assert len(substack_docs) >= 1
        assert len(hf_docs) == 2

    async def test_runs_only_arxiv_when_no_substack(self, mongo_client, mocker) -> None:
        _mock_init_mongodb(mocker, mongo_client)
        _mock_arxiv_source(mocker)
        sources = _make_sources(
            mocker,
            substack_feeds=[],
            substack_articles=[],
        )

        with prefect_tags("tests"):
            result = await data_etl_worker(_USER_ID, sources)

        assert all(d.source_type == SourceType.HUGGINGFACE for d in result)
        assert len(result) == 2

    async def test_dispatches_all_five_source_variants(
        self, mongo_client, mocker, tmp_path
    ) -> None:
        """Single ``data_etl_worker()`` invocation against a YAML fixture covering
        all five ``SourceEntry`` variants (substack_rss, substack_article,
        huggingface_dataset, explicit web, untyped → web fallback).

        Verifies:
            - The Substack platform pipeline is invoked ONCE with BOTH its entries
              (the RSS feed + the article) together.
            - The arxiv sub-flow is invoked once with the YAML's
              ``max_samples`` / ``fetch_content`` values.
            - The web platform pipeline (``ingest_web_batch``) is invoked ONCE with
              BOTH web entries (the explicit ``web`` entry plus the untyped Reddit
              entry the load-time validator normalizes to ``WebSource``) — web is the
              last platform.
        """

        # --- Source-file fixture (flat top-level list) with all 5 variants ---
        config_yaml = tmp_path / "all_variants.yaml"
        config_yaml.write_text(
            """
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

        # Load through the shared source loader (ADR-003) — the production path
        # for source files; it applies the same load-time type inference.
        entries = load_sources([str(config_yaml)])
        # Sanity: the load-time validator normalized the untyped Reddit
        # entry into a WebSource, leaving us with exactly 5 entries.
        types = [type(s) for s in entries]
        assert types == [
            SubstackRssSource,
            SubstackArticleSource,
            HuggingFaceDatasetSource,
            WebSource,
            WebSource,
        ], types
        # The reddit entry must be a WebSource (untyped → web fallback).
        reddit_entry = entries[4]
        assert isinstance(reddit_entry, WebSource)
        assert "reddit.com" in reddit_entry.uri

        # --- Mock the platform pipelines the worker dispatches to ---

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

        # Substack platform pipeline gets BOTH its kinds (RSS feed + article) in one
        # call and returns their docs; Web (last platform) gets both web URLs (explicit
        # + untyped→web) in ONE call. Swap each platform's batch_fn in the dispatch table.
        substack_mock = AsyncMock(return_value=[rss_doc, article_doc])
        web_mock = AsyncMock(return_value=[web_doc_anthropic, web_doc_reddit])
        by_label = {"Substack": substack_mock, "Web": web_mock}
        mocker.patch(
            "tree.data.offline_pipeline._PLATFORM_PIPELINES",
            [
                dataclasses.replace(p, batch_fn=by_label.get(p.label, p.batch_fn))
                for p in _PLATFORM_PIPELINES
            ],
        )
        arxiv_mock = mocker.patch(
            "tree.data.offline_pipeline.ingest_arxiv_dataset",
            new=AsyncMock(return_value=[arxiv_doc]),
        )

        # Skip the real Mongo init.
        mocker.patch(
            "tree.data.offline_pipeline.init_mongodb",
            new=AsyncMock(return_value=mongo_client),
        )

        # --- Run the worker against the loaded fixture's sources ---
        with prefect_tags("tests"):
            result = await data_etl_worker(_USER_ID, entries)

        # --- Each platform pipeline got its TYPED entries (RSS + single together) ---
        substack_mock.assert_awaited_once_with([entries[0], entries[1]], _USER_ID)
        arxiv_mock.assert_awaited_once_with(
            user_id=_USER_ID, max_samples=2, fetch_content=False, offset=None
        )
        # Both web URLs in ONE ingest_web_batch call, in configured order.
        web_mock.assert_awaited_once_with([entries[3], entries[4]], _USER_ID)

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
