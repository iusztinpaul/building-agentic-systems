"""Unit tests for tree.data.conversation — load_conversation_document."""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.data.conversation import _content_hash, load_conversation_document

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


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
            await load_conversation_document("   ", _USER_ID)

    async def test_raises_for_whitespace_only(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            await load_conversation_document("\n\t  ", _USER_ID)

    async def test_creates_document_for_new_conversation(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        doc = await load_conversation_document("Alice likes Python.", _USER_ID)

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

        doc1 = await load_conversation_document("Same text", _USER_ID)
        doc2 = await load_conversation_document("Same text", _USER_ID)

        assert doc1.source_uri == doc2.source_uri

    async def test_returns_none_for_duplicate(self, mocker) -> None:
        existing = MagicMock()
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=existing,
        )

        result = await load_conversation_document("Already ingested text.", _USER_ID)
        assert result is None

    async def test_custom_title_used(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        doc = await load_conversation_document("Text", _USER_ID, title="My Title")
        assert doc.title == "My Title"

    async def test_default_title_contains_timestamp(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        doc = await load_conversation_document("Text", _USER_ID)
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

        result = await load_conversation_document("Race condition text.", _USER_ID)
        assert result is None


class TestSourceUriDerivation:
    """Phase-2: source_uri rule — session_uri wins; else content-hash fallback."""

    async def test_session_uri_used_verbatim_when_provided(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        doc = await load_conversation_document(
            "Alice likes Python.",
            _USER_ID,
            session_uri="claude-session://abc",
        )

        assert doc is not None
        assert doc.source_uri == "claude-session://abc"
        # No content-hash prefix; the URI is verbatim.
        assert not doc.source_uri.startswith("conversation://")

    async def test_session_uri_none_falls_back_to_content_hash(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        text = "Alice likes Python."
        doc = await load_conversation_document(text, _USER_ID, session_uri=None)

        assert doc is not None
        assert doc.source_uri == f"conversation://{_content_hash(text)}"

    async def test_rejects_empty_session_uri(self) -> None:
        with pytest.raises(ValueError, match="session_uri"):
            await load_conversation_document(
                "Alice likes Python.",
                _USER_ID,
                session_uri="   ",
            )

    async def test_same_session_uri_returns_none_on_second_call(self, mocker) -> None:
        # First call: find_one returns None → insert succeeds.
        # Second call: find_one returns the previously-inserted doc → None.
        existing = MagicMock()
        find_one_mock = mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            side_effect=[None, existing],
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        first = await load_conversation_document(
            "Same text", _USER_ID, session_uri="claude-session://abc"
        )
        second = await load_conversation_document(
            "Same text", _USER_ID, session_uri="claude-session://abc"
        )

        assert first is not None
        assert second is None
        assert find_one_mock.call_count == 2
        # Both queries used the supplied session_uri verbatim.
        for call in find_one_mock.call_args_list:
            assert call.args[0]["source_uri"] == "claude-session://abc"

    async def test_distinct_session_uris_produce_distinct_documents(
        self, mocker
    ) -> None:
        # Both queries return None — distinct source_uris mean distinct
        # rows under the (user_id, source_type, source_uri) unique index.
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        text = "byte-identical transcript"
        doc_a = await load_conversation_document(
            text, _USER_ID, session_uri="session://a"
        )
        doc_b = await load_conversation_document(
            text, _USER_ID, session_uri="session://b"
        )

        assert doc_a is not None
        assert doc_b is not None
        assert doc_a.source_uri == "session://a"
        assert doc_b.source_uri == "session://b"
        assert doc_a.source_uri != doc_b.source_uri


class TestSessionStartedAt:
    """Phase-2: session_started_at validation + metadata round-trip."""

    async def test_naive_datetime_rejected(self) -> None:
        naive = datetime(2026, 5, 17, 14, 30, 0)  # noqa: DTZ001 — intentional
        assert naive.tzinfo is None  # sanity

        with pytest.raises(ValueError, match="timezone-aware"):
            await load_conversation_document(
                "Alice likes Python.",
                _USER_ID,
                session_started_at=naive,
            )

    async def test_tz_aware_utc_roundtrips_to_metadata(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        started_at = datetime(2026, 5, 17, 14, 30, 0, tzinfo=UTC)
        doc = await load_conversation_document(
            "Alice likes Python.",
            _USER_ID,
            session_started_at=started_at,
        )

        assert doc is not None
        assert "session_started_at" in doc.metadata
        stored = doc.metadata["session_started_at"]
        assert isinstance(stored, datetime)
        assert stored.tzinfo is not None
        assert stored == started_at

    async def test_non_utc_tz_aware_normalized_to_utc(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        # 14:30 in UTC+02:00 == 12:30 UTC.
        plus_two = timezone(timedelta(hours=2))
        started_at = datetime(2026, 5, 17, 14, 30, 0, tzinfo=plus_two)
        doc = await load_conversation_document(
            "Alice likes Python.",
            _USER_ID,
            session_started_at=started_at,
        )

        assert doc is not None
        stored = doc.metadata["session_started_at"]
        assert stored.tzinfo is UTC
        assert stored == datetime(2026, 5, 17, 12, 30, 0, tzinfo=UTC)

    async def test_no_session_started_at_means_empty_metadata(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch("tree.data.conversation.Document.insert", new_callable=AsyncMock)

        doc = await load_conversation_document("Alice likes Python.", _USER_ID)

        assert doc is not None
        assert doc.metadata == {}


class TestLongTranscriptChunker:
    """Smoke test: the existing chunker handles ~50KB transcripts cleanly.

    This is a regression guard, not a behavior change. If the chunker
    misbehaves on long input today, a separate bug task is filed before
    Phase-2 ships. (Per the groomed spec for #026.)
    """

    def test_50kb_transcript_chunks_cleanly(self) -> None:
        from tree.config.app_config import app_config
        from tree.memory.extraction.core import _ENCODER, chunk_document

        # Build a ~50KB transcript out of varied repeated lines so the
        # tokenizer can't trivially collapse it.
        line = (
            "Alice: hey, did you finish the report? "
            "Bob: yes — pushed it this morning to the shared drive.\n"
        )
        text = (line * 600)[:50_000]
        assert 49_000 <= len(text) <= 50_000

        chunks = chunk_document(text)

        # (a) non-empty + bounded count.
        assert len(chunks) > 0
        assert len(chunks) <= 200
        # (b) every chunk non-empty.
        for chunk in chunks:
            assert chunk.strip(), "chunker emitted an empty chunk"
        # (c) every chunk within (chunk_size + chunk_overlap) tokens.
        #     The chunker operates on tokens, not characters — so the AC's
        #     "chars" wording is interpreted as tokens (the unit the
        #     splitter actually bounds). chunk_size=512, overlap=64 →
        #     max-tokens-per-chunk = 512.
        max_tokens = app_config.extraction.chunk_size
        for chunk in chunks:
            token_count = len(_ENCODER.encode(chunk))
            assert token_count <= max_tokens, (
                f"chunk exceeds bound: {token_count} > {max_tokens}"
            )
