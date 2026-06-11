"""Entry point for the Tree Memory MCP server.

Phase 1 of multi-tenancy pins the server to a single ``user_id`` at
boot. Resolution order (first hit wins):

1. ``--user-id <ObjectId>`` — used as-is.
2. ``--identifier <handle>`` or env ``TREE_USER_IDENTIFIER`` — looked up
   via ``User.find_one({"identifier": ...})``.
3. Neither set → :class:`RuntimeError`.

The user MUST already exist; the server does not auto-create (see
``scripts/signup.py`` / ``make memory-signup`` for seeding).

Usage:
    make memory-serve-mcp USER_ID=507f1f77bcf86cd799439011
    TREE_USER_IDENTIFIER=paul@example.com make memory-serve-mcp
    uv run python scripts/serve_mcp.py --user-id 507f...
    uv run python scripts/serve_mcp.py --identifier paul@example.com --transport http
"""

from __future__ import annotations

import asyncio
import logging
import os

import click
from beanie import PydanticObjectId

from tree.config.settings import settings
from tree.db import init_mongodb
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)


async def _resolve_and_pin(
    user_id: str | None, identifier: str | None
) -> PydanticObjectId:
    """Connect to Mongo, resolve the user_id, pin it on the server module.

    Initialising the Mongo connection here (rather than inside the
    FastMCP lifespan) is necessary because :func:`User.find_one` needs
    Beanie to be wired up before we can look the user up by identifier.
    The lifespan re-uses the same connection, so no extra cost.
    """

    from tree.mcp.server import _resolve_server_user_id, set_server_user_id

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    parsed_user_id: PydanticObjectId | None = None
    if user_id:
        try:
            parsed_user_id = PydanticObjectId(user_id)
        except Exception as exc:  # noqa: BLE001 — surface raw input.
            raise SystemExit(
                f"--user-id {user_id!r} is not a valid Mongo ObjectId: {exc}"
            ) from exc

    resolved = await _resolve_server_user_id(
        user_id=parsed_user_id, identifier=identifier
    )
    set_server_user_id(resolved)
    return resolved


@click.command()
@click.option(
    "--user-id",
    default=None,
    help=(
        "Mongo ObjectId of the user the server runs under. One of "
        "``--user-id`` / ``--identifier`` / ``TREE_USER_IDENTIFIER`` is "
        "required."
    ),
)
@click.option(
    "--identifier",
    default=None,
    help=(
        "Stable user identifier (e.g. email). Resolved to ``_id`` via "
        '``User.find_one({"identifier": ...})``. Falls back to the '
        "``TREE_USER_IDENTIFIER`` env var when omitted."
    ),
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http", "sse", "streamable-http"]),
    default=None,
    help="Transport protocol to use. Defaults to FastMCP's stdio.",
)
def main(
    user_id: str | None,
    identifier: str | None,
    transport: str | None,
) -> None:
    """Resolve the boot-pinned user_id, then start the MCP server."""

    # The env var is the conventional fallback for --identifier so the
    # Makefile / docker-compose can set it once without re-typing.
    effective_identifier = identifier or os.environ.get("TREE_USER_IDENTIFIER")

    resolved = asyncio.run(_resolve_and_pin(user_id, effective_identifier))
    logger.info("MCP server pinned to user_id=%s; starting…", resolved)

    # Import the server lazily so the module-level ``_SERVER_USER_ID`` we
    # just set is what the lifespan reads.
    from tree.mcp.server import mcp

    if transport:
        mcp.run(transport=transport)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
