"""Shared CLI glue: resolve which user a pipeline / query entrypoint runs for.

Thin wrapper over :func:`tree.entities.sessions.resolve_user` that applies the
common precedence used by every ``run-*`` / ``query-*`` entrypoint:

    --user-id  >  --user-identifier  >  USER_ID env  >  USER_IDENTIFIER env
              >  the current-session user

So with nothing passed, work runs as the active user pinned in the ``sessions``
collection; pass ``USER_ID`` / ``USER_IDENTIFIER`` to override. Mongo must
already be initialised by the caller (the resolver reads the ``users`` /
``sessions`` collections). Exits 1 with an actionable message on failure.
"""

import logging
import os

from beanie import PydanticObjectId

from tree.entities.sessions import resolve_user

logger = logging.getLogger(__name__)


async def resolve_user_id(
    user_id: str | None, user_identifier: str | None
) -> PydanticObjectId:
    """Resolve the target user's ``_id`` from CLI args + env, or exit 1."""

    try:
        user = await resolve_user(
            user_id=user_id or os.environ.get("USER_ID"),
            user_identifier=user_identifier or os.environ.get("USER_IDENTIFIER"),
        )
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc

    logger.info("Resolved target user: id=%s identifier=%s", user.id, user.identifier)
    return user.id
