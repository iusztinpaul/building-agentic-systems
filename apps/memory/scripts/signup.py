"""Fictive sign-up + current-user CLI.

Tree has no auth wiring yet, so this script is how users get created and how we
record which one is "current". It is the operator-facing front door to the
``users`` and ``sessions`` collections:

* ``signup``      — create a :class:`~tree.entities.users.User` (idempotent on
                    ``identifier``) and, by default, set it as the current user.
* ``set-current`` — point the current-user session at an existing user.
* ``whoami``      — print the current user.

Every command resolves Mongo from ``settings`` (so it follows the ``.env`` /
``.env.prod`` target switch) and prints the user's ObjectId — the value the
pipeline ``run-*`` targets need as ``USER_ID``.

Usage::

    make memory-signup USER_IDENTIFIER=me@example.com NAME="Paul Iusztin"
    make memory-set-current-user USER_IDENTIFIER=me@example.com
    make memory-whoami
"""

import asyncio
import logging

import click
from beanie import PydanticObjectId

from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.sessions import get_current_user, set_current_user
from tree.entities.users import User
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)


async def _connect() -> None:
    """Initialise Beanie against the configured (target-switched) Mongo."""

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )


async def _signup(identifier: str, name: str | None, make_current: bool) -> None:
    await _connect()

    user = await User.find_one({"identifier": identifier})
    if user is None:
        attributes = {"name": name} if name else {}
        user = User(identifier=identifier, attributes=attributes)
        await user.insert()
        logger.info("Created user identifier=%s id=%s", identifier, user.id)
    else:
        logger.info("User already exists identifier=%s id=%s", identifier, user.id)

    if make_current:
        await set_current_user(user.id)
        logger.info("Set current user -> id=%s", user.id)

    click.echo(str(user.id))


async def _set_current(identifier: str | None, user_id: str | None) -> None:
    await _connect()

    if user_id is not None:
        user = await User.get(PydanticObjectId(user_id))
    else:
        user = await User.find_one({"identifier": identifier})

    if user is None:
        raise click.ClickException(
            f"No user found for {'user_id=' + user_id if user_id else 'identifier=' + str(identifier)}."
        )

    await set_current_user(user.id)
    logger.info("Set current user -> id=%s", user.id)
    click.echo(str(user.id))


async def _whoami() -> None:
    await _connect()

    user = await get_current_user()
    if user is None:
        raise click.ClickException(
            "No current user is set. Run `signup` or `set-current` first."
        )

    name = user.attributes.get("name", user.identifier)
    click.echo(f"{user.id}\t{user.identifier}\t{name}")


@click.group()
def cli() -> None:
    """User sign-up and current-user session management."""


@cli.command()
@click.option("--user-identifier", required=True, help="Stable handle (e.g. email).")
@click.option("--name", default=None, help="Display name stored in attributes.name.")
@click.option(
    "--set-current/--no-set-current",
    default=True,
    help="Set the new user as the current user (default: yes).",
)
def signup(user_identifier: str, name: str | None, set_current: bool) -> None:
    """Create a user (idempotent on identifier) and optionally set it current."""

    asyncio.run(_signup(user_identifier, name, set_current))


@cli.command("set-current")
@click.option("--user-identifier", default=None, help="Select the user by identifier.")
@click.option("--user-id", default=None, help="Select the user by ObjectId.")
def set_current_command(user_identifier: str | None, user_id: str | None) -> None:
    """Point the current-user session at an existing user."""

    if not user_identifier and not user_id:
        raise click.UsageError("Pass --user-identifier or --user-id.")
    asyncio.run(_set_current(user_identifier, user_id))


@cli.command()
def whoami() -> None:
    """Print the current user (id, identifier, name)."""

    asyncio.run(_whoami())


if __name__ == "__main__":
    cli()
