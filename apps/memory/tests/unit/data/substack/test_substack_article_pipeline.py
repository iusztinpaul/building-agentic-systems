"""Unit tests for ``tree.data.substack.substack_article_pipeline`` (#079).

The substack ARTICLE leaf pipeline SCRAPES each URL, so Extract+Transform FUSE: one
scrape yields the Document. The batch flow runs ``extract_batch`` (network, ``retries=2``,
via the pure ``substack_article.fetch_and_extract``) → ``load_batch`` (DB Load,
``retries=1``, via the SHARED ``substack_article.load_article_document``). Per-element
failures are isolated inside each task via ``tree.data.batch.gather_isolated``.

The per-item sub-flow ``ingest_substack_article``'s body is demoted to a plain async core
``_ingest_substack_article_one``; ``ingest_substack_article`` remains a THIN 1-line @flow
wrapper used ONLY by the MCP URL router (``tree.data.ingest``). The BATCH path calls the
batch tasks directly — it MUST NOT invoke the thin wrapper (no per-item sub-flow runs).
"""

import tree.data.substack.substack_article_pipeline as article_pipeline
from beanie import PydanticObjectId

from tree.data.substack.substack_article_pipeline import (
    _ingest_substack_article_one,
    extract_batch,
    ingest_substack_article,
    ingest_substack_article_batch,
    load_batch,
)
from tree.entities.documents import Document, SourceType


def _make_doc(url: str = "https://example.substack.com/p/article") -> Document:
    return Document(
        source_type=SourceType.SUBSTACK,
        source_uri=url,
        user_id=PydanticObjectId(),
        title="Article",
        summary="Summary",
        content="Body",
        authors=["Author"],
    )


class TestTaskAndFlowMetadata:
    """Retry grain lives on the batch tasks (mirrors test_web_pipeline)."""

    def test_extract_batch_retries(self) -> None:
        assert extract_batch.retries == 2
        assert extract_batch.retry_delay_seconds == 5
        assert extract_batch.name == "extract-substack-article-batch"

    def test_load_batch_retries(self) -> None:
        assert load_batch.retries == 1
        assert load_batch.retry_delay_seconds == 2
        assert load_batch.name == "load-substack-article-batch"

    def test_thin_flow_name(self) -> None:
        assert ingest_substack_article.name == "ingest-substack-article-etl"

    def test_batch_flow_name(self) -> None:
        assert ingest_substack_article_batch.name == "ingest-substack-article-batch-etl"

    def test_per_row_tasks_are_gone(self) -> None:
        assert not hasattr(article_pipeline, "fetch_and_extract_task")
        assert not hasattr(article_pipeline, "load_article_document_task")


class TestIngestOne:
    """The plain async core: fetch+extract then shared load."""

    async def test_is_a_plain_function_not_a_flow_or_task(self) -> None:
        # The core carries NO Prefect decorator (no ``.fn`` / ``.name`` attrs).
        assert not hasattr(_ingest_substack_article_one, "fn")

    async def test_fetches_extracts_then_loads(self, mocker) -> None:
        doc = _make_doc()
        fetch_mock = mocker.patch.object(
            article_pipeline,
            "fetch_and_extract",
            mocker.AsyncMock(return_value=(doc, "<div>body</div>")),
        )
        load_mock = mocker.patch.object(
            article_pipeline,
            "load_article_document",
            mocker.AsyncMock(return_value=doc),
        )

        result = await _ingest_substack_article_one(
            "https://example.substack.com/p/article", PydanticObjectId()
        )

        assert result is doc
        fetch_mock.assert_awaited_once()
        # Load delegates to the SHARED load_article_document with the body HTML.
        load_mock.assert_awaited_once_with(doc, "<div>body</div>")

    async def test_returns_none_for_duplicate(self, mocker) -> None:
        doc = _make_doc()
        mocker.patch.object(
            article_pipeline,
            "fetch_and_extract",
            mocker.AsyncMock(return_value=(doc, "<div>body</div>")),
        )
        mocker.patch.object(
            article_pipeline,
            "load_article_document",
            mocker.AsyncMock(return_value=None),
        )

        result = await _ingest_substack_article_one(
            "https://example.substack.com/p/article", PydanticObjectId()
        )

        assert result is None


class TestThinFlow:
    """The thin MCP-only @flow delegates to the core."""

    async def test_delegates_to_core(self, mocker) -> None:
        doc = _make_doc()
        core_mock = mocker.patch.object(
            article_pipeline,
            "_ingest_substack_article_one",
            mocker.AsyncMock(return_value=doc),
        )
        user_id = PydanticObjectId()

        result = await ingest_substack_article.fn(
            "https://example.substack.com/p/article", user_id
        )

        assert result is doc
        core_mock.assert_awaited_once_with(
            "https://example.substack.com/p/article", user_id
        )


class TestExtractBatch:
    """Network Extract+Transform FUSED; per-URL scrape failures isolated."""

    async def test_scrapes_each_url(self, mocker) -> None:
        doc1 = _make_doc("https://a.substack.com/p/1")
        doc2 = _make_doc("https://a.substack.com/p/2")
        fetch_mock = mocker.patch.object(
            article_pipeline,
            "fetch_and_extract",
            mocker.AsyncMock(side_effect=[(doc1, "html1"), (doc2, "html2")]),
        )

        result = await extract_batch.fn(
            ["https://a.substack.com/p/1", "https://a.substack.com/p/2"],
            PydanticObjectId(),
        )

        assert result == [(doc1, "html1"), (doc2, "html2")]
        # ONE awaited gather over the whole URL list: one scrape per URL.
        assert fetch_mock.await_count == 2

    async def test_isolates_one_scrape_failure(self, mocker) -> None:
        doc1 = _make_doc("https://a.substack.com/p/1")
        mocker.patch.object(
            article_pipeline,
            "fetch_and_extract",
            mocker.AsyncMock(
                side_effect=[(doc1, "html1"), RuntimeError("scrape failed")]
            ),
        )

        # 1 of 2 URLs raises during fetch — logged + skipped, the other extracted.
        result = await extract_batch.fn(
            ["https://a.substack.com/p/1", "https://a.substack.com/p/2"],
            PydanticObjectId(),
        )

        assert result == [(doc1, "html1")]

    async def test_empty_batch_returns_empty(self, mocker) -> None:
        fetch_mock = mocker.patch.object(
            article_pipeline, "fetch_and_extract", mocker.AsyncMock()
        )

        result = await extract_batch.fn([], PydanticObjectId())

        assert result == []
        fetch_mock.assert_not_awaited()


class TestLoadBatch:
    """DB Load over a single gather via the SHARED load_article_document."""

    async def test_returns_persisted_subset_dropping_duplicates(self, mocker) -> None:
        doc_a = _make_doc("https://a.substack.com/p/1")
        doc_b = _make_doc("https://a.substack.com/p/2")
        load_mock = mocker.patch.object(
            article_pipeline,
            "load_article_document",
            mocker.AsyncMock(side_effect=[doc_a, None]),
        )

        result = await load_batch.fn([(doc_a, "html_a"), (doc_b, "html_b")])

        assert result == [doc_a]
        # ONE awaited gather over the URL list: one load per element.
        assert load_mock.await_count == 2
        # Load delegates to the SHARED load_article_document with the body HTML.
        load_mock.assert_any_await(doc_a, "html_a")

    async def test_isolates_one_element_failure(self, mocker) -> None:
        doc_a = _make_doc("https://a.substack.com/p/1")
        doc_b = _make_doc("https://a.substack.com/p/2")
        mocker.patch.object(
            article_pipeline,
            "load_article_document",
            mocker.AsyncMock(side_effect=[doc_a, RuntimeError("bad load")]),
        )

        result = await load_batch.fn([(doc_a, "html_a"), (doc_b, "html_b")])

        assert result == [doc_a]

    async def test_empty_batch_returns_empty(self, mocker) -> None:
        load_mock = mocker.patch.object(
            article_pipeline, "load_article_document", mocker.AsyncMock()
        )

        result = await load_batch.fn([])

        assert result == []
        load_mock.assert_not_awaited()


class TestIngestSubstackArticleBatch:
    """The batch flow runs extract_batch then load_batch — NOT the thin sub-flow."""

    async def test_does_not_call_thin_flow(self, mocker) -> None:
        urls = [f"https://a.substack.com/p/{i}" for i in range(10)]
        docs = [_make_doc(u) for u in urls]
        mocker.patch.object(article_pipeline, "init_mongodb", mocker.AsyncMock())
        mocker.patch.object(
            article_pipeline,
            "fetch_and_extract",
            mocker.AsyncMock(side_effect=[(d, "html") for d in docs]),
        )
        mocker.patch.object(
            article_pipeline,
            "load_article_document",
            mocker.AsyncMock(side_effect=docs),
        )
        thin_spy = mocker.patch.object(
            article_pipeline,
            "ingest_substack_article",
            mocker.AsyncMock(),
        )

        result = await ingest_substack_article_batch.fn(urls, PydanticObjectId())

        assert len(result) == 10
        # No per-item sub-flow runs: the batch path never calls the thin wrapper.
        thin_spy.assert_not_awaited()

    async def test_runs_extract_and_load_each_once_over_the_url_list(
        self, mocker
    ) -> None:
        urls = [f"https://a.substack.com/p/{i}" for i in range(10)]
        docs = [_make_doc(u) for u in urls]
        mocker.patch.object(article_pipeline, "init_mongodb", mocker.AsyncMock())
        extract_spy = mocker.patch.object(
            article_pipeline,
            "extract_batch",
            mocker.AsyncMock(return_value=[(d, "html") for d in docs]),
        )
        load_spy = mocker.patch.object(
            article_pipeline,
            "load_batch",
            mocker.AsyncMock(return_value=docs),
        )

        await ingest_substack_article_batch.fn(urls, PydanticObjectId())

        # extract_batch + load_batch each run ONCE over the whole 10-URL list.
        extract_spy.assert_awaited_once()
        load_spy.assert_awaited_once()
