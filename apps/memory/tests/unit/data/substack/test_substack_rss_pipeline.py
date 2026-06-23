"""Unit tests for ``tree.data.substack.substack_rss_pipeline`` (#079).

The substack RSS leaf pipeline is BATCH-grain and builds Documents from FEED-EMBEDDED
content — it NEVER re-scrapes the articles. Per configured feed the batch flow runs
``fetch_feed`` (Extract, ``retries=2``) → ``transform_batch`` (pure map, ``retries=0``,
via the pure ``substack_rss.extract_document``) → ``load_batch`` (DB Load, ``retries=1``,
via the shared ``substack_rss.load_document``). Per-element load failures are isolated
inside ``load_batch`` via ``tree.data.batch.gather_isolated`` — a bad entry is logged +
skipped and the task returns the successful subset; the per-feed sub-flow is gone, folded
into the batch flow's per-feed loop with per-feed failure isolation preserved.
"""

import tree.data.substack.substack_rss_pipeline as rss_pipeline
from beanie import PydanticObjectId

from tree.data.substack.substack_rss_pipeline import (
    fetch_feed_task,
    ingest_substack_rss_feed_batch,
    load_batch,
    transform_batch,
)
from tree.entities.documents import Document, SourceType


def _make_raw_entry(link: str = "https://example.substack.com/p/test-post") -> dict:
    return {
        "title": "Test Post",
        "link": link,
        "published": "Mon, 01 Jan 2024 00:00:00 GMT",
        "summary": "A summary",
        "author": "Author Name",
        "content": [{"value": "<p>Body</p>"}],
    }


def _make_doc(link: str = "https://example.substack.com/p/test-post") -> Document:
    return Document(
        source_type=SourceType.SUBSTACK,
        source_uri=link,
        user_id=PydanticObjectId(),
        title="Test Post",
        summary="A summary",
        content="Body",
        authors=["Author Name"],
    )


class TestTaskMetadata:
    """Retry grain lives on the batch tasks (mirrors test_web_pipeline)."""

    def test_fetch_feed_task_retries(self) -> None:
        assert fetch_feed_task.retries == 2
        assert fetch_feed_task.retry_delay_seconds == 5
        assert fetch_feed_task.name == "fetch-substack-rss-feed"

    def test_transform_batch_is_pure_map(self) -> None:
        assert transform_batch.retries == 0
        assert transform_batch.name == "transform-substack-rss-batch"

    def test_load_batch_retries(self) -> None:
        assert load_batch.retries == 1
        assert load_batch.retry_delay_seconds == 2
        assert load_batch.name == "load-substack-rss-batch"

    def test_batch_flow_name(self) -> None:
        assert (
            ingest_substack_rss_feed_batch.name == "ingest-substack-rss-feed-batch-etl"
        )


class TestNoArticleReFetch:
    """The RSS path builds from feed-embedded content; it never re-scrapes."""

    def test_does_not_import_fetch_and_extract(self) -> None:
        # The article-scrape entry point must not leak into the RSS pipeline module
        # namespace — proving the RSS path cannot re-fetch articles.
        assert not hasattr(rss_pipeline, "fetch_and_extract")
        assert not hasattr(rss_pipeline, "fetch_article")

    def test_per_feed_sub_flow_is_gone(self) -> None:
        # The inner single-feed ``ingest_substack_rss_feed`` @flow is collapsed into
        # the batch flow's per-feed loop.
        assert not hasattr(rss_pipeline, "ingest_substack_rss_feed")

    def test_per_row_tasks_are_gone(self) -> None:
        assert not hasattr(rss_pipeline, "extract_document_task")
        assert not hasattr(rss_pipeline, "load_document_task")


class TestFetchFeedTask:
    async def test_wraps_fetch_feed(self, mocker) -> None:
        mocker.patch.object(
            rss_pipeline,
            "fetch_feed",
            mocker.AsyncMock(return_value=[_make_raw_entry()]),
        )

        result = await fetch_feed_task.fn("https://example.substack.com/feed")

        assert len(result) == 1
        assert result[0]["title"] == "Test Post"


class TestTransformBatch:
    """Pure map ``list[dict] -> list[Document]`` via the pure ``extract_document``."""

    async def test_maps_entries_to_documents(self, mocker) -> None:
        user_id = PydanticObjectId()
        entries = [
            _make_raw_entry("https://a.substack.com/p/1"),
            _make_raw_entry("https://a.substack.com/p/2"),
        ]
        extract_spy = mocker.patch.object(
            rss_pipeline,
            "extract_document",
            side_effect=[
                _make_doc("https://a.substack.com/p/1"),
                _make_doc("https://a.substack.com/p/2"),
            ],
        )

        result = await transform_batch.fn(entries, user_id)

        assert len(result) == 2
        # Pure map: one extract call per entry, in order, no network.
        assert extract_spy.call_count == 2

    async def test_empty_batch_returns_empty(self) -> None:
        result = await transform_batch.fn([], PydanticObjectId())

        assert result == []


class TestLoadBatch:
    """DB Load over a single gather; dups dropped, per-element failures isolated."""

    async def test_returns_persisted_subset_dropping_duplicates(self, mocker) -> None:
        doc_a = _make_doc("https://a.substack.com/p/1")
        doc_b = _make_doc("https://a.substack.com/p/2")
        entries = [
            _make_raw_entry("https://a.substack.com/p/1"),
            _make_raw_entry("https://a.substack.com/p/2"),
        ]
        load_mock = mocker.patch.object(
            rss_pipeline,
            "load_document",
            mocker.AsyncMock(side_effect=[doc_a, None]),
        )

        result = await load_batch.fn([doc_a, doc_b], entries)

        assert result == [doc_a]
        # ONE awaited gather over the feed's entries: one load call per element.
        assert load_mock.await_count == 2

    async def test_isolates_one_element_failure(self, mocker) -> None:
        doc_a = _make_doc("https://a.substack.com/p/1")
        doc_b = _make_doc("https://a.substack.com/p/2")
        entries = [
            _make_raw_entry("https://a.substack.com/p/1"),
            _make_raw_entry("https://a.substack.com/p/2"),
        ]
        mocker.patch.object(
            rss_pipeline,
            "load_document",
            mocker.AsyncMock(side_effect=[doc_a, RuntimeError("bad entry")]),
        )

        # The raise is caught + isolated; NOT propagated.
        result = await load_batch.fn([doc_a, doc_b], entries)

        assert result == [doc_a]

    async def test_passes_matching_raw_entry_to_load_document(self, mocker) -> None:
        doc = _make_doc("https://a.substack.com/p/1")
        entry = _make_raw_entry("https://a.substack.com/p/1")
        load_mock = mocker.patch.object(
            rss_pipeline,
            "load_document",
            mocker.AsyncMock(return_value=doc),
        )

        await load_batch.fn([doc], [entry])

        # Reference resolution still reads the feed-embedded raw entry (unchanged).
        load_mock.assert_awaited_once_with(doc, entry)

    async def test_empty_batch_returns_empty(self, mocker) -> None:
        load_mock = mocker.patch.object(
            rss_pipeline, "load_document", mocker.AsyncMock()
        )

        result = await load_batch.fn([], [])

        assert result == []
        load_mock.assert_not_awaited()


class TestIngestSubstackRssFeedBatch:
    """The batch flow loops per feed: fetch_feed → transform_batch → load_batch."""

    async def test_one_fetch_per_feed_no_article_scrape(self, mocker) -> None:
        mocker.patch.object(rss_pipeline, "init_mongodb", mocker.AsyncMock())
        fetch_mock = mocker.patch.object(
            rss_pipeline,
            "fetch_feed",
            mocker.AsyncMock(
                side_effect=[
                    [_make_raw_entry("https://a.substack.com/p/1")],
                    [_make_raw_entry("https://b.substack.com/p/1")],
                ]
            ),
        )
        mocker.patch.object(
            rss_pipeline,
            "extract_document",
            side_effect=lambda entry, user_id: _make_doc(entry["link"]),
        )
        mocker.patch.object(
            rss_pipeline,
            "load_document",
            mocker.AsyncMock(side_effect=lambda doc, entry: doc),
        )

        result = await ingest_substack_rss_feed_batch.fn(
            ["https://a.substack.com/feed", "https://b.substack.com/feed"],
            PydanticObjectId(),
        )

        assert len(result) == 2
        # Exactly one feed fetch per feed — no per-article re-scrape.
        assert fetch_mock.await_count == 2

    async def test_calls_load_batch_once_per_feed(self, mocker) -> None:
        mocker.patch.object(rss_pipeline, "init_mongodb", mocker.AsyncMock())
        mocker.patch.object(
            rss_pipeline,
            "fetch_feed",
            mocker.AsyncMock(
                side_effect=[
                    [_make_raw_entry("https://a.substack.com/p/1")],
                    [_make_raw_entry("https://b.substack.com/p/1")],
                ]
            ),
        )
        load_batch_spy = mocker.patch.object(
            rss_pipeline,
            "load_batch",
            mocker.AsyncMock(side_effect=lambda docs, entries: docs),
        )
        mocker.patch.object(
            rss_pipeline,
            "transform_batch",
            mocker.AsyncMock(
                side_effect=lambda entries, user_id: [
                    _make_doc(e["link"]) for e in entries
                ]
            ),
        )

        await ingest_substack_rss_feed_batch.fn(
            ["https://a.substack.com/feed", "https://b.substack.com/feed"],
            PydanticObjectId(),
        )

        # Two feeds ⇒ exactly two load_batch task runs (not one per entry).
        assert load_batch_spy.await_count == 2

    async def test_isolates_one_bad_feed(self, mocker) -> None:
        mocker.patch.object(rss_pipeline, "init_mongodb", mocker.AsyncMock())
        mocker.patch.object(
            rss_pipeline,
            "fetch_feed",
            mocker.AsyncMock(
                side_effect=[
                    RuntimeError("feed down"),
                    [_make_raw_entry("https://b.substack.com/p/1")],
                ]
            ),
        )
        mocker.patch.object(
            rss_pipeline,
            "extract_document",
            side_effect=lambda entry, user_id: _make_doc(entry["link"]),
        )
        mocker.patch.object(
            rss_pipeline,
            "load_document",
            mocker.AsyncMock(side_effect=lambda doc, entry: doc),
        )

        # The bad feed is logged + skipped; the good feed still ingests.
        result = await ingest_substack_rss_feed_batch.fn(
            ["https://a.substack.com/feed", "https://b.substack.com/feed"],
            PydanticObjectId(),
        )

        assert len(result) == 1
        assert result[0].source_uri == "https://b.substack.com/p/1"
