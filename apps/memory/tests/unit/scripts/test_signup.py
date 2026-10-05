"""``scripts/signup.py`` keeps the case-insensitive **Request user** match unambiguous.

The MCP seam looks ``User.identifier`` up case-insensitively (ADR-014 §1), so
signup stores NEW identifiers stripped + lowercased and refuses one that
differs from an existing row only in case. Rows stored before that rule keep
their case and are still found. Runs against the real ``users`` collection of
the unit-test database; only the script's own Mongo connect is stubbed so it
stays on that database.
"""

import uuid
from collections.abc import AsyncIterator

import click
import pytest
from beanie import PydanticObjectId

from tree.entities.memory import MemoryEntry
from tree.entities.users import User


@pytest.fixture
def cli_module(mocker):
    """Import the script lazily; keep it on the unit-test database."""

    import scripts.signup as module

    mocker.patch.object(module, "_connect", new=mocker.AsyncMock())
    return module


@pytest.fixture
async def local_part() -> AsyncIterator[str]:
    """A unique identifier stem; every ``users`` row built on it is deleted after."""

    stem = f"paul-{uuid.uuid4().hex[:10]}"
    yield stem

    users = User.get_pymongo_collection()
    pattern = {"identifier": {"$regex": f"^{stem}@", "$options": "i"}}
    ids = [doc["_id"] async for doc in users.find(pattern, {"_id": 1})]
    await users.delete_many({"_id": {"$in": ids}})
    await MemoryEntry.get_pymongo_collection().delete_many({"user_id": {"$in": ids}})


async def _rows(stem: str) -> list[str]:
    users = User.get_pymongo_collection()
    pattern = {"identifier": {"$regex": f"^{stem}@", "$options": "i"}}
    return [doc["identifier"] async for doc in users.find(pattern)]


async def test_signup_stores_the_identifier_stripped_and_lowercased(
    cli_module, local_part: str
) -> None:
    await cli_module._signup(f"  {local_part.title()}@Example.com ", None, False)

    assert await _rows(local_part) == [f"{local_part}@example.com"]


async def test_signup_refuses_a_case_only_variant_of_an_existing_row(
    cli_module, local_part: str
) -> None:
    await cli_module._signup(f"{local_part}@example.com", None, False)

    with pytest.raises(click.ClickException) as raised:
        await cli_module._signup(f"{local_part.upper()}@example.com", None, False)

    assert raised.value.message == (
        f"identifier '{local_part.upper()}@example.com' already exists as "
        f"'{local_part}@example.com'."
    )
    assert await _rows(local_part) == [f"{local_part}@example.com"]


async def test_signup_is_idempotent_on_the_stored_identifier(
    cli_module, local_part: str, capsys
) -> None:
    await cli_module._signup(f"{local_part}@example.com", None, False)
    first = capsys.readouterr().out

    await cli_module._signup(f"{local_part}@example.com", None, False)

    assert capsys.readouterr().out == first
    assert await _rows(local_part) == [f"{local_part}@example.com"]


@pytest.fixture
async def legacy_row(local_part: str) -> tuple[PydanticObjectId, str]:
    """A row stored BEFORE signup lowercased (raw insert, no hook)."""

    identifier = f"{local_part.title()}@Example.com"
    user_id = PydanticObjectId()
    await User.get_pymongo_collection().insert_one(
        {"_id": user_id, "identifier": identifier, "attributes": {}}
    )
    return user_id, identifier


async def test_signup_reruns_a_legacy_uppercase_row_idempotently(
    cli_module, legacy_row: tuple[PydanticObjectId, str], local_part: str, capsys
) -> None:
    user_id, identifier = legacy_row

    await cli_module._signup(identifier, None, False)

    assert capsys.readouterr().out.strip() == str(user_id)
    assert await _rows(local_part) == [identifier]


async def test_set_current_and_whoami_find_a_legacy_uppercase_row(
    cli_module, legacy_row: tuple[PydanticObjectId, str], local_part: str, capsys
) -> None:
    user_id, identifier = legacy_row

    await cli_module._set_current(f"{local_part}@example.com", None)
    await cli_module._whoami()

    set_out, whoami_out = capsys.readouterr().out.strip().splitlines()
    assert set_out == str(user_id)
    assert whoami_out.split("\t")[:2] == [str(user_id), identifier]
