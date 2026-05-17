"""Integration tests for the ``Document`` compound unique index from #018.

Asserts the live behavior of the
``(user_id, source_type, source_uri)`` unique index against a real
MongoDB instance:

* Same ``(source_type, source_uri)`` under two distinct ``user_id``s
  both insert successfully (different tenants, same external URL).
* Re-inserting the exact same triple raises ``DuplicateKeyError``.
"""

from __future__ import annotations

import pymongo.errors
import pytest
from beanie import PydanticObjectId

from tree.entities.documents import Document, SourceType


class TestDocumentCompoundUniqueIndex:
    async def test_same_uri_under_two_users_both_insert(self) -> None:
        user_a = PydanticObjectId()
        user_b = PydanticObjectId()
        uri = "https://example.com/p/shared-article"

        doc_a = Document(
            source_type=SourceType.SUBSTACK,
            source_uri=uri,
            user_id=user_a,
            title="A's copy",
        )
        doc_b = Document(
            source_type=SourceType.SUBSTACK,
            source_uri=uri,
            user_id=user_b,
            title="B's copy",
        )

        await doc_a.insert()
        await doc_b.insert()  # MUST succeed — different tenant, same URI.

        assert doc_a.id is not None
        assert doc_b.id is not None
        assert doc_a.user_id == user_a
        assert doc_b.user_id == user_b

    async def test_same_triple_raises_duplicate_key(self) -> None:
        user_id = PydanticObjectId()
        uri = "https://example.com/p/duplicate-article"

        first = Document(
            source_type=SourceType.SUBSTACK,
            source_uri=uri,
            user_id=user_id,
            title="First",
        )
        await first.insert()

        second = Document(
            source_type=SourceType.SUBSTACK,
            source_uri=uri,
            user_id=user_id,
            title="Second",
        )

        with pytest.raises(pymongo.errors.DuplicateKeyError):
            await second.insert()

    async def test_same_uri_different_source_type_under_one_user(self) -> None:
        # The compound unique includes source_type, so the same URI under
        # different types is allowed — though unusual, it's a real edge
        # case (e.g., the same article archived as both WEB and LATENT).
        user_id = PydanticObjectId()
        uri = "https://example.com/multi-source"

        doc_web = Document(
            source_type=SourceType.WEB,
            source_uri=uri,
            user_id=user_id,
        )
        doc_latent = Document(
            source_type=SourceType.LATENT,
            source_uri=uri,
            user_id=user_id,
        )

        await doc_web.insert()
        await doc_latent.insert()

        assert doc_web.id is not None
        assert doc_latent.id is not None
