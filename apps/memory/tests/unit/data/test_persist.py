from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from tree.data.persist import insert_or_upgrade_latent
from tree.entities.documents import Document, SourceType

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_URI = "https://example.substack.com/p/linked-article"


def _real_doc() -> Document:
    return Document(
        source_type=SourceType.SUBSTACK,
        source_uri=_URI,
        user_id=_USER_ID,
        title="Linked article",
        content="Body.",
    )


def _stored(source_type: SourceType) -> Document:
    return Document(
        id=PydanticObjectId(),
        source_type=source_type,
        source_uri=_URI,
        user_id=_USER_ID,
    )


class TestInsertOrUpgradeLatent:
    async def test_a_clean_insert_returns_the_document(self, mocker) -> None:
        insert = mocker.patch.object(Document, "insert", new_callable=mocker.AsyncMock)
        find_one = mocker.patch.object(
            Document, "find_one", new_callable=mocker.AsyncMock
        )
        doc = _real_doc()

        result = await insert_or_upgrade_latent(doc)

        assert result is doc
        insert.assert_awaited_once()
        find_one.assert_not_awaited()

    async def test_a_latent_placeholder_that_won_the_race_is_upgraded_in_place(
        self, mocker
    ) -> None:
        # Another article in the same batch linked here and inserted a LATENT
        # placeholder between this loader's find_one and its insert.
        placeholder = _stored(SourceType.LATENT)
        mocker.patch.object(
            Document,
            "insert",
            new_callable=mocker.AsyncMock,
            side_effect=DuplicateKeyError("dup"),
        )
        mocker.patch.object(
            Document,
            "find_one",
            new_callable=mocker.AsyncMock,
            return_value=placeholder,
        )
        replace = mocker.patch.object(
            Document, "replace", new_callable=mocker.AsyncMock
        )
        doc = _real_doc()

        result = await insert_or_upgrade_latent(doc)

        assert result is doc
        assert doc.id == placeholder.id
        assert doc.source_type == SourceType.SUBSTACK
        replace.assert_awaited_once()

    async def test_a_real_row_that_won_the_race_is_a_clean_skip(self, mocker) -> None:
        mocker.patch.object(
            Document,
            "insert",
            new_callable=mocker.AsyncMock,
            side_effect=DuplicateKeyError("dup"),
        )
        mocker.patch.object(
            Document,
            "find_one",
            new_callable=mocker.AsyncMock,
            return_value=_stored(SourceType.WEB),
        )
        replace = mocker.patch.object(
            Document, "replace", new_callable=mocker.AsyncMock
        )

        result = await insert_or_upgrade_latent(_real_doc())

        assert result is None
        replace.assert_not_awaited()

    async def test_a_row_gone_by_the_reread_is_a_clean_skip(self, mocker) -> None:
        mocker.patch.object(
            Document,
            "insert",
            new_callable=mocker.AsyncMock,
            side_effect=DuplicateKeyError("dup"),
        )
        mocker.patch.object(
            Document, "find_one", new_callable=mocker.AsyncMock, return_value=None
        )
        replace = mocker.patch.object(
            Document, "replace", new_callable=mocker.AsyncMock
        )

        result = await insert_or_upgrade_latent(_real_doc())

        assert result is None
        replace.assert_not_awaited()
