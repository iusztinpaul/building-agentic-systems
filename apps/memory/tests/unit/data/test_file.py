"""Unit tests for tree.data.file.file — read_file and load_file_document."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.data.file.file import _SUPPORTED_EXTENSIONS, read_file

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


class TestReadFile:
    def test_reads_txt_file(self, tmp_path) -> None:
        txt = tmp_path / "notes.txt"
        txt.write_text("hello world", encoding="utf-8")

        assert read_file(str(txt)) == "hello world"

    def test_reads_md_file(self, tmp_path) -> None:
        md = tmp_path / "readme.md"
        md.write_text("# Title\nBody text", encoding="utf-8")

        assert read_file(str(md)) == "# Title\nBody text"

    def test_reads_html_and_converts_to_plain_text(self, tmp_path) -> None:
        html = tmp_path / "page.html"
        html.write_text("<html><body><p>Hello</p></body></html>", encoding="utf-8")

        result = read_file(str(html))
        assert "Hello" in result
        assert "<p>" not in result

    def test_raises_file_not_found(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            read_file(str(tmp_path / "nonexistent.txt"))

    def test_raises_is_a_directory(self, tmp_path) -> None:
        with pytest.raises(IsADirectoryError):
            read_file(str(tmp_path))

    @pytest.mark.parametrize(
        "extension",
        [".py", ".json", ".csv", ".pdf"],
        ids=["py", "json", "csv", "pdf"],
    )
    def test_raises_value_error_for_unsupported_extension(
        self, tmp_path, extension: str
    ) -> None:
        bad_file = tmp_path / f"data{extension}"
        bad_file.write_text("content", encoding="utf-8")

        with pytest.raises(ValueError, match="Unsupported file type"):
            read_file(str(bad_file))

    def test_raises_unicode_decode_error_for_binary(self, tmp_path) -> None:
        binary = tmp_path / "broken.txt"
        binary.write_bytes(b"\x80\x81\x82\xff")

        with pytest.raises(UnicodeDecodeError):
            read_file(str(binary))

    def test_supported_extensions_constant(self) -> None:
        assert ".txt" in _SUPPORTED_EXTENSIONS
        assert ".md" in _SUPPORTED_EXTENSIONS
        assert ".html" in _SUPPORTED_EXTENSIONS


class TestLoadFileDocument:
    async def test_creates_document_for_new_file(self, tmp_path, mocker) -> None:
        txt = tmp_path / "notes.txt"
        txt.write_text("Some content", encoding="utf-8")

        mock_find_one = mocker.patch(
            "tree.data.file.file.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mock_insert = mocker.patch(
            "tree.data.file.file.Document.insert", new_callable=AsyncMock
        )

        from tree.data.file.file import load_file_document

        doc = await load_file_document(str(txt), _USER_ID)

        assert doc is not None
        assert doc.source_uri == f"file://{txt.resolve()}"
        assert doc.title == "notes.txt"
        assert doc.content == "Some content"
        mock_find_one.assert_awaited_once()
        mock_insert.assert_awaited_once()

    async def test_returns_none_for_duplicate(self, tmp_path, mocker) -> None:
        txt = tmp_path / "dup.txt"
        txt.write_text("content", encoding="utf-8")

        from tree.entities.documents import SourceType

        existing = MagicMock()
        existing.source_type = SourceType.FILE

        mocker.patch(
            "tree.data.file.file.Document.find_one",
            new_callable=AsyncMock,
            return_value=existing,
        )

        from tree.data.file.file import load_file_document

        result = await load_file_document(str(txt), _USER_ID)
        assert result is None

    async def test_custom_title_used(self, tmp_path, mocker) -> None:
        txt = tmp_path / "data.txt"
        txt.write_text("text", encoding="utf-8")

        mocker.patch(
            "tree.data.file.file.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.file.file.Document.insert", new_callable=AsyncMock)

        from tree.data.file.file import load_file_document

        doc = await load_file_document(str(txt), _USER_ID, title="My Custom Title")
        assert doc.title == "My Custom Title"
