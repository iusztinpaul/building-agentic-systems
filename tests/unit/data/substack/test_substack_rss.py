from twin.data.substack.substack_rss import (
    extract_document,
    extract_references,
    html_to_plain_text,
    parse_date,
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
        doc = extract_document(SAMPLE_ENTRY)

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
        doc = extract_document(entry)

        assert doc.content == "A short subtitle for the article."

    def test_missing_author_defaults_to_unknown(self):
        entry = {k: v for k, v in SAMPLE_ENTRY.items() if k != "author"}
        doc = extract_document(entry)

        assert doc.authors == ["Unknown"]

    def test_empty_entry(self):
        doc = extract_document({})

        assert doc.source_uri == ""
        assert doc.title == ""
        assert doc.content == ""
        assert doc.authors == ["Unknown"]
        assert doc.summary == ""
