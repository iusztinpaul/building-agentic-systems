"""Unit tests for tree.data.conversation — load_conversation_document."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from tree.data.conversation import _content_hash, load_conversation_document


class TestContentHash:
    def test_returns_hex_string(self) -> None:
        result = _content_hash("hello")
        assert isinstance(result, str)
        assert len(result) == 16

    def test_deterministic(self) -> None:
        assert _content_hash("same text") == _content_hash("same text")

    def test_different_text_gives_different_hash(self) -> None:
        assert _content_hash("text a") != _content_hash("text b")


class TestLoadConversationDocument:
    async def test_raises_for_empty_text(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            await load_conversation_document("   ")

    async def test_raises_for_whitespace_only(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            await load_conversation_document("\n\t  ")

    async def test_creates_document_for_new_conversation(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        doc = await load_conversation_document("Alice likes Python.")

        assert doc is not None
        assert doc.source_uri.startswith("conversation://")
        assert doc.content == "Alice likes Python."

    async def test_source_uri_is_deterministic(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        doc1 = await load_conversation_document("Same text")
        doc2 = await load_conversation_document("Same text")

        assert doc1.source_uri == doc2.source_uri

    async def test_returns_none_for_duplicate(self, mocker) -> None:
        existing = MagicMock()
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=existing,
        )

        result = await load_conversation_document("Already ingested text.")
        assert result is None

    async def test_custom_title_used(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        doc = await load_conversation_document("Text", title="My Title")
        assert doc.title == "My Title"

    async def test_default_title_contains_timestamp(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        doc = await load_conversation_document("Text")
        assert doc.title.startswith("Conversation ")

    async def test_handles_duplicate_key_error_gracefully(self, mocker) -> None:
        from pymongo.errors import DuplicateKeyError

        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.Document.insert",
            new_callable=AsyncMock,
            side_effect=DuplicateKeyError("duplicate"),
        )

        result = await load_conversation_document("Race condition text.")
        assert result is None
