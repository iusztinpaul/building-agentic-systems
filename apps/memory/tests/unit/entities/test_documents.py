from datetime import datetime, timezone

import pytest
from beanie import PydanticObjectId
from pydantic import ValidationError
from pymongo import IndexModel

from tree.entities.documents import Document, SourceType


def _user_id() -> PydanticObjectId:
    return PydanticObjectId()


class TestDocumentModel:
    async def test_valid_document(self):
        doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://example.com/p/test-article",
            user_id=_user_id(),
            title="Test Article",
            summary="A test article about AI.",
            content="Full article content here.",
            authors=["Paul Iusztin"],
            date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        assert doc.source_type == SourceType.SUBSTACK
        assert doc.source_uri == "https://example.com/p/test-article"
        assert doc.references == []
        assert doc.user_id is not None

    async def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            Document(source_type=SourceType.SUBSTACK)

    async def test_missing_user_id_raises(self):
        # Per #018: user_id is required at construction (no default).
        with pytest.raises(ValidationError):
            Document(
                source_type=SourceType.SUBSTACK,
                source_uri="https://example.com/p/missing-user",
            )

    async def test_multiple_authors(self):
        doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://example.com/p/test-article",
            user_id=_user_id(),
            title="Test Article",
            summary="A test article.",
            content="Content.",
            authors=["Author One", "Author Two"],
            date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )

        assert len(doc.authors) == 2

    async def test_latent_document(self):
        doc = Document(
            source_type=SourceType.LATENT,
            source_uri="https://example.com/some-external-link",
            user_id=_user_id(),
        )

        assert doc.source_type == SourceType.LATENT
        assert doc.title is None
        assert doc.summary is None
        assert doc.content is None
        assert doc.authors == []
        assert doc.date is None
        assert doc.references == []


class TestDocumentIngestError:
    """``ingest_error`` is the persisted-failure marker (ADR-004 §6): a
    normalized ``"<code>: <message>"`` string on a row whose ``content`` is
    ``None``. Nullable, unindexed, written only by the YouTube path.
    """

    async def test_ingest_error_defaults_to_none(self):
        doc = Document(
            source_type=SourceType.YOUTUBE,
            source_uri="https://www.youtube.com/watch?v=eYaWxljC4sA",
            user_id=_user_id(),
        )

        assert doc.ingest_error is None

    async def test_ingest_error_round_trips_normalized_code_message_string(self):
        error = "no_transcript: no transcript available from either backend"
        doc = Document(
            source_type=SourceType.YOUTUBE,
            source_uri="https://www.youtube.com/watch?v=eYaWxljC4sA",
            user_id=_user_id(),
            content=None,
            ingest_error=error,
        )

        restored = Document.model_validate(doc.model_dump())

        assert restored.ingest_error == error
        assert restored.content is None

    def test_ingest_error_is_not_indexed(self) -> None:
        index_models: list[IndexModel] = list(Document.Settings.indexes)

        indexed_keys = {
            key for im in index_models for key in im.document.get("key", {})
        }
        assert "ingest_error" not in indexed_keys


class TestDocumentCompoundUniqueIndex:
    """The **Document**'s natural key ``(user_id, source_uri)`` is the ONE
    compound unique index (ADR-010) — the same URI is allowed for different
    tenants; the same ``(user_id, source_uri)`` is not, whatever its
    ``source_type``.
    """

    def test_settings_declares_the_natural_key_as_the_only_unique_index(
        self,
    ) -> None:
        index_models: list[IndexModel] = list(Document.Settings.indexes)

        unique = [im for im in index_models if im.document.get("unique") is True]

        assert len(unique) == 1, f"Expected one unique index; got {index_models}"
        assert list(unique[0].document["key"].items()) == [
            ("user_id", 1),
            ("source_uri", 1),
        ]
        assert unique[0].document["name"] == "user_source_uri_unique"

    def test_source_type_is_not_part_of_any_index_key(self) -> None:
        # A ``LATENT`` placeholder is upgraded IN PLACE by rewriting its
        # ``source_type``, so with it in the key a LATENT and a real row for
        # the same URI could coexist (ADR-010).
        index_models: list[IndexModel] = list(Document.Settings.indexes)

        indexed_keys = {
            key for im in index_models for key in im.document.get("key", {})
        }

        assert "source_type" not in indexed_keys

    def test_no_inline_unique_on_source_uri(self) -> None:
        # The previous ``Indexed(str, unique=True)`` annotation has been
        # removed in favour of the compound index above.
        annotation = Document.model_fields["source_uri"].annotation
        # The plain ``str`` annotation has no Beanie ``Indexed`` metadata.
        assert annotation is str
