"""Integration tests for ``tree.entities.sessions`` — the current-user pointer.

These hit a real Mongo (the ``mongo_client`` session fixture initialises Beanie
against ``TEST_DATABASE``; the autouse ``_clean_collections`` fixture wipes every
registered collection — including ``sessions`` — between tests). The behaviour
under test is inherently a round-trip to the DB, so per the project's testing
rules it belongs in integration, not a mocked unit test.
"""

from __future__ import annotations

from beanie import PydanticObjectId

from tree.entities.sessions import (
    CURRENT_SESSION_ID,
    Session,
    get_current_user,
    get_current_user_id,
    set_current_user,
)
from tree.entities.users import User


async def test_get_current_user_id_returns_none_when_unset(mongo_client) -> None:
    # Arrange / Act
    result = await get_current_user_id()

    # Assert
    assert result is None


async def test_get_current_user_returns_none_when_unset(mongo_client) -> None:
    # Arrange / Act
    result = await get_current_user()

    # Assert
    assert result is None


async def test_set_current_user_pins_pointer(mongo_client) -> None:
    # Arrange
    user_id = PydanticObjectId()

    # Act
    await set_current_user(user_id)

    # Assert
    assert await get_current_user_id() == user_id


async def test_set_current_user_is_singleton(mongo_client) -> None:
    # Arrange
    first = PydanticObjectId()
    second = PydanticObjectId()

    # Act — selecting a second user overwrites the pointer, never appends.
    await set_current_user(first)
    await set_current_user(second)

    # Assert — exactly one session row, pinned to the latest user.
    sessions = await Session.find_all().to_list()
    assert len(sessions) == 1
    assert sessions[0].id == CURRENT_SESSION_ID
    assert await get_current_user_id() == second


async def test_get_current_user_resolves_the_user_document(mongo_client) -> None:
    # Arrange
    user = User(identifier="session-it@example.com", attributes={"name": "Session IT"})
    await user.insert()
    await set_current_user(user.id)

    # Act
    current = await get_current_user()

    # Assert
    assert current is not None
    assert current.id == user.id
    assert current.identifier == "session-it@example.com"


async def test_get_current_user_returns_none_for_dangling_pointer(mongo_client) -> None:
    # Arrange — point at a user id that has no User document.
    await set_current_user(PydanticObjectId())

    # Act
    current = await get_current_user()

    # Assert
    assert current is None
