import httpx
import pytest
from beanie import PydanticObjectId

from tree.data.substack.substack_article import (
    _extract_article_body,
    _extract_meta,
    _parse_article_date,
    extract_document_from_html,
    fetch_article,
)
from tree.entities.documents import SourceType

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")

SAMPLE_HTML = """
<html>
<head>
    <meta property="og:title" content="My Substack Article" />
    <meta property="og:description" content="A great summary." />
    <meta name="author" content="Paul Iusztin" />
    <meta property="article:published_time" content="2026-03-15T10:00:00+00:00" />
</head>
<body>
    <div class="body">
        <p>Hello <strong>world</strong>.</p>
        <p>Check <a href="https://example.com/ref1">this</a>.</p>
    </div>
</body>
</html>
"""

MINIMAL_HTML = """
<html>
<head><title>Fallback Title</title></head>
<body><article><p>Article content here.</p></article></body>
</html>
"""

EMPTY_HTML = "<html><head></head><body></body></html>"


class TestExtractMeta:
    def test_extracts_og_property(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        assert _extract_meta(soup, "og:title") == "My Substack Article"

    def test_extracts_name_attribute(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        assert _extract_meta(soup, "author") == "Paul Iusztin"

    def test_returns_empty_for_missing(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        assert _extract_meta(soup, "nonexistent") == ""


class TestParseArticleDate:
    def test_parses_iso_from_meta(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        dt = _parse_article_date(soup)
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 15

    def test_parses_from_time_tag(self):
        from bs4 import BeautifulSoup

        html = '<html><head></head><body><time datetime="2025-06-01T08:00:00+00:00">June 1</time></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        dt = _parse_article_date(soup)
        assert dt.year == 2025
        assert dt.month == 6

    def test_fallback_to_now(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(EMPTY_HTML, "html.parser")
        dt = _parse_article_date(soup)
        assert dt.tzinfo is not None


class TestExtractArticleBody:
    def test_extracts_div_body(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        body = _extract_article_body(soup)
        assert "Hello" in body
        assert "<strong>world</strong>" in body

    def test_falls_back_to_article_tag(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(MINIMAL_HTML, "html.parser")
        body = _extract_article_body(soup)
        assert "Article content here" in body

    def test_returns_empty_for_no_body(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(EMPTY_HTML, "html.parser")
        assert _extract_article_body(soup) == ""


class TestExtractDocumentFromHtml:
    def test_extracts_all_fields(self):
        doc, _ = extract_document_from_html(
            SAMPLE_HTML, "https://example.substack.com/p/my-article", _USER_ID
        )

        assert doc.source_type == SourceType.SUBSTACK
        assert doc.source_uri == "https://example.substack.com/p/my-article"
        assert doc.title == "My Substack Article"
        assert doc.summary == "A great summary."
        assert doc.authors == ["Paul Iusztin"]
        assert doc.date.year == 2026
        assert "Hello" in doc.content
        assert "world" in doc.content
        assert "<p>" not in doc.content

    def test_fallback_title_from_title_tag(self):
        doc, _ = extract_document_from_html(
            MINIMAL_HTML, "https://example.com/p/test", _USER_ID
        )

        assert doc.title == "Fallback Title"

    def test_summary_falls_back_to_title(self):
        doc, _ = extract_document_from_html(
            MINIMAL_HTML, "https://example.com/p/test", _USER_ID
        )

        assert doc.summary == "Fallback Title"

    def test_missing_author_defaults_to_unknown(self):
        doc, _ = extract_document_from_html(
            EMPTY_HTML, "https://example.com/p/test", _USER_ID
        )

        assert doc.authors == ["Unknown"]

    def test_empty_html(self):
        doc, _ = extract_document_from_html(
            EMPTY_HTML, "https://example.com/p/test", _USER_ID
        )

        assert doc.source_uri == "https://example.com/p/test"
        assert doc.content == ""


class TestFetchArticle:
    def _mock_httpx(self, mocker, mock_response):
        mock_client = mocker.AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_response
        mocker.patch(
            "tree.data.substack.substack_article.httpx.AsyncClient",
            return_value=mock_client,
        )
        return mock_client

    async def test_returns_html(self, mocker):
        mock_response = mocker.Mock(text="<html>content</html>")
        mock_client = self._mock_httpx(mocker, mock_response)

        result = await fetch_article("https://example.substack.com/p/test")

        assert result == "<html>content</html>"
        mock_client.get.assert_called_once_with(
            "https://example.substack.com/p/test", follow_redirects=True, timeout=30
        )

    async def test_raises_on_http_error(self, mocker):
        mock_response = mocker.Mock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=mocker.Mock(),
            response=mocker.Mock(status_code=404),
        )
        self._mock_httpx(mocker, mock_response)

        with pytest.raises(httpx.HTTPStatusError):
            await fetch_article("https://example.substack.com/p/test")
