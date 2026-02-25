from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from twin.entities.documents import Document, SourceType


class TestDocumentModel:
    async def test_valid_document(self):
        doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://example.com/p/test-article",
            title="Test Article",
            summary="A test article about AI.",
            content="Full article content here.",
            authors=["Paul Iusztin"],
            date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        assert doc.source_type == SourceType.SUBSTACK
        assert doc.source_uri == "https://example.com/p/test-article"
        assert doc.summary_embedding == []
        assert doc.references == []

    async def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            Document(
                source_type=SourceType.SUBSTACK,
                source_uri="https://example.com/p/test",
            )

    async def test_with_embedding_and_references(self):
        doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://example.com/p/test-article",
            title="Test Article",
            summary="A test article.",
            summary_embedding=[0.1, 0.2, 0.3],
            content="Content.",
            authors=["Author One", "Author Two"],
            date=datetime(2026, 2, 1, tzinfo=timezone.utc),
            references=["https://ref1.com", "https://ref2.com"],
        )

        assert doc.summary_embedding == [0.1, 0.2, 0.3]
        assert len(doc.authors) == 2
        assert len(doc.references) == 2
