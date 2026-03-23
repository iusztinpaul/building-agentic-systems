import asyncio
from unittest.mock import AsyncMock

import pytest

from twin.data.huggingface.arxiv_dataset_pipeline import (
    _process_document,
    extract_document,
    fetch_paper_content,
    ingest_arxiv_dataset,
    load_document,
)
from twin.entities.documents import Document, SourceType


def _make_doc(arxiv_id: str = "2103.00001") -> Document:
    return Document(
        source_type=SourceType.HUGGINGFACE,
        source_uri=f"https://arxiv.org/abs/{arxiv_id}",
        title="Test Paper",
        summary="Abstract",
        content="",
        authors=["Author"],
    )


class TestExtractDocumentTask:
    def test_wraps_extract_document(self) -> None:
        raw = {
            "id": "2103.12345",
            "authors": "Jane Doe",
            "title": "Test",
            "abstract": "Abstract",
            "update_date": "2021-03-24",
        }

        result = extract_document.fn(raw)

        assert result.source_uri == "https://arxiv.org/abs/2103.12345"
        assert result.title == "Test"


class TestFetchPaperContentTask:
    @pytest.mark.asyncio
    async def test_sets_content_on_doc(self, mocker) -> None:
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline._fetch_paper_content",
            new_callable=AsyncMock,
            return_value="Full text",
        )
        doc = _make_doc()

        result = await fetch_paper_content.fn(doc)

        assert result.content == "Full text"

    @pytest.mark.asyncio
    async def test_leaves_content_empty_when_not_available(self, mocker) -> None:
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline._fetch_paper_content",
            new_callable=AsyncMock,
            return_value="",
        )
        doc = _make_doc()

        result = await fetch_paper_content.fn(doc)

        assert result.content == ""


class TestLoadDocumentTask:
    @pytest.mark.asyncio
    async def test_wraps_load_document(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline._load_document",
            new_callable=AsyncMock,
            return_value=doc,
        )

        result = await load_document.fn(doc)

        assert result == doc


class TestProcessDocument:
    @pytest.mark.asyncio
    async def test_with_fetch_content(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline._fetch_paper_content",
            new_callable=AsyncMock,
            return_value="Paper text",
        )
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline._load_document",
            new_callable=AsyncMock,
            return_value=doc,
        )
        semaphore = asyncio.Semaphore(5)

        result = await _process_document(
            doc, do_fetch_content=True, semaphore=semaphore
        )

        assert result is not None
        assert result.content == "Paper text"

    @pytest.mark.asyncio
    async def test_without_fetch_content(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline._load_document",
            new_callable=AsyncMock,
            return_value=doc,
        )
        semaphore = asyncio.Semaphore(5)

        result = await _process_document(
            doc, do_fetch_content=False, semaphore=semaphore
        )

        assert result == doc

    @pytest.mark.asyncio
    async def test_returns_none_for_duplicate(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline._load_document",
            new_callable=AsyncMock,
            return_value=None,
        )
        semaphore = asyncio.Semaphore(5)

        result = await _process_document(
            doc, do_fetch_content=False, semaphore=semaphore
        )

        assert result is None


class TestIngestArxivDataset:
    @pytest.mark.asyncio
    async def test_processes_batches_in_parallel(self, mocker) -> None:
        entries = [
            {
                "id": f"2103.{i:05d}",
                "authors": "A",
                "title": "T",
                "abstract": "Ab",
                "update_date": "2021-01-01",
            }
            for i in range(4)
        ]

        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
            return_value=iter([entries[:2], entries[2:]]),
        )
        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )

        call_count = 0

        async def mock_load(doc: Document) -> Document:
            nonlocal call_count
            call_count += 1
            return doc

        mocker.patch(
            "twin.data.huggingface.arxiv_dataset_pipeline._load_document",
            side_effect=mock_load,
        )

        result = await ingest_arxiv_dataset.fn(max_samples=4, fetch_content=False)

        assert len(result) == 4
        assert call_count == 4
