from datetime import datetime, timezone

from twin.data.substack_rss import SubstackRSSFeedETL
from twin.entities.documents import Document, SourceType


class TestDocumentIntegration:
    async def test_insert_and_read_round_trip(self, mongo_client):
        ref = Document(
            source_type=SourceType.LATENT,
            source_uri="https://ref.com",
        )
        await ref.insert()

        doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://example.com/p/round-trip",
            title="Round Trip Test",
            summary="Testing insert and read.",
            summary_embedding=[0.1, 0.2],
            content="Full content here.",
            authors=["Paul Iusztin"],
            date=datetime(2026, 3, 1, tzinfo=timezone.utc),
            references=[ref],
        )
        await doc.insert()

        fetched = await Document.find_one(
            Document.source_uri == doc.source_uri, fetch_links=True
        )

        assert fetched is not None
        assert fetched.id == doc.id
        assert fetched.source_type == SourceType.SUBSTACK
        assert fetched.source_uri == "https://example.com/p/round-trip"
        assert fetched.title == "Round Trip Test"
        assert fetched.summary == "Testing insert and read."
        assert fetched.summary_embedding == [0.1, 0.2]
        assert fetched.content == "Full content here."
        assert fetched.authors == ["Paul Iusztin"]
        assert fetched.date == datetime(2026, 3, 1, tzinfo=timezone.utc)
        assert len(fetched.references) == 1
        assert fetched.references[0].source_uri == "https://ref.com"
        assert fetched.references[0].source_type == SourceType.LATENT


class TestResolveReferences:
    async def test_creates_latent_documents(self, mongo_client):
        etl = SubstackRSSFeedETL()
        refs = ["https://external.com/a", "https://external.com/b"]
        ref_docs = await etl._resolve_references(refs)

        assert len(ref_docs) == 2
        for doc in ref_docs:
            assert doc.source_type == SourceType.LATENT
            assert doc.id is not None

        for uri in refs:
            fetched = await Document.find_one(Document.source_uri == uri)
            assert fetched is not None
            assert fetched.source_type == SourceType.LATENT

    async def test_returns_existing_document(self, mongo_client):
        etl = SubstackRSSFeedETL()
        existing = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://existing.com/article",
            title="Existing",
            content="Content.",
        )
        await existing.insert()

        ref_docs = await etl._resolve_references(["https://existing.com/article"])

        assert len(ref_docs) == 1
        assert ref_docs[0].id == existing.id
        assert ref_docs[0].source_type == SourceType.SUBSTACK
        assert ref_docs[0].title == "Existing"
