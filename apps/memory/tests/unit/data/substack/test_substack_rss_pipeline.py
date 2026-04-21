from unittest.mock import AsyncMock

import pytest

from tree.data.substack.substack_rss_pipeline import (
    extract_document_task,
    fetch_feed_task,
    ingest_substack_rss_feed,
    ingest_substack_rss_feed_batch,
    load_document_task,
)
from tree.entities.documents import Document, SourceType


def _make_raw_entry(title: str = "Test Post") -> dict:
    return {
        "title": title,
        "link": "https://example.substack.com/p/test-post",
        "published": "Mon, 01 Jan 2024 00:00:00 GMT",
        "summary": "A summary",
        "author": "Author Name",
    }


def _make_doc(title: str = "Test Post") -> Document:
    return Document(
        source_type=SourceType.SUBSTACK,
        source_uri="https://example.substack.com/p/test-post",
        title=title,
        summary="A summary",
        content="",
        authors=["Author Name"],
    )


class TestFetchFeedTask:
    @pytest.mark.asyncio
    async def test_wraps_fetch_feed(self, mocker) -> None:
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.fetch_feed",
            new_callable=AsyncMock,
            return_value=[_make_raw_entry()],
        )

        result = await fetch_feed_task.fn("https://example.substack.com/feed")

        assert len(result) == 1
        assert result[0]["title"] == "Test Post"

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_empty_feed(self, mocker) -> None:
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.fetch_feed",
            new_callable=AsyncMock,
            return_value=[],
        )

        result = await fetch_feed_task.fn("https://example.substack.com/feed")

        assert result == []


class TestExtractDocumentTask:
    @pytest.mark.asyncio
    async def test_wraps_extract_document(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.extract_document",
            return_value=doc,
        )

        result = await extract_document_task.fn(_make_raw_entry())

        assert result.title == "Test Post"
        assert result.source_type == SourceType.SUBSTACK


class TestLoadDocumentTask:
    @pytest.mark.asyncio
    async def test_wraps_load_document(self, mocker) -> None:
        doc = _make_doc()
        raw = _make_raw_entry()
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.load_document",
            new_callable=AsyncMock,
            return_value=doc,
        )

        result = await load_document_task.fn(doc, raw)

        assert result == doc

    @pytest.mark.asyncio
    async def test_returns_none_for_duplicate(self, mocker) -> None:
        doc = _make_doc()
        raw = _make_raw_entry()
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.load_document",
            new_callable=AsyncMock,
            return_value=None,
        )

        result = await load_document_task.fn(doc, raw)

        assert result is None


class TestIngestSubstackRssFeed:
    @pytest.mark.asyncio
    async def test_ingests_documents(self, mocker) -> None:
        raw_entries = [_make_raw_entry("Post 1"), _make_raw_entry("Post 2")]
        doc1 = _make_doc("Post 1")
        doc2 = _make_doc("Post 2")

        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.fetch_feed",
            new_callable=AsyncMock,
            return_value=raw_entries,
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.extract_document",
            side_effect=[doc1, doc2],
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.load_document",
            new_callable=AsyncMock,
            side_effect=[doc1, doc2],
        )

        result = await ingest_substack_rss_feed.fn("https://example.substack.com/feed")

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_filters_out_duplicates(self, mocker) -> None:
        raw_entries = [_make_raw_entry("Post 1"), _make_raw_entry("Post 2")]
        doc1 = _make_doc("Post 1")
        doc2 = _make_doc("Post 2")

        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.fetch_feed",
            new_callable=AsyncMock,
            return_value=raw_entries,
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.extract_document",
            side_effect=[doc1, doc2],
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.load_document",
            new_callable=AsyncMock,
            side_effect=[doc1, None],
        )

        result = await ingest_substack_rss_feed.fn("https://example.substack.com/feed")

        assert len(result) == 1
        assert result[0].title == "Post 1"


class TestIngestSubstackRssFeedBatch:
    @pytest.mark.asyncio
    async def test_processes_multiple_feeds(self, mocker) -> None:
        doc1 = _make_doc("Post 1")
        doc2 = _make_doc("Post 2")

        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.fetch_feed",
            new_callable=AsyncMock,
            side_effect=[[_make_raw_entry("Post 1")], [_make_raw_entry("Post 2")]],
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.extract_document",
            side_effect=[doc1, doc2],
        )
        mocker.patch(
            "tree.data.substack.substack_rss_pipeline.load_document",
            new_callable=AsyncMock,
            side_effect=[doc1, doc2],
        )

        result = await ingest_substack_rss_feed_batch.fn(
            ["https://a.substack.com/feed", "https://b.substack.com/feed"]
        )

        assert len(result) == 2
