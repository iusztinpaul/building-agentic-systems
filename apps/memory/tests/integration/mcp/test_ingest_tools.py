"""Integration tests for MCP ingestion tools — end-to-end through real MongoDB."""

import json


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


class TestIngestConversation:
    async def test_creates_document_and_extracts(self, make_mcp_ctx, mongo_client):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

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

        # Verify KG entries were created.
        kg_col = mongo_client[TEST_DATABASE]["knowledge_graph"]
        kg_count = await kg_col.count_documents({})
        assert kg_count > 0

    async def test_returns_summary_with_counts(self, make_mcp_ctx):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

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


class TestIngestFile:
    async def test_ingests_txt_file(self, make_mcp_ctx, tmp_path, mongo_client):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

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

    async def test_ingests_html_with_conversion(self, make_mcp_ctx, tmp_path):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

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

    async def test_file_not_found_returns_error(self, make_mcp_ctx):
        ctx = make_mcp_ctx(llm=FakeLLM(), embedding_model=FakeEmbeddingModel())

        result = await ingest_file("/nonexistent/path/file.txt", ctx)

        parsed = json.loads(result)
        assert parsed["error"] == "file_error"

    async def test_duplicate_file_skipped(self, make_mcp_ctx, tmp_path):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

        txt_file = tmp_path / "dup.txt"
        txt_file.write_text("Duplicate content test.")

        first = await ingest_file(str(txt_file), ctx)
        assert json.loads(first)["status"] == "ingested"

        second = await ingest_file(str(txt_file), ctx)
        assert json.loads(second)["status"] == "already_ingested"


# ---------------------------------------------------------------------------
# ingest_url
# ---------------------------------------------------------------------------


class TestIngestUrl:
    async def test_ingests_substack_article(self, make_mcp_ctx, mocker, mongo_client):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

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

    async def test_unsupported_url_returns_error(self, make_mcp_ctx):
        ctx = make_mcp_ctx(llm=FakeLLM(), embedding_model=FakeEmbeddingModel())

        result = await ingest_url("https://example.com/some-page", ctx)

        parsed = json.loads(result)
        assert parsed["error"] == "unsupported_url"

    async def test_duplicate_url_skipped(self, make_mcp_ctx, mocker):
        llm = _make_fake_llm()
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

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
