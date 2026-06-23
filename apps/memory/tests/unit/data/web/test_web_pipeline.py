"""Unit tests for ``tree.data.web.web_pipeline`` (#081).

The generic web leaf pipeline SCRAPES each URL via Bright Data, so Extract+Transform
FUSE: one scrape yields the Document. The batch flow runs ``extract_batch`` (network,
``retries=2``, via the pure ``web.fetch_and_extract_web``) → ``load_batch`` (DB Load,
``retries=1``, via the pure ``web.load_web_document``). Per-element failures are isolated
inside each task via ``tree.data.batch.gather_isolated``.

The per-item sub-flow ``ingest_web_url``'s body is demoted to a plain async core
``_ingest_web_url_one``; ``ingest_web_url`` remains a THIN 1-line @flow wrapper used ONLY
by the MCP URL router (``tree.data.ingest``). The BATCH path calls the batch tasks
directly — it MUST NOT invoke the thin wrapper (no per-item sub-flow runs).
"""

from datetime import UTC, datetime

import tree.data.web.web_pipeline as web_pipeline
from beanie import PydanticObjectId

from tree.data.web.web_pipeline import (
    _ingest_web_url_one,
    extract_batch,
    ingest_web_url,
    ingest_web_url_batch,
    load_batch,
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
        user_id=PydanticObjectId(),
        title=title,
        summary="summary",
        content="body",
        authors=["Unknown"],
        date=datetime.now(tz=UTC),
    )


class TestTaskAndFlowMetadata:
    """Retry grain lives on the batch tasks (mirrors substack_article_pipeline)."""

    def test_extract_batch_retries(self) -> None:
        assert extract_batch.retries == 2
        assert extract_batch.retry_delay_seconds == 5
        assert extract_batch.name == "extract-web-batch"

    def test_load_batch_retries(self) -> None:
        assert load_batch.retries == 1
        assert load_batch.retry_delay_seconds == 2
        assert load_batch.name == "load-web-batch"

    def test_thin_flow_name(self) -> None:
        assert ingest_web_url.name == "ingest-web-url-etl"

    def test_batch_flow_name(self) -> None:
        assert ingest_web_url_batch.name == "ingest-web-url-batch-etl"

    def test_per_row_tasks_are_gone(self) -> None:
        assert not hasattr(web_pipeline, "fetch_and_extract_web_task")
        assert not hasattr(web_pipeline, "load_web_document_task")


class TestIngestOne:
    """The plain async core: fetch+extract then load."""

    async def test_is_a_plain_function_not_a_flow_or_task(self) -> None:
        # The core carries NO Prefect decorator (no ``.fn`` / ``.name`` attrs).
        assert not hasattr(_ingest_web_url_one, "fn")

    async def test_fetches_extracts_then_loads(self, mocker) -> None:
        doc = _make_doc()
        fetch_mock = mocker.patch.object(
            web_pipeline,
            "fetch_and_extract_web",
            mocker.AsyncMock(return_value=doc),
        )
        load_mock = mocker.patch.object(
            web_pipeline,
            "load_web_document",
            mocker.AsyncMock(return_value=doc),
        )
        user_id = PydanticObjectId()

        result = await _ingest_web_url_one("https://example.com/x", user_id)

        assert result is doc
        fetch_mock.assert_awaited_once_with("https://example.com/x", user_id)
        load_mock.assert_awaited_once_with(doc)

    async def test_returns_none_for_duplicate(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch.object(
            web_pipeline,
            "fetch_and_extract_web",
            mocker.AsyncMock(return_value=doc),
        )
        mocker.patch.object(
            web_pipeline,
            "load_web_document",
            mocker.AsyncMock(return_value=None),
        )

        result = await _ingest_web_url_one("https://example.com/x", PydanticObjectId())

        assert result is None


class TestThinFlow:
    """The thin MCP-only @flow delegates to the core."""

    async def test_delegates_to_core(self, mocker) -> None:
        doc = _make_doc()
        core_mock = mocker.patch.object(
            web_pipeline,
            "_ingest_web_url_one",
            mocker.AsyncMock(return_value=doc),
        )
        user_id = PydanticObjectId()

        result = await ingest_web_url.fn("https://example.com/x", user_id)

        assert result is doc
        core_mock.assert_awaited_once_with("https://example.com/x", user_id)


class TestExtractBatch:
    """Network Extract+Transform FUSED; per-URL scrape failures isolated."""

    async def test_scrapes_each_url(self, mocker) -> None:
        doc1 = _make_doc(source_uri="https://example.com/a", title="A")
        doc2 = _make_doc(source_uri="https://example.com/b", title="B")
        fetch_mock = mocker.patch.object(
            web_pipeline,
            "fetch_and_extract_web",
            mocker.AsyncMock(side_effect=[doc1, doc2]),
        )

        result = await extract_batch.fn(
            ["https://example.com/a", "https://example.com/b"],
            PydanticObjectId(),
        )

        assert result == [doc1, doc2]
        # ONE awaited gather over the whole URL list: one scrape per URL.
        assert fetch_mock.await_count == 2

    async def test_isolates_one_scrape_failure(self, mocker) -> None:
        doc1 = _make_doc(source_uri="https://example.com/a", title="A")
        doc3 = _make_doc(source_uri="https://example.com/c", title="C")
        mocker.patch.object(
            web_pipeline,
            "fetch_and_extract_web",
            mocker.AsyncMock(side_effect=[doc1, RuntimeError("scrape failed"), doc3]),
        )

        # 1 of 3 URLs raises during fetch — logged + skipped, the others extracted.
        result = await extract_batch.fn(
            [
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
            ],
            PydanticObjectId(),
        )

        assert result == [doc1, doc3]

    async def test_empty_batch_returns_empty(self, mocker) -> None:
        fetch_mock = mocker.patch.object(
            web_pipeline, "fetch_and_extract_web", mocker.AsyncMock()
        )

        result = await extract_batch.fn([], PydanticObjectId())

        assert result == []
        fetch_mock.assert_not_awaited()


class TestLoadBatch:
    """DB Load over a single gather via the pure load_web_document."""

    async def test_returns_persisted_subset_dropping_duplicates(self, mocker) -> None:
        doc_a = _make_doc(source_uri="https://example.com/a", title="A")
        doc_b = _make_doc(source_uri="https://example.com/b", title="B")
        load_mock = mocker.patch.object(
            web_pipeline,
            "load_web_document",
            mocker.AsyncMock(side_effect=[doc_a, None]),
        )

        result = await load_batch.fn([doc_a, doc_b])

        assert result == [doc_a]
        # ONE awaited gather over the doc list: one load per element.
        assert load_mock.await_count == 2
        load_mock.assert_any_await(doc_a)

    async def test_isolates_one_element_failure(self, mocker) -> None:
        doc_a = _make_doc(source_uri="https://example.com/a", title="A")
        doc_b = _make_doc(source_uri="https://example.com/b", title="B")
        mocker.patch.object(
            web_pipeline,
            "load_web_document",
            mocker.AsyncMock(side_effect=[doc_a, RuntimeError("bad load")]),
        )

        result = await load_batch.fn([doc_a, doc_b])

        assert result == [doc_a]

    async def test_empty_batch_returns_empty(self, mocker) -> None:
        load_mock = mocker.patch.object(
            web_pipeline, "load_web_document", mocker.AsyncMock()
        )

        result = await load_batch.fn([])

        assert result == []
        load_mock.assert_not_awaited()


class TestIngestWebUrlBatch:
    """The batch flow runs extract_batch then load_batch — NOT the thin sub-flow."""

    async def test_does_not_call_thin_flow(self, mocker) -> None:
        urls = [f"https://example.com/{i}" for i in range(5)]
        docs = [_make_doc(source_uri=u, title=str(i)) for i, u in enumerate(urls)]
        mocker.patch.object(web_pipeline, "init_mongodb", mocker.AsyncMock())
        mocker.patch.object(
            web_pipeline,
            "fetch_and_extract_web",
            mocker.AsyncMock(side_effect=list(docs)),
        )
        mocker.patch.object(
            web_pipeline,
            "load_web_document",
            mocker.AsyncMock(side_effect=list(docs)),
        )
        thin_spy = mocker.patch.object(
            web_pipeline,
            "ingest_web_url",
            mocker.AsyncMock(),
        )

        result = await ingest_web_url_batch.fn(urls, PydanticObjectId())

        assert len(result) == 5
        # No per-item sub-flow runs: the batch path never calls the thin wrapper.
        thin_spy.assert_not_awaited()

    async def test_runs_extract_and_load_each_once_over_the_url_list(
        self, mocker
    ) -> None:
        urls = [f"https://example.com/{i}" for i in range(5)]
        docs = [_make_doc(source_uri=u, title=str(i)) for i, u in enumerate(urls)]
        mocker.patch.object(web_pipeline, "init_mongodb", mocker.AsyncMock())
        extract_spy = mocker.patch.object(
            web_pipeline,
            "extract_batch",
            mocker.AsyncMock(return_value=list(docs)),
        )
        load_spy = mocker.patch.object(
            web_pipeline,
            "load_batch",
            mocker.AsyncMock(return_value=docs),
        )

        await ingest_web_url_batch.fn(urls, PydanticObjectId())

        # extract_batch + load_batch each run ONCE over the whole 5-URL list.
        extract_spy.assert_awaited_once()
        load_spy.assert_awaited_once()

    async def test_initialises_mongodb_once(self, mocker) -> None:
        doc1 = _make_doc(source_uri="https://example.com/a", title="A")
        doc2 = _make_doc(source_uri="https://example.com/b", title="B")

        mock_init = mocker.patch.object(
            web_pipeline, "init_mongodb", mocker.AsyncMock()
        )
        mocker.patch.object(
            web_pipeline,
            "fetch_and_extract_web",
            mocker.AsyncMock(side_effect=[doc1, doc2]),
        )
        mocker.patch.object(
            web_pipeline,
            "load_web_document",
            mocker.AsyncMock(side_effect=[doc1, doc2]),
        )

        result = await ingest_web_url_batch.fn(
            ["https://example.com/a", "https://example.com/b"], PydanticObjectId()
        )

        mock_init.assert_awaited_once()
        assert len(result) == 2

    async def test_filters_out_duplicates(self, mocker) -> None:
        doc1 = _make_doc(source_uri="https://example.com/a", title="A")
        doc2 = _make_doc(source_uri="https://example.com/b", title="B")

        mocker.patch.object(web_pipeline, "init_mongodb", mocker.AsyncMock())
        mocker.patch.object(
            web_pipeline,
            "fetch_and_extract_web",
            mocker.AsyncMock(side_effect=[doc1, doc2]),
        )
        mocker.patch.object(
            web_pipeline,
            "load_web_document",
            mocker.AsyncMock(side_effect=[doc1, None]),
        )

        result = await ingest_web_url_batch.fn(
            ["https://example.com/a", "https://example.com/b"], PydanticObjectId()
        )

        assert len(result) == 1
        assert result[0].title == "A"

    async def test_empty_url_list(self, mocker) -> None:
        mock_init = mocker.patch.object(
            web_pipeline, "init_mongodb", mocker.AsyncMock()
        )

        result = await ingest_web_url_batch.fn([], PydanticObjectId())

        mock_init.assert_awaited_once()
        assert result == []
