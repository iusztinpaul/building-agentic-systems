import pytest

from twin.data.substack_rss import (
    SubstackRSSFeedETL,
    _extract_references,
    _html_to_plain_text,
    _parse_date,
)
from twin.entities.documents import SourceType

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


class TestHtmlToPlainText:
    def test_strips_tags(self):
        assert _html_to_plain_text("<p>Hello <b>world</b></p>") == "Hello world"

    def test_preserves_paragraph_breaks(self):
        result = _html_to_plain_text("<p>Line 1</p><p>Line 2</p>")
        lines = result.splitlines()
        assert "Line 1" in lines
        assert "Line 2" in lines

    def test_empty_string(self):
        assert _html_to_plain_text("") == ""


class TestExtractReferences:
    def test_extracts_urls(self):
        html = '<a href="https://example.com">link</a><a href="https://other.com">link2</a>'
        refs = _extract_references(html)
        assert refs == ["https://example.com", "https://other.com"]

    def test_deduplicates(self):
        html = '<a href="https://example.com">a</a><a href="https://example.com">b</a>'
        refs = _extract_references(html)
        assert refs == ["https://example.com"]

    def test_skips_non_http(self):
        html = '<a href="mailto:test@test.com">email</a><a href="https://real.com">real</a>'
        refs = _extract_references(html)
        assert refs == ["https://real.com"]


class TestParseDate:
    def test_rfc2822_format(self):
        entry = {"published": "Tue, 24 Feb 2026 12:02:13 GMT"}
        dt = _parse_date(entry)
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.day == 24

    def test_missing_date_returns_now(self):
        dt = _parse_date({})
        assert dt.tzinfo is not None

    def test_invalid_date_returns_now(self):
        dt = _parse_date({"published": "not-a-date"})
        assert dt.tzinfo is not None


class TestSubstackRSSFeedETLExtractOne:
    @pytest.fixture
    def etl(self):
        return SubstackRSSFeedETL()

    async def test_extract_one_maps_fields(self, etl):
        doc = await etl.extract_one(SAMPLE_ENTRY)

        assert doc.source_type == SourceType.SUBSTACK
        assert doc.source_uri == "https://example.substack.com/p/test-article"
        assert doc.summary == "A short subtitle for the article."
        assert doc.authors == ["Paul Iusztin"]
        assert doc.date.year == 2026
        assert "Hello" in doc.content
        assert "world" in doc.content
        assert "<p>" not in doc.content
        assert len(doc.references) == 2
        assert doc.summary_embedding == []

    async def test_extract_one_fallback_to_summary(self, etl):
        entry = {**SAMPLE_ENTRY, "content": [{}]}
        doc = await etl.extract_one(entry)

        assert doc.content == "A short subtitle for the article."
