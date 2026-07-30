import httpx
import pytest
from beanie import PydanticObjectId
from bs4 import BeautifulSoup
from pymongo.errors import DuplicateKeyError

from tree.data.substack.substack import (
    _extract_article_body,
    _extract_meta,
    _parse_article_date,
    entry_content_html,
    extract_document,
    extract_document_from_html,
    extract_references,
    fetch_article,
    fetch_feed,
    html_to_plain_text,
    load_document,
    parse_date,
)
from tree.entities.documents import Document, SourceType

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")

SAMPLE_ENTRY = {
    "title": "Test Article",
    "link": "https://example.substack.com/p/test-article",
    "summary": "A short subtitle for the article.",
    "author": "Paul Iusztin",
    "published": "Tue, 24 Feb 2026 12:02:13 GMT",
    "content": [
        {
            "value": (
                "<p>Hello <strong>world</strong>.</p>"
                '<p>Check <a href="https://example.com/ref1">this</a> and '
                '<a href="https://example.com/ref2">that</a>.</p>'
            ),
        }
    ],
}

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


class TestEntryContentHtml:
    def test_reads_first_content_value(self):
        assert "Hello" in entry_content_html(SAMPLE_ENTRY)

    def test_missing_content_key_is_empty(self):
        assert entry_content_html({"title": "No content"}) == ""

    def test_empty_content_list_is_empty_not_indexerror(self):
        assert entry_content_html({"content": []}) == ""


class TestHtmlToPlainText:
    def test_strips_tags(self):
        assert html_to_plain_text("<p>Hello <b>world</b></p>") == "Hello world"

    def test_preserves_paragraph_breaks(self):
        result = html_to_plain_text("<p>Line 1</p><p>Line 2</p>")
        lines = result.splitlines()
        assert "Line 1" in lines
        assert "Line 2" in lines

    def test_empty_string(self):
        assert html_to_plain_text("") == ""


class TestExtractReferences:
    def test_extracts_urls(self):
        html = '<a href="https://example.com">link</a><a href="https://other.com">link2</a>'
        refs = extract_references(html)
        assert refs == ["https://example.com", "https://other.com"]

    def test_deduplicates(self):
        html = '<a href="https://example.com">a</a><a href="https://example.com">b</a>'
        refs = extract_references(html)
        assert refs == ["https://example.com"]

    def test_skips_non_http(self):
        html = '<a href="mailto:test@test.com">email</a><a href="https://real.com">real</a>'
        refs = extract_references(html)
        assert refs == ["https://real.com"]

    def test_no_anchor_tags(self):
        html = "<p>No links here.</p>"
        assert extract_references(html) == []


class TestParseDate:
    def test_rfc2822_format(self):
        entry = {"published": "Tue, 24 Feb 2026 12:02:13 GMT"}
        dt = parse_date(entry)
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.day == 24

    def test_missing_date_returns_now(self):
        dt = parse_date({})
        assert dt.tzinfo is not None

    def test_invalid_date_returns_now(self):
        dt = parse_date({"published": "not-a-date"})
        assert dt.tzinfo is not None


class TestExtractDocument:
    def test_maps_fields(self):
        doc = extract_document(SAMPLE_ENTRY, _USER_ID)

        assert doc.source_type == SourceType.SUBSTACK
        assert doc.source_uri == "https://example.substack.com/p/test-article"
        assert doc.summary == "A short subtitle for the article."
        assert doc.authors == ["Paul Iusztin"]
        assert doc.date.year == 2026
        assert "Hello" in doc.content
        assert "world" in doc.content
        assert "<p>" not in doc.content
        assert doc.references == []

    def test_fallback_to_summary(self):
        entry = {**SAMPLE_ENTRY, "content": [{}]}
        doc = extract_document(entry, _USER_ID)

        assert doc.content == "A short subtitle for the article."

    def test_missing_author_defaults_to_unknown(self):
        entry = {k: v for k, v in SAMPLE_ENTRY.items() if k != "author"}
        doc = extract_document(entry, _USER_ID)

        assert doc.authors == ["Unknown"]

    def test_empty_entry(self):
        doc = extract_document({}, _USER_ID)

        assert doc.source_uri == ""
        assert doc.title == ""
        assert doc.content == ""
        assert doc.authors == ["Unknown"]
        assert doc.summary == ""


class TestFetchFeed:
    def _mock_httpx(self, mocker, mock_response):
        """Patch httpx.AsyncClient so `async with … as client` returns a mock
        whose `.get()` resolves to *mock_response*."""
        mock_client = mocker.AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_response
        mocker.patch(
            "tree.data.substack.substack.httpx.AsyncClient",
            return_value=mock_client,
        )
        return mock_client

    async def test_returns_parsed_entries(self, mocker):
        mock_response = mocker.Mock(text="<rss>mock</rss>")
        mock_client = self._mock_httpx(mocker, mock_response)

        entries = [{"title": "Entry 1"}, {"title": "Entry 2"}]
        mocker.patch(
            "tree.data.substack.substack.feedparser.parse",
            return_value=mocker.Mock(bozo=False, entries=entries),
        )

        result = await fetch_feed("https://example.com/feed")

        assert result == entries
        mock_client.get.assert_called_once_with(
            "https://example.com/feed", follow_redirects=True, timeout=30
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
            await fetch_feed("https://example.com/feed")

    async def test_raises_on_malformed_feed_without_entries(self, mocker):
        mock_response = mocker.Mock(text="not xml")
        self._mock_httpx(mocker, mock_response)

        mocker.patch(
            "tree.data.substack.substack.feedparser.parse",
            return_value=mocker.Mock(
                bozo=True, entries=[], bozo_exception=Exception("bad")
            ),
        )

        with pytest.raises(ValueError, match="Failed to parse RSS feed"):
            await fetch_feed("https://example.com/feed")

    async def test_bozo_feed_with_entries_returns_entries(self, mocker):
        mock_response = mocker.Mock(text="<rss>partial</rss>")
        self._mock_httpx(mocker, mock_response)

        entries = [{"title": "Recovered Entry"}]
        mocker.patch(
            "tree.data.substack.substack.feedparser.parse",
            return_value=mocker.Mock(bozo=True, entries=entries),
        )

        result = await fetch_feed("https://example.com/feed")

        assert result == entries

    async def test_returns_empty_list_for_empty_feed(self, mocker):
        mock_response = mocker.Mock(text="<rss></rss>")
        self._mock_httpx(mocker, mock_response)

        mocker.patch(
            "tree.data.substack.substack.feedparser.parse",
            return_value=mocker.Mock(bozo=False, entries=[]),
        )

        result = await fetch_feed("https://example.com/feed")

        assert result == []


class TestLoadDocument:
    async def test_returns_none_on_duplicate_key_race(self, mocker):
        """The in-batch collision path: a flattened unified batch can hold the
        same canonical URL twice (once via a feed entry, once via a single
        source). Both pass `find_one` as "not present", then race on `insert()`;
        the second `insert()` raises `DuplicateKeyError`, which the loader must
        convert into a clean `None` skip rather than propagate.
        """
        doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://example.substack.com/p/test-article",
            user_id=_USER_ID,
        )

        # No existing doc → take the insert path (not the dedup early-return).
        mocker.patch(
            "tree.data.substack.substack.Document.find_one",
            new_callable=mocker.AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.substack.substack.Document.insert",
            new_callable=mocker.AsyncMock,
            side_effect=DuplicateKeyError("dup"),
        )

        # Empty content → no references to resolve; isolates the insert branch.
        result = await load_document(doc, {"content": [{"value": ""}]})

        assert result is None


class TestExtractMeta:
    def test_extracts_og_property(self):
        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        assert _extract_meta(soup, "og:title") == "My Substack Article"

    def test_extracts_name_attribute(self):
        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        assert _extract_meta(soup, "author") == "Paul Iusztin"

    def test_returns_empty_for_missing(self):
        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        assert _extract_meta(soup, "nonexistent") == ""


class TestParseArticleDate:
    def test_parses_iso_from_meta(self):
        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        dt = _parse_article_date(soup)
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 15

    def test_parses_from_time_tag(self):
        html = '<html><head></head><body><time datetime="2025-06-01T08:00:00+00:00">June 1</time></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        dt = _parse_article_date(soup)
        assert dt.year == 2025
        assert dt.month == 6

    def test_fallback_to_now(self):
        soup = BeautifulSoup(EMPTY_HTML, "html.parser")
        dt = _parse_article_date(soup)
        assert dt.tzinfo is not None


class TestExtractArticleBody:
    def test_extracts_div_body(self):
        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        body = _extract_article_body(soup)
        assert "Hello" in body
        assert "<strong>world</strong>" in body

    def test_falls_back_to_article_tag(self):
        soup = BeautifulSoup(MINIMAL_HTML, "html.parser")
        body = _extract_article_body(soup)
        assert "Article content here" in body

    def test_returns_empty_for_no_body(self):
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
            "tree.data.substack.substack.httpx.AsyncClient",
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
