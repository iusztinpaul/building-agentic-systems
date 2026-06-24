"""Unit tests for ``tree.data.substack.substack_pipeline`` — the ONLINE article path.

After the platform unification this module holds only the single-article scrape core
``_ingest_substack_article_one`` and the thin MCP-only @flow ``ingest_substack_article``
(used by the URL router). The batch flow moved to
``substack_pipeline_batch.ingest_substack_batch`` (tested in ``test_substack_pipeline_batch.py``).
"""

import tree.data.substack.substack_pipeline as article_pipeline
from beanie import PydanticObjectId

from tree.data.substack.substack_pipeline import (
    _ingest_substack_article_one,
    ingest_substack_article,
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


class TestFlowMetadata:
    def test_thin_flow_name(self) -> None:
        assert ingest_substack_article.name == "ingest-substack-article-etl"

    def test_per_row_tasks_are_gone(self) -> None:
        assert not hasattr(article_pipeline, "fetch_and_extract_task")
        assert not hasattr(article_pipeline, "load_article_document_task")


class TestIngestOne:
    """The plain async core: fetch+extract then shared load."""

    async def test_is_a_plain_function_not_a_flow_or_task(self) -> None:
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
