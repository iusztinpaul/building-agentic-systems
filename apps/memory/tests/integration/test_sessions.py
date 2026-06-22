"""Integration tests for ``tree.entities.sessions`` — the current-user pointer.

These hit a real Mongo (the ``mongo_client`` session fixture initialises Beanie
against ``TEST_DATABASE``; the autouse ``_clean_collections`` fixture wipes every
registered collection — including ``sessions`` — between tests). The behaviour
under test is inherently a round-trip to the DB, so per the project's testing
rules it belongs in integration, not a mocked unit test.
"""

from __future__ import annotations

from beanie import PydanticObjectId

import pytest

from tree.entities.sessions import (
    CURRENT_SESSION_ID,
    Session,
    get_current_user,
    get_current_user_id,
    get_user_by_identifier,
    resolve_user,
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


async def test_get_user_by_identifier_returns_the_user(mongo_client) -> None:
    # Arrange
    user = User(identifier="by-id@example.com")
    await user.insert()

    # Act
    found = await get_user_by_identifier("by-id@example.com")

    # Assert
    assert found is not None
    assert found.id == user.id


async def test_get_user_by_identifier_returns_none_when_absent(mongo_client) -> None:
    # Arrange / Act
    found = await get_user_by_identifier("nobody@example.com")

    # Assert
    assert found is None


async def test_resolve_user_by_user_id(mongo_client) -> None:
    # Arrange
    user = User(identifier="resolve-id@example.com")
    await user.insert()

    # Act
    resolved = await resolve_user(user_id=str(user.id))

    # Assert
    assert resolved.id == user.id


async def test_resolve_user_by_identifier(mongo_client) -> None:
    # Arrange
    user = User(identifier="resolve-handle@example.com")
    await user.insert()

    # Act
    resolved = await resolve_user(user_identifier="resolve-handle@example.com")

    # Assert
    assert resolved.id == user.id


async def test_resolve_user_defaults_to_current_session_user(mongo_client) -> None:
    # Arrange
    user = User(identifier="current@example.com")
    await user.insert()
    await set_current_user(user.id)

    # Act — no override given.
    resolved = await resolve_user()

    # Assert
    assert resolved.id == user.id


async def test_resolve_user_id_takes_precedence_over_identifier_and_current(
    mongo_client,
) -> None:
    # Arrange — current + a different identifier both point elsewhere.
    target = User(identifier="target@example.com")
    other = User(identifier="other@example.com")
    await target.insert()
    await other.insert()
    await set_current_user(other.id)

    # Act — explicit user_id must win over identifier and the current pointer.
    resolved = await resolve_user(
        user_id=str(target.id), user_identifier="other@example.com"
    )

    # Assert
    assert resolved.id == target.id


async def test_resolve_user_identifier_takes_precedence_over_current(
    mongo_client,
) -> None:
    # Arrange
    target = User(identifier="pick-me@example.com")
    current = User(identifier="current2@example.com")
    await target.insert()
    await current.insert()
    await set_current_user(current.id)

    # Act
    resolved = await resolve_user(user_identifier="pick-me@example.com")

    # Assert
    assert resolved.id == target.id


async def test_resolve_user_raises_for_invalid_user_id(mongo_client) -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="ObjectId"):
        await resolve_user(user_id="not-an-objectid")


async def test_resolve_user_raises_for_unknown_user_id(mongo_client) -> None:
    # Arrange / Act / Assert — well-formed id, no such user.
    with pytest.raises(ValueError, match="No user found"):
        await resolve_user(user_id=str(PydanticObjectId()))


async def test_resolve_user_raises_for_unknown_identifier(mongo_client) -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="No user found"):
        await resolve_user(user_identifier="ghost@example.com")


async def test_resolve_user_raises_when_nothing_set(mongo_client) -> None:
    # Arrange / Act / Assert — no override, no current user.
    with pytest.raises(ValueError, match="no current user"):
        await resolve_user()
