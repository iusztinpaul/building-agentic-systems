"""Unit tests for tree.data.conversation.conversation — load_conversation_document."""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.config.app_config import app_config
from tree.data.conversation.conversation import (
    _content_hash,
    conversation_source_uri,
    load_conversation_document,
)
from tree.memory.rag.chunking import _ENCODER, split_document

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
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

        doc = await load_conversation_document("Alice likes Python.", _USER_ID)

        assert doc is not None
        assert doc.source_uri.startswith("conversation://")
        assert doc.content == "Alice likes Python."

    async def test_source_uri_is_deterministic(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

        doc1 = await load_conversation_document("Same text", _USER_ID)
        doc2 = await load_conversation_document("Same text", _USER_ID)

        assert doc1.source_uri == doc2.source_uri

    async def test_returns_none_for_duplicate(self, mocker) -> None:
        existing = MagicMock()
        mocker.patch(
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=existing,
        )

        result = await load_conversation_document("Already ingested text.", _USER_ID)
        assert result is None

    async def test_custom_title_used(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

        doc = await load_conversation_document("Text", _USER_ID, title="My Title")
        assert doc.title == "My Title"

    async def test_default_title_contains_timestamp(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

        doc = await load_conversation_document("Text", _USER_ID)
        assert doc.title.startswith("Conversation ")

    async def test_handles_duplicate_key_error_gracefully(self, mocker) -> None:
        from pymongo.errors import DuplicateKeyError

        mocker.patch(
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
            side_effect=DuplicateKeyError("duplicate"),
        )

        result = await load_conversation_document("Race condition text.", _USER_ID)
        assert result is None


class TestSourceUriDerivation:
    """Phase-2: source_uri rule — session_uri wins; else content-hash fallback."""

    async def test_session_uri_used_verbatim_when_provided(self, mocker) -> None:
        mocker.patch(
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

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
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

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
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            side_effect=[None, existing],
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

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
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

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
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

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
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

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
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

        doc = await load_conversation_document("Alice likes Python.", _USER_ID)

        assert doc is not None
        assert doc.metadata == {}


class TestLongTranscriptChunker:
    """Smoke test: the two-level splitter handles ~50KB transcripts cleanly.

    A regression guard, not a behavior change. Post-ADR-006 the chunker under
    test is ``tree.memory.rag.chunking.split_document`` driven by the live
    ``memory.chunking`` config — the same call the pipeline's task ① makes.
    """

    def test_50kb_transcript_chunks_cleanly(self) -> None:
        # Build a ~50KB transcript out of varied repeated lines so the
        # tokenizer can't trivially collapse it.
        line = (
            "Alice: hey, did you finish the report? "
            "Bob: yes — pushed it this morning to the shared drive.\n"
        )
        text = (line * 600)[:50_000]
        assert 49_000 <= len(text) <= 50_000

        parents = split_document(text, app_config.memory.chunking)

        # (a) non-empty + bounded count at both levels.
        children = [child for parent in parents for child in parent.children]
        assert len(parents) > 0
        assert len(parents) <= 200
        assert len(children) >= len(parents)
        # (b) every chunk non-empty.
        for parent in parents:
            assert parent.content.strip(), "chunker emitted an empty parent"
        for child in children:
            assert child.content.strip(), "chunker emitted an empty child"
        # (c) every chunk within its level's token budget. The splitter
        #     operates on tokens, not characters — so the AC's "chars" wording
        #     is interpreted as tokens (the unit the splitter actually bounds).
        chunking = app_config.memory.chunking
        for parent in parents:
            token_count = len(_ENCODER.encode(parent.content))
            assert token_count <= chunking.parent.size, (
                f"parent exceeds bound: {token_count} > {chunking.parent.size}"
            )
        for child in children:
            token_count = len(_ENCODER.encode(child.content))
            assert token_count <= chunking.child.size, (
                f"child exceeds bound: {token_count} > {chunking.child.size}"
            )


class TestConversationSourceUri:
    """``conversation_source_uri`` is the ONE derivation; the leaf calls it.

    Shared with the dispatcher's pre-flight duplicate lookup
    (``source_uri_for``), so the two can never disagree (ADR-008 §1).
    """

    def test_session_uri_is_used_verbatim(self) -> None:
        assert (
            conversation_source_uri("text", "claude-session://abc")
            == "claude-session://abc"
        )

    def test_no_session_uri_falls_back_to_the_content_hash(self) -> None:
        assert (
            conversation_source_uri("text", None)
            == f"conversation://{_content_hash('text')}"
        )

    @pytest.mark.parametrize("session_uri", ["", "   "], ids=["empty", "whitespace"])
    def test_blank_session_uri_raises(self, session_uri: str) -> None:
        with pytest.raises(ValueError, match="session_uri must not be empty"):
            conversation_source_uri("text", session_uri)

    @pytest.mark.parametrize(
        "session_uri", [None, "claude-session://abc"], ids=["hash", "session"]
    )
    async def test_leaf_derives_the_same_source_uri(
        self, mocker, session_uri: str | None
    ) -> None:
        mocker.patch(
            "tree.data.conversation.conversation.Document.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.conversation.conversation.Document.insert",
            new_callable=AsyncMock,
        )

        doc = await load_conversation_document(
            "Alice likes Python.", _USER_ID, session_uri=session_uri
        )

        assert doc is not None
        assert doc.source_uri == conversation_source_uri(
            "Alice likes Python.", session_uri
        )
