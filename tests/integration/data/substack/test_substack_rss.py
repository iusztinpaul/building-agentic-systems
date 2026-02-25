from datetime import datetime, timezone

from twin.data.substack.substack_rss import load_document, resolve_references
from twin.entities.documents import Document, SourceType


class TestResolveReferences:
    async def test_creates_latent_documents(self, mongo_client):
        refs = ["https://external.com/a", "https://external.com/b"]
        ref_docs = await resolve_references(refs)

        assert len(ref_docs) == 2
        for doc in ref_docs:
            assert doc.source_type == SourceType.LATENT
            assert doc.id is not None

        for uri in refs:
            fetched = await Document.find_one(Document.source_uri == uri)
            assert fetched is not None
            assert fetched.source_type == SourceType.LATENT

    async def test_returns_existing_document(self, mongo_client):
        existing = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://existing.com/article",
            title="Existing",
            content="Content.",
        )
        await existing.insert()

        ref_docs = await resolve_references(["https://existing.com/article"])

        assert len(ref_docs) == 1
        assert ref_docs[0].id == existing.id
        assert ref_docs[0].source_type == SourceType.SUBSTACK
        assert ref_docs[0].title == "Existing"

    async def test_empty_uri_list(self, mongo_client):
        ref_docs = await resolve_references([])

        assert ref_docs == []

    async def test_mix_of_existing_and_new_uris(self, mongo_client):
        existing = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://known.com/article",
            title="Known",
            content="Known content.",
        )
        await existing.insert()

        ref_docs = await resolve_references(
            ["https://known.com/article", "https://brand-new.com/page"]
        )

        assert len(ref_docs) == 2
        assert ref_docs[0].id == existing.id
        assert ref_docs[0].source_type == SourceType.SUBSTACK
        assert ref_docs[1].source_type == SourceType.LATENT
        assert ref_docs[1].source_uri == "https://brand-new.com/page"


class TestLoadDocument:
    async def test_inserts_new_document(self, mongo_client):
        raw_entry = {
            "content": [{"value": '<p>See <a href="https://ref.com/a">link</a>.</p>'}],
        }
        doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://new-article.com/p/post",
            title="New Post",
            content="See link.",
            authors=["Author"],
            date=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )

        result = await load_document(doc, raw_entry)

        assert result is not None
        assert result.id is not None
        assert len(result.references) == 1
        assert result.references[0].source_uri == "https://ref.com/a"

        fetched = await Document.find_one(
            Document.source_uri == "https://new-article.com/p/post"
        )
        assert fetched is not None

    async def test_skips_non_latent_duplicate(self, mongo_client):
        existing = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://dup.com/p/article",
            title="Already Here",
            content="Original content.",
        )
        await existing.insert()

        doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://dup.com/p/article",
            title="Already Here",
            content="Original content.",
        )
        result = await load_document(doc, {"content": [{}]})

        assert result is None

    async def test_upgrades_latent_to_full(self, mongo_client):
        latent = Document(
            source_type=SourceType.LATENT,
            source_uri="https://upgrade.com/p/article",
        )
        await latent.insert()

        doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://upgrade.com/p/article",
            title="Full Article",
            content="Full content now available.",
            authors=["Author"],
            date=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        result = await load_document(doc, {"content": [{}]})

        assert result is not None
        assert result.id == latent.id
        assert result.source_type == SourceType.SUBSTACK
        assert result.title == "Full Article"

        fetched = await Document.find_one(
            Document.source_uri == "https://upgrade.com/p/article"
        )
        assert fetched.source_type == SourceType.SUBSTACK
        assert fetched.title == "Full Article"

    async def test_filters_self_references(self, mongo_client):
        self_uri = "https://self-ref.com/p/article"
        raw_entry = {
            "content": [
                {
                    "value": (
                        f'<p>Link to <a href="{self_uri}">self</a> '
                        'and <a href="https://other.com/page">other</a>.</p>'
                    )
                }
            ],
        }
        doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri=self_uri,
            title="Self Ref Article",
            content="Content.",
        )

        result = await load_document(doc, raw_entry)

        assert result is not None
        assert len(result.references) == 1
        assert result.references[0].source_uri == "https://other.com/page"
