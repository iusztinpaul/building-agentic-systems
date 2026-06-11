"""Integration tests for MCP ingestion tools — end-to-end through real MongoDB.

The ingest tools are now ASYNC: each creates a real ``Document`` in MongoDB and
then SUBMITS the memory-extraction pipeline to Prefect, returning immediately
(``{"status": "submitted", "flow_run_id": ...}``) instead of running extraction
in-process. These tests therefore exercise the real document-creation path end
to end against MongoDB and stub only the Prefect submission boundary (an external
infra dependency) so they don't need a live Prefect API or a served worker.

Because extraction no longer runs inline, these tests no longer issue the live
Atlas ``$vectorSearch`` that previously forced the ``requires_mongot``/``slow``
markers — they're plain MongoDB integration tests now.
"""

import json

import pytest

from tree.entities.documents import Document, SourceType
from tree.mcp.tools import ingest_conversation, ingest_file, ingest_url
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM

TEST_DATABASE = "integration_tests_twin"


@pytest.fixture
def stub_prefect_submit(mocker):
    """Stub the Prefect submission boundary used by ``submit_ingestion``.

    Replaces ``tree.mcp.ingest.get_client`` with a fake async client whose
    ``read_deployment_by_name`` / ``create_flow_run_from_deployment`` succeed, so
    ``submit_ingestion`` returns ``status="submitted"`` without a live Prefect API
    or worker. Yields the fake client so tests can assert the submitted params.
    """

    deployment = mocker.MagicMock(id="dep-123")
    flow_run = mocker.MagicMock(id="fr-456")
    fake_client = mocker.MagicMock()
    fake_client.read_deployment_by_name = mocker.AsyncMock(return_value=deployment)
    fake_client.create_flow_run_from_deployment = mocker.AsyncMock(
        return_value=flow_run
    )
    ctx_manager = mocker.MagicMock()
    ctx_manager.__aenter__ = mocker.AsyncMock(return_value=fake_client)
    ctx_manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("tree.mcp.ingest.get_client", return_value=ctx_manager)
    return fake_client


# ---------------------------------------------------------------------------
# ingest_conversation
# ---------------------------------------------------------------------------


class TestIngestConversation:
    async def test_creates_document_and_submits(
        self, make_mcp_ctx, stub_prefect_submit, test_user
    ):
        ctx = make_mcp_ctx(
            llm=FakeLLM(), embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        result = await ingest_conversation(
            "Alice told me she loves Python for data science.", ctx
        )

        parsed = json.loads(result)
        assert parsed["status"] == "submitted"
        assert parsed["flow_run_id"] == "fr-456"

        # The Document is persisted (real e2e) and tagged with the pinned user_id.
        doc = await Document.find_one(Document.source_uri == parsed["source_uri"])
        assert doc is not None
        assert doc.source_type == SourceType.CONVERSATION
        assert doc.user_id == test_user.id

        # The submitted flow run is scoped to this user + just this document.
        submit_kwargs = (
            stub_prefect_submit.create_flow_run_from_deployment.await_args.kwargs
        )
        params = submit_kwargs["parameters"]
        assert params["user_id"] == str(test_user.id)
        assert params["document_ids"] == [str(doc.id)]

    async def test_returns_submission_summary(
        self, make_mcp_ctx, stub_prefect_submit, test_user
    ):
        ctx = make_mcp_ctx(
            llm=FakeLLM(), embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        result = await ingest_conversation("Bob discussed his new project.", ctx)

        parsed = json.loads(result)
        assert parsed["status"] == "submitted"
        assert "document_id" in parsed
        assert "source_uri" in parsed
        assert "title" in parsed
        assert "flow_run_id" in parsed

    async def test_empty_text_returns_error(self, make_mcp_ctx):
        ctx = make_mcp_ctx(llm=FakeLLM(), embedding_model=FakeEmbeddingModel())

        result = await ingest_conversation("   ", ctx)

        parsed = json.loads(result)
        assert parsed["error"] == "empty_input"


# ---------------------------------------------------------------------------
# ingest_file
# ---------------------------------------------------------------------------


class TestIngestFile:
    async def test_ingests_txt_file(
        self, make_mcp_ctx, stub_prefect_submit, tmp_path, test_user
    ):
        ctx = make_mcp_ctx(
            llm=FakeLLM(), embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Alice works on machine learning projects with Bob.")

        result = await ingest_file(str(txt_file), ctx)

        parsed = json.loads(result)
        assert parsed["status"] == "submitted"
        assert parsed["flow_run_id"] == "fr-456"

        doc = await Document.find_one(
            Document.source_uri == f"file://{txt_file.resolve()}"
        )
        assert doc is not None
        assert doc.source_type == SourceType.FILE
        assert doc.title == "notes.txt"

    async def test_ingests_html_with_conversion(
        self, make_mcp_ctx, stub_prefect_submit, tmp_path, test_user
    ):
        ctx = make_mcp_ctx(
            llm=FakeLLM(), embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        html_file = tmp_path / "page.html"
        html_file.write_text("<html><body><p>Hello from HTML.</p></body></html>")

        result = await ingest_file(str(html_file), ctx)

        parsed = json.loads(result)
        assert parsed["status"] == "submitted"

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

    async def test_duplicate_file_skipped(
        self, make_mcp_ctx, stub_prefect_submit, tmp_path, test_user
    ):
        ctx = make_mcp_ctx(
            llm=FakeLLM(), embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        txt_file = tmp_path / "dup.txt"
        txt_file.write_text("Duplicate content test.")

        first = await ingest_file(str(txt_file), ctx)
        assert json.loads(first)["status"] == "submitted"

        second = await ingest_file(str(txt_file), ctx)
        assert json.loads(second)["status"] == "already_ingested"


# ---------------------------------------------------------------------------
# ingest_url
# ---------------------------------------------------------------------------


def _mock_substack_fetch(mocker, sample_html: str) -> None:
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


class TestIngestUrl:
    async def test_ingests_substack_article(
        self, make_mcp_ctx, stub_prefect_submit, mocker, test_user
    ):
        ctx = make_mcp_ctx(
            llm=FakeLLM(), embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        _mock_substack_fetch(
            mocker,
            """
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
            """,
        )

        result = await ingest_url("https://test.substack.com/p/test-article", ctx)

        parsed = json.loads(result)
        assert parsed["status"] == "submitted"
        assert parsed["flow_run_id"] == "fr-456"

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

    async def test_duplicate_url_skipped(
        self, make_mcp_ctx, stub_prefect_submit, mocker, test_user
    ):
        ctx = make_mcp_ctx(
            llm=FakeLLM(), embedding_model=FakeEmbeddingModel(), user_id=test_user.id
        )

        _mock_substack_fetch(
            mocker,
            """
            <html>
            <head><meta property="og:title" content="Dup Article"></head>
            <body><div class="body"><p>Content here.</p></div></body>
            </html>
            """,
        )

        url = "https://dup-test.substack.com/p/dup-article"
        first = await ingest_url(url, ctx)
        assert json.loads(first)["status"] == "submitted"

        second = await ingest_url(url, ctx)
        assert json.loads(second)["status"] == "already_ingested"
