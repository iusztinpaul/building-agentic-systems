"""Integration tests for MCP ingestion tools — end-to-end through real MongoDB."""

import json

import pytest

from tree.entities.documents import Document, SourceType
from tree.mcp.tools import ingest_conversation, ingest_file, ingest_url
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM

TEST_DATABASE = "integration_tests_twin"

# Canned LLM response: one person node + one preference edge.
_LLM_EXTRACTION = {
    "nodes": [
        {
            "name": "alice",
            "type": "person",
            "properties": {"aliases": []},
        },
        {
            "name": "likes python",
            "type": "preference",
            "properties": {"content": "Alice prefers Python for data work"},
        },
    ],
    "edges": [
        {
            "source_node_id": "alice",
            "source_type": "person",
            "target_node_id": "likes python",
            "target_type": "preference",
            "type": "has",
            "properties": {},
        },
    ],
}


def _make_fake_llm() -> FakeLLM:
    """FakeLLM that returns canned extraction for any number of chunks."""

    return FakeLLM([_LLM_EXTRACTION] * 50)


# ---------------------------------------------------------------------------
# ingest_conversation
# ---------------------------------------------------------------------------


# NOTE: every test in these MCP ingest classes drives ``tree.mcp.tools.ingest_*``,
# which under the hood runs the memory-extraction pipeline. The pipeline's
# ``add_entity`` step issues a live Atlas ``$vectorSearch`` (no patching of
# ``dedupe_entity`` here, unlike ``test_extraction_pipeline.py``). On CI runners
# mongot's Search Index Management gRPC channel is unreliable and the live
# aggregation hangs until the per-test ``--timeout`` fires (5 min each → ~35 min
# wasted in CI run 25989844295). We tag the whole class as ``requires_mongot``
# so CI excludes them (``-m "not requires_mongot"``) and we still run them
# locally where the full ``docker-compose.yml`` brings mongot up. The trivial
# early-return error tests (``empty_text``, ``file_not_found``,
# ``unsupported_scheme``) sit in the same class for cohesion — they're cheap to
# rerun locally and lose nothing meaningful by being skipped in CI.
@pytest.mark.requires_mongot
class TestIngestConversation:
    @pytest.mark.slow
    async def test_creates_document_and_extracts(
        self, make_mcp_ctx, mongo_client, test_user
    ):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(
            llm=llm, embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        result = await ingest_conversation(
            "Alice told me she loves Python for data science.", ctx
        )

        parsed = json.loads(result)
        assert parsed["status"] == "ingested"
        assert parsed["nodes_extracted"] > 0
        assert parsed["edges_extracted"] > 0

        # Verify Document exists in MongoDB.
        doc = await Document.find_one(
            Document.source_uri == parsed["source_uri"],
        )
        assert doc is not None
        assert doc.source_type == SourceType.CONVERSATION
        # #020: the persisted row carries the boot-pinned user_id —
        # closes the gap surfaced in plan.md 2026-05-16.
        assert doc.user_id == test_user.id

        # Verify KG entries were created and tagged for this tenant only.
        kg_col = mongo_client[TEST_DATABASE]["knowledge_graph"]
        kg_count = await kg_col.count_documents({})
        assert kg_count > 0
        kg_count_other = await kg_col.count_documents(
            {"user_id": {"$ne": test_user.id}}
        )
        assert kg_count_other == 0, (
            "Conversation ingest leaked rows to a different tenant — "
            "user_id propagation is broken."
        )

    @pytest.mark.slow
    async def test_returns_summary_with_counts(self, make_mcp_ctx, test_user):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(
            llm=llm, embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        result = await ingest_conversation("Bob discussed his new project.", ctx)

        parsed = json.loads(result)
        assert "document_id" in parsed
        assert "source_uri" in parsed
        assert "title" in parsed
        assert isinstance(parsed["nodes_extracted"], int)
        assert isinstance(parsed["edges_extracted"], int)

    async def test_empty_text_returns_error(self, make_mcp_ctx):
        ctx = make_mcp_ctx(llm=FakeLLM(), embedding_model=FakeEmbeddingModel())

        result = await ingest_conversation("   ", ctx)

        parsed = json.loads(result)
        assert parsed["error"] == "empty_input"


# ---------------------------------------------------------------------------
# ingest_file
# ---------------------------------------------------------------------------


@pytest.mark.requires_mongot
class TestIngestFile:
    @pytest.mark.slow
    async def test_ingests_txt_file(
        self, make_mcp_ctx, tmp_path, mongo_client, test_user
    ):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(
            llm=llm, embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Alice works on machine learning projects with Bob.")

        result = await ingest_file(str(txt_file), ctx)

        parsed = json.loads(result)
        assert parsed["status"] == "ingested"

        doc = await Document.find_one(
            Document.source_uri == f"file://{txt_file.resolve()}"
        )
        assert doc is not None
        assert doc.source_type == SourceType.FILE
        assert doc.title == "notes.txt"

    @pytest.mark.slow
    async def test_ingests_html_with_conversion(
        self, make_mcp_ctx, tmp_path, test_user
    ):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(
            llm=llm, embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        html_file = tmp_path / "page.html"
        html_file.write_text("<html><body><p>Hello from HTML.</p></body></html>")

        result = await ingest_file(str(html_file), ctx)

        parsed = json.loads(result)
        assert parsed["status"] == "ingested"

        doc = await Document.find_one(
            Document.source_uri == f"file://{html_file.resolve()}"
        )
        assert doc is not None
        assert "Hello from HTML" in doc.content

    async def test_file_not_found_returns_error(self, make_mcp_ctx, test_user):
        ctx = make_mcp_ctx(
            llm=FakeLLM(), embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        result = await ingest_file("/nonexistent/path/file.txt", ctx)

        parsed = json.loads(result)
        assert parsed["error"] == "file_error"

    @pytest.mark.slow
    async def test_duplicate_file_skipped(self, make_mcp_ctx, tmp_path, test_user):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(
            llm=llm, embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        txt_file = tmp_path / "dup.txt"
        txt_file.write_text("Duplicate content test.")

        first = await ingest_file(str(txt_file), ctx)
        assert json.loads(first)["status"] == "ingested"

        second = await ingest_file(str(txt_file), ctx)
        assert json.loads(second)["status"] == "already_ingested"


# ---------------------------------------------------------------------------
# ingest_url
# ---------------------------------------------------------------------------


@pytest.mark.requires_mongot
class TestIngestUrl:
    @pytest.mark.slow
    async def test_ingests_substack_article(
        self, make_mcp_ctx, mocker, mongo_client, test_user
    ):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(
            llm=llm, embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        sample_html = """
        <html>
        <head>
            <meta property="og:title" content="Test Article">
            <meta property="og:description" content="A test summary">
            <meta name="author" content="Alice">
            <time datetime="2025-01-15T10:00:00Z">Jan 15, 2025</time>
        </head>
        <body>
            <div class="body">
                <p>Alice discusses Python best practices for data pipelines.</p>
            </div>
        </body>
        </html>
        """

        mock_response = mocker.MagicMock()
        mock_response.text = sample_html
        mock_response.raise_for_status = mocker.MagicMock()

        mocker.patch(
            "tree.data.substack.substack_article.httpx.AsyncClient",
            return_value=mocker.AsyncMock(
                __aenter__=mocker.AsyncMock(
                    return_value=mocker.MagicMock(
                        get=mocker.AsyncMock(return_value=mock_response),
                    )
                ),
                __aexit__=mocker.AsyncMock(return_value=False),
            ),
        )

        result = await ingest_url("https://test.substack.com/p/test-article", ctx)

        parsed = json.loads(result)
        assert parsed["status"] == "ingested"
        assert parsed["nodes_extracted"] > 0

        doc = await Document.find_one(
            Document.source_uri == "https://test.substack.com/p/test-article"
        )
        assert doc is not None
        assert doc.source_type == SourceType.SUBSTACK

    async def test_unsupported_scheme_returns_error(self, make_mcp_ctx):
        ctx = make_mcp_ctx(llm=FakeLLM(), embedding_model=FakeEmbeddingModel())

        result = await ingest_url("ftp://example.com/file.tar", ctx)

        parsed = json.loads(result)
        assert parsed["error"] == "unsupported_url"

    @pytest.mark.slow
    async def test_fallthrough_without_brightdata_credentials_returns_config_error(
        self, make_mcp_ctx, mocker
    ):
        """Unmatched http(s) URLs now fall through to the Bright Data web pipeline.

        With no `BRIGHTDATA_API_KEY` configured, the wrapper must translate the
        resulting `BrightDataConfigurationError` into a clean MCP error response
        instead of letting the exception escape.
        """

        # Force credentials empty for this test regardless of test env.
        mocker.patch(
            "tree.data.web.web_unlocker.settings.brightdata_api_key",
            mocker.MagicMock(get_secret_value=mocker.MagicMock(return_value="")),
        )

        ctx = make_mcp_ctx(llm=FakeLLM(), embedding_model=FakeEmbeddingModel())

        result = await ingest_url("https://example.com/some-page", ctx)

        parsed = json.loads(result)
        assert parsed["error"] == "configuration_error"
        assert "BRIGHTDATA_API_KEY" in parsed["detail"]

    @pytest.mark.slow
    async def test_duplicate_url_skipped(self, make_mcp_ctx, mocker, test_user):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(
            llm=llm, embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        sample_html = """
        <html>
        <head><meta property="og:title" content="Dup Article"></head>
        <body><div class="body"><p>Content here.</p></div></body>
        </html>
        """

        mock_response = mocker.MagicMock()
        mock_response.text = sample_html
        mock_response.raise_for_status = mocker.MagicMock()

        mocker.patch(
            "tree.data.substack.substack_article.httpx.AsyncClient",
            return_value=mocker.AsyncMock(
                __aenter__=mocker.AsyncMock(
                    return_value=mocker.MagicMock(
                        get=mocker.AsyncMock(return_value=mock_response),
                    )
                ),
                __aexit__=mocker.AsyncMock(return_value=False),
            ),
        )

        url = "https://dup-test.substack.com/p/dup-article"
        first = await ingest_url(url, ctx)
        assert json.loads(first)["status"] == "ingested"

        second = await ingest_url(url, ctx)
        assert json.loads(second)["status"] == "already_ingested"
