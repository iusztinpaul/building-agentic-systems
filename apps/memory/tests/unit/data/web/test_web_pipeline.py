"""Unit tests for tree.data.web.web_pipeline — Prefect tasks and flows."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from tree.data.web.web_pipeline import (
    fetch_and_extract_web_task,
    ingest_web_url,
    ingest_web_url_batch,
    load_web_document_task,
)
from tree.entities.documents import Document, SourceType


def _make_doc(
    *,
    source_uri: str = "https://example.com/x",
    title: str = "Title",
) -> Document:
    return Document(
        source_type=SourceType.WEB,
        source_uri=source_uri,
        title=title,
        summary="summary",
        content="body",
        authors=["Unknown"],
        date=datetime.now(tz=UTC),
    )


class TestTaskAndFlowMetadata:
    def test_fetch_task_retries(self) -> None:
        # Mirrors substack_article_pipeline.fetch_and_extract_task
        assert fetch_and_extract_web_task.retries == 2
        assert fetch_and_extract_web_task.retry_delay_seconds == 5
        assert fetch_and_extract_web_task.name == "fetch-and-extract-web"

    def test_load_task_retries(self) -> None:
        assert load_web_document_task.retries == 1
        assert load_web_document_task.retry_delay_seconds == 2
        assert load_web_document_task.name == "load-web-document"

    def test_flow_names(self) -> None:
        assert ingest_web_url.name == "ingest-web-url-etl"
        assert ingest_web_url_batch.name == "ingest-web-url-batch-etl"


class TestFetchAndExtractWebTask:
    async def test_wraps_fetch_and_extract_web(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch(
            "tree.data.web.web_pipeline.fetch_and_extract_web",
            new_callable=AsyncMock,
            return_value=doc,
        )

        result = await fetch_and_extract_web_task.fn("https://example.com/x")

        assert result is doc


class TestLoadWebDocumentTask:
    async def test_wraps_load_web_document(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch(
            "tree.data.web.web_pipeline.load_web_document",
            new_callable=AsyncMock,
            return_value=doc,
        )

        result = await load_web_document_task.fn(doc)

        assert result is doc

    async def test_returns_none_for_duplicate(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch(
            "tree.data.web.web_pipeline.load_web_document",
            new_callable=AsyncMock,
            return_value=None,
        )

        result = await load_web_document_task.fn(doc)

        assert result is None


class TestIngestWebUrl:
    async def test_returns_doc_when_persisted(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch(
            "tree.data.web.web_pipeline.fetch_and_extract_web",
            new_callable=AsyncMock,
            return_value=doc,
        )
        mocker.patch(
            "tree.data.web.web_pipeline.load_web_document",
            new_callable=AsyncMock,
            return_value=doc,
        )

        result = await ingest_web_url.fn("https://example.com/x")

        assert result is doc

    async def test_returns_none_for_duplicate(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch(
            "tree.data.web.web_pipeline.fetch_and_extract_web",
            new_callable=AsyncMock,
            return_value=doc,
        )
        mocker.patch(
            "tree.data.web.web_pipeline.load_web_document",
            new_callable=AsyncMock,
            return_value=None,
        )

        result = await ingest_web_url.fn("https://example.com/x")

        assert result is None


class TestIngestWebUrlBatch:
    async def test_initialises_mongodb_once(self, mocker) -> None:
        doc1 = _make_doc(source_uri="https://example.com/a", title="A")
        doc2 = _make_doc(source_uri="https://example.com/b", title="B")

        mock_init = mocker.patch(
            "tree.data.web.web_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "tree.data.web.web_pipeline.fetch_and_extract_web",
            new_callable=AsyncMock,
            side_effect=[doc1, doc2],
        )
        mocker.patch(
            "tree.data.web.web_pipeline.load_web_document",
            new_callable=AsyncMock,
            side_effect=[doc1, doc2],
        )

        result = await ingest_web_url_batch.fn(
            ["https://example.com/a", "https://example.com/b"]
        )

        mock_init.assert_awaited_once()
        assert len(result) == 2

    async def test_filters_out_duplicates(self, mocker) -> None:
        doc1 = _make_doc(source_uri="https://example.com/a", title="A")
        doc2 = _make_doc(source_uri="https://example.com/b", title="B")

        mocker.patch(
            "tree.data.web.web_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "tree.data.web.web_pipeline.fetch_and_extract_web",
            new_callable=AsyncMock,
            side_effect=[doc1, doc2],
        )
        mocker.patch(
            "tree.data.web.web_pipeline.load_web_document",
            new_callable=AsyncMock,
            side_effect=[doc1, None],
        )

        result = await ingest_web_url_batch.fn(
            ["https://example.com/a", "https://example.com/b"]
        )

        assert len(result) == 1
        assert result[0].title == "A"

    async def test_empty_url_list(self, mocker) -> None:
        mock_init = mocker.patch(
            "tree.data.web.web_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )

        result = await ingest_web_url_batch.fn([])

        mock_init.assert_awaited_once()
        assert result == []
