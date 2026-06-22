"""Current-user session pointer.

Tree is a single-operator personal assistant, so "the current user" is a
**singleton**: at most one row in the ``sessions`` collection, pinned to a
fixed ``_id`` (:data:`CURRENT_SESSION_ID`) so every write overwrites the same
document instead of accumulating rows. It names which :class:`~tree.entities.users.User`
is active for CLI / ops / harness flows — everything that needs "who am I?"
reads it back from Mongo via :func:`get_current_user`.

The pointer is intentionally thin: it stores only the referenced ``user_id``
(plus an ``updated_at`` stamp). The user's actual identity and attributes live
on the ``User`` document; this collection just records the selection.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document as BeanieDocument
from beanie import PydanticObjectId
from pydantic import Field

from tree.entities.users import User

# Fixed primary key of the singleton session document. There is only ever one.
CURRENT_SESSION_ID = "current"


class Session(BeanieDocument):
    """Singleton pointer to the active user.

    Keyed by the constant string ``_id = "current"`` so the collection holds
    at most one document; selecting a different user overwrites it rather than
    inserting a new row.
    """

    id: str = Field(default=CURRENT_SESSION_ID)
    user_id: PydanticObjectId
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "sessions"


async def set_current_user(user_id: PydanticObjectId) -> Session:
    """Pin ``user_id`` as the current user (idempotent upsert of the singleton).

    Uses a ``$set`` upsert keyed by the fixed ``_id`` so repeated calls just
    overwrite the pointer — there is never more than one session row.
    """

    now = datetime.now(UTC)
    collection = Session.get_pymongo_collection()
    await collection.update_one(
        {"_id": CURRENT_SESSION_ID},
        {"$set": {"user_id": user_id, "updated_at": now}},
        upsert=True,
    )
    return Session(id=CURRENT_SESSION_ID, user_id=user_id, updated_at=now)


async def get_current_user_id() -> PydanticObjectId | None:
    """Return the current user's ``_id``, or ``None`` if none is set."""

    session = await Session.get(CURRENT_SESSION_ID)
    return session.user_id if session is not None else None


async def get_current_user() -> User | None:
    """Resolve the current user document, or ``None`` if unset / dangling.

    Returns ``None`` both when no session pointer exists and when it references
    a ``User`` that no longer exists (a dangling pointer).
    """

    user_id = await get_current_user_id()
    if user_id is None:
        return None
    return await User.get(user_id)


async def get_user_by_identifier(identifier: str) -> User | None:
    """Look up a user by their stable handle, or ``None`` if absent."""

    return await User.find_one(User.identifier == identifier)


async def resolve_user(
    *, user_id: str | None = None, user_identifier: str | None = None
) -> User:
    """Resolve which user to act as, defaulting to the current-session user.

    Precedence (first hit wins): explicit ``user_id`` > ``user_identifier`` >
    the current-session user (:func:`get_current_user`). This is the shared
    policy for the operator entrypoints: with neither override, work runs as
    the active user pinned in the ``sessions`` collection.

    Raises :class:`ValueError` with an actionable message when the requested
    user can't be found, or when no override is given and no current user is
    set — so callers never silently act on the wrong tenant.
    """

    if user_id:
        try:
            oid = PydanticObjectId(user_id)
        except Exception as exc:  # noqa: BLE001 — surface the raw input.
            raise ValueError(
                f"user_id {user_id!r} is not a valid Mongo ObjectId"
            ) from exc
        user = await User.get(oid)
        if user is None:
            raise ValueError(f"No user found for user_id={user_id}")
        return user

    if user_identifier:
        user = await get_user_by_identifier(user_identifier)
        if user is None:
            raise ValueError(f"No user found for identifier={user_identifier!r}")
        return user

    user = await get_current_user()
    if user is None:
        raise ValueError(
            "No user_id/user_identifier given and no current user is set. "
            "Run `make memory-signup` or `make memory-set-current-user` first."
        )
    return user
