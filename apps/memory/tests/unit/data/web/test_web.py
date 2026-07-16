"""Unit tests for tree.data.web.web — fetch_and_extract_web and load_web_document."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from tree.data.web.web import (
    _derive_summary,
    _derive_title,
    fetch_and_extract_web,
    load_web_document,
)
from tree.entities.documents import Document, SourceType

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


class TestDeriveTitle:
    def test_uses_first_h1(self) -> None:
        markdown = "Intro paragraph\n\n# The Real Title\n\nBody"

        title = _derive_title(markdown, "https://example.com/whatever")

        assert title == "The Real Title"

    def test_skips_h2_and_falls_back_to_path(self) -> None:
        markdown = "## Subheading only\n\nNo h1 here."

        title = _derive_title(markdown, "https://example.com/articles/staff-engineer/")

        assert title == "Staff Engineer"

    def test_url_path_tail_title_cased(self) -> None:
        markdown = "Just plain text, no headings."

        title = _derive_title(markdown, "https://example.com/blog/my-cool-post")

        assert title == "My Cool Post"

    def test_root_path_uses_host(self) -> None:
        markdown = "no headings"

        title = _derive_title(markdown, "https://example.com/")

        assert title == "Example.Com"

    def test_h1_with_extra_hashes_stripped(self) -> None:
        markdown = "#   Title With Spaces   \n\nbody"

        title = _derive_title(markdown, "https://example.com/x")

        assert title == "Title With Spaces"


class TestDeriveSummary:
    def test_first_300_chars_single_line(self) -> None:
        body = "Line one.\nLine two.\nLine three."

        summary = _derive_summary(body)

        assert "\n" not in summary
        assert "Line one." in summary
        assert "Line two." in summary

    def test_truncates_at_300_chars(self) -> None:
        body = "x" * 1000

        summary = _derive_summary(body)

        assert len(summary) == 300

    def test_collapses_whitespace(self) -> None:
        body = "hello   world\t\tfoo"

        summary = _derive_summary(body)

        assert summary == "hello world foo"

    def test_empty_body(self) -> None:
        assert _derive_summary("") == ""


class TestFetchAndExtractWeb:
    async def test_returns_document_with_web_source(self, mocker) -> None:
        markdown = "# A Great Article\n\nThis is the body of the article."
        mocker.patch(
            "tree.data.web.web.fetch_url",
            new_callable=AsyncMock,
            return_value=markdown,
        )

        doc = await fetch_and_extract_web("https://example.com/posts/great", _USER_ID)

        assert doc.source_type == SourceType.WEB
        assert doc.source_uri == "https://example.com/posts/great"
        assert doc.title == "A Great Article"
        assert doc.content == markdown
        assert doc.summary  # non-empty
        assert doc.authors == ["Unknown"]
        assert doc.date is not None
        assert doc.date.tzinfo is not None
        # tzinfo is UTC
        assert doc.date.utcoffset().total_seconds() == 0

    async def test_falls_back_to_url_path_tail(self, mocker) -> None:
        markdown = "Plain body, no h1."
        mocker.patch(
            "tree.data.web.web.fetch_url",
            new_callable=AsyncMock,
            return_value=markdown,
        )

        doc = await fetch_and_extract_web(
            "https://example.com/blog/some-thing", _USER_ID
        )

        assert doc.title == "Some Thing"

    async def test_passes_markdown_data_format(self, mocker) -> None:
        markdown = "# Title\n\nbody"
        mock_fetch = mocker.patch(
            "tree.data.web.web.fetch_url",
            new_callable=AsyncMock,
            return_value=markdown,
        )

        await fetch_and_extract_web("https://example.com/x", _USER_ID)

        mock_fetch.assert_awaited_once()
        kwargs = mock_fetch.call_args.kwargs
        assert kwargs.get("data_format") == "markdown"


def _make_doc(
    *,
    source_uri: str = "https://example.com/x",
    title: str = "Title",
    content: str = "body",
) -> Document:
    return Document(
        source_type=SourceType.WEB,
        source_uri=source_uri,
        user_id=PydanticObjectId(),
        title=title,
        summary=content[:300],
        content=content,
        authors=["Unknown"],
        date=datetime.now(tz=UTC),
    )


class TestLoadWebDocument:
    async def test_inserts_new_document(self, mocker) -> None:
        doc = _make_doc()

        mock_find = mocker.patch(
            "tree.data.web.web.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mock_insert = mocker.patch(
            "tree.data.web.web.Document.insert", new_callable=AsyncMock
        )

        result = await load_web_document(doc)

        assert result is doc
        mock_find.assert_awaited_once()
        mock_insert.assert_awaited_once()

    async def test_returns_none_for_non_latent_duplicate(self, mocker) -> None:
        doc = _make_doc()

        existing = MagicMock()
        existing.source_type = SourceType.WEB

        mocker.patch(
            "tree.data.web.web.Document.find_one",
            new_callable=AsyncMock,
            return_value=existing,
        )

        result = await load_web_document(doc)

        assert result is None

    async def test_upgrades_latent_document_in_place(self, mocker) -> None:
        doc = _make_doc(
            source_uri="https://example.com/y",
            title="Promoted Title",
            content="full markdown body",
        )

        existing = MagicMock()
        existing.source_type = SourceType.LATENT
        existing.replace = AsyncMock()

        mocker.patch(
            "tree.data.web.web.Document.find_one",
            new_callable=AsyncMock,
            return_value=existing,
        )

        result = await load_web_document(doc)

        assert result is existing
        assert existing.source_type == SourceType.WEB
        assert existing.title == "Promoted Title"
        assert existing.content == "full markdown body"
        # date got refreshed and is timezone-aware UTC
        assert existing.date.tzinfo is not None
        assert existing.date.utcoffset().total_seconds() == 0
        existing.replace.assert_awaited_once()

    async def test_returns_none_on_duplicate_key_race(self, mocker) -> None:
        doc = _make_doc()

        mocker.patch(
            "tree.data.web.web.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.web.web.Document.insert",
            new_callable=AsyncMock,
            side_effect=DuplicateKeyError("dup"),
        )

        result = await load_web_document(doc)

        assert result is None


class TestLoadWebDocumentReturnType:
    @pytest.mark.parametrize(
        "source_type",
        [SourceType.SUBSTACK, SourceType.FILE, SourceType.HUGGINGFACE, SourceType.WEB],
        ids=["substack", "file", "huggingface", "web"],
    )
    async def test_skips_any_non_latent_existing(
        self, mocker, source_type: SourceType
    ) -> None:
        doc = _make_doc()
        existing = MagicMock()
        existing.source_type = source_type

        mocker.patch(
            "tree.data.web.web.Document.find_one",
            new_callable=AsyncMock,
            return_value=existing,
        )

        result = await load_web_document(doc)

        assert result is None
