"""Integration tests for the Bright Data Web Unlocker fallback pipeline.

These tests hit the real Bright Data Web Unlocker API and the real local
MongoDB. They are gated on ``BRIGHTDATA_API_KEY`` and
``BRIGHTDATA_UNLOCKER_ZONE`` being present so CI runs without those
secrets stay green.

Each test cleans up the document(s) it creates by ``source_uri`` in a
``try/finally`` so re-running the suite back-to-back stays idempotent.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from prefect import tags as prefect_tags

from tree.data.core.ingest import ingest_url
from tree.data.pipeline import ingest_all_data
from tree.data.web.web_pipeline import ingest_web_url, ingest_web_url_batch
from tree.entities.documents import Document, SourceType

_BRIGHTDATA_REASON = (
    "Bright Data credentials not configured (or set to .env.example placeholder)"
)
_PLACEHOLDER_VALUES = {
    "",
    "your-brightdata-api-key",
    "your-brightdata-unlocker-zone",
}


def _is_real(value: str | None) -> bool:
    return bool(value) and value not in _PLACEHOLDER_VALUES


pytestmark = pytest.mark.skipif(
    not (
        _is_real(os.environ.get("BRIGHTDATA_API_KEY"))
        and _is_real(os.environ.get("BRIGHTDATA_UNLOCKER_ZONE"))
    ),
    reason=_BRIGHTDATA_REASON,
)


_EXAMPLE_URL = "https://example.com"
_EXAMPLE_ORG_URL = "https://example.org"
_EXAMPLE_NET_URL = "https://example.net"
_FALLBACK_URL = "https://martinfowler.com/bliki/CQRS.html"
# A long-stable Substack article that is also present in
# ``configs/default.yaml`` under ``sources.substack_articles``.
_SUBSTACK_URL = "https://www.decodingai.com/p/ai-agents-foundations-course"


async def _delete_by_source_uri(source_uri: str) -> None:
    """Delete every document with the given ``source_uri`` (cleanup helper)."""

    await Document.find(Document.source_uri == source_uri).delete()


class TestIngestWebUrlFlow:
    async def test_ingest_web_url_persists_document(self, mongo_client) -> None:
        try:
            with prefect_tags("tests"):
                doc = await ingest_web_url(_EXAMPLE_URL)

            assert doc is not None
            assert doc.source_type == SourceType.WEB
            assert doc.source_uri == _EXAMPLE_URL
            assert doc.content
            assert doc.content.strip()

            db_docs = await Document.find(Document.source_uri == _EXAMPLE_URL).to_list()
            assert len(db_docs) == 1
            assert db_docs[0].source_type == SourceType.WEB
            assert db_docs[0].content
        finally:
            await _delete_by_source_uri(_EXAMPLE_URL)

    async def test_ingest_web_url_idempotent(self, mongo_client) -> None:
        try:
            with prefect_tags("tests"):
                first = await ingest_web_url(_EXAMPLE_URL)
            assert first is not None

            with prefect_tags("tests"):
                second = await ingest_web_url(_EXAMPLE_URL)
            assert second is None

            db_docs = await Document.find(Document.source_uri == _EXAMPLE_URL).to_list()
            assert len(db_docs) == 1
        finally:
            await _delete_by_source_uri(_EXAMPLE_URL)


class TestIngestWebUrlBatchFlow:
    async def test_ingest_web_url_batch(self, mongo_client, mocker) -> None:
        mocker.patch(
            "tree.data.web.web_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        urls = [_EXAMPLE_ORG_URL, _EXAMPLE_NET_URL]
        try:
            with prefect_tags("tests"):
                first_run = await ingest_web_url_batch(urls)
            assert len(first_run) == 2
            for doc in first_run:
                assert doc.source_type == SourceType.WEB
                assert doc.source_uri in urls

            with prefect_tags("tests"):
                second_run = await ingest_web_url_batch(urls)
            assert len(second_run) == 0

            db_docs = await Document.find({"source_uri": {"$in": urls}}).to_list()
            assert len(db_docs) == 2
        finally:
            for url in urls:
                await _delete_by_source_uri(url)


class TestDispatcherFallback:
    async def test_dispatcher_falls_through_to_web(self, mongo_client) -> None:
        try:
            with prefect_tags("tests"):
                doc = await ingest_url(_FALLBACK_URL)

            assert doc is not None
            assert doc.source_type == SourceType.WEB
            assert doc.source_uri == _FALLBACK_URL
            assert doc.content

            db_docs = await Document.find(
                Document.source_uri == _FALLBACK_URL
            ).to_list()
            assert len(db_docs) == 1
            assert db_docs[0].source_type == SourceType.WEB
        finally:
            await _delete_by_source_uri(_FALLBACK_URL)

    async def test_dispatcher_routes_substack_first(self, mongo_client) -> None:
        try:
            with prefect_tags("tests"):
                doc = await ingest_url(_SUBSTACK_URL)

            assert doc is not None
            # The regression guard: a substack URL must NOT fall through to
            # the generic web pipeline even though Bright Data could fetch it.
            assert doc.source_type == SourceType.SUBSTACK
            assert doc.source_uri == _SUBSTACK_URL

            db_docs = await Document.find(
                Document.source_uri == _SUBSTACK_URL
            ).to_list()
            assert len(db_docs) == 1
            assert db_docs[0].source_type == SourceType.SUBSTACK
        finally:
            await _delete_by_source_uri(_SUBSTACK_URL)


class TestIngestAllDataPicksUpUrls:
    async def test_ingest_all_data_picks_up_urls_config(
        self, mongo_client, mocker
    ) -> None:
        mock_config = MagicMock()
        mock_config.sources.substack = []
        mock_config.sources.substack_articles = []
        mock_config.sources.huggingface_arxiv_dataset.max_samples = 0
        mock_config.sources.huggingface_arxiv_dataset.fetch_content = False
        mock_config.sources.huggingface_arxiv_dataset.batch_size = 50
        mock_config.sources.huggingface_arxiv_dataset.concurrency = 10
        mock_config.sources.urls = [_EXAMPLE_URL]
        mocker.patch("tree.data.pipeline.app_config", mock_config)
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.app_config",
            mock_config,
        )
        mocker.patch(
            "tree.data.pipeline.init_mongodb",
            return_value=mongo_client,
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        # Stub the arxiv batch generator so ``ingest_all_data`` doesn't
        # touch the real HuggingFace dataset during this test.
        def _empty_batches(max_samples, batch_size):
            return
            yield  # pragma: no cover - make this a generator function

        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
            side_effect=_empty_batches,
        )

        try:
            with prefect_tags("tests"):
                result = await ingest_all_data()

            web_docs = [d for d in result if d.source_type == SourceType.WEB]
            assert len(web_docs) == 1
            assert web_docs[0].source_uri == _EXAMPLE_URL

            db_doc = await Document.find_one(Document.source_uri == _EXAMPLE_URL)
            assert db_doc is not None
            assert db_doc.source_type == SourceType.WEB
            assert db_doc.content
        finally:
            await _delete_by_source_uri(_EXAMPLE_URL)
