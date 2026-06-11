"""FastMCP server with lifespan for MongoDB + model initialization.

Phase 1 of multi-tenancy pins the server to a **single** user at boot
time. The id is resolved once via :func:`_resolve_server_user_id` from
either the ``--user-id`` CLI flag or the ``TREE_USER_IDENTIFIER`` env
var, then stashed in the module-level :data:`_SERVER_USER_ID` constant.
Every MCP tool reads that constant (mirrored onto the lifespan context
as ``user_id`` so existing code paths keep working) and threads the
value down into the data / extraction / query layers. There is no
silent fallback to a default user — if neither input is set, the server
fails to boot with a clear error.

Future request-scoped sourcing (different ``user_id`` per call) is a
small refactor to a ``ContextVar``; this module is the only place that
needs to change.
"""

import logging
import os
import sys
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from beanie import PydanticObjectId
from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.users import User
from tree.memory.indexing.core import (
    assert_settings_match_live_vector_index,
    ensure_indexes,
)
from tree.models.get_model import get_embedding_model, get_llm
from tree.observability import configure_opik, flush_opik

logger = logging.getLogger(__name__)


# Module-level pin set by :func:`set_server_user_id` (called from the
# entrypoint, ``scripts/serve_mcp.py``, BEFORE ``mcp.run()``). The value
# is also mirrored into the lifespan context as ``user_id``. ``None``
# means "the server has not been initialised" — see
# :func:`get_server_user_id`.
_SERVER_USER_ID: PydanticObjectId | None = None


def set_server_user_id(user_id: PydanticObjectId) -> None:
    """Pin the server's user_id BEFORE the FastMCP lifespan kicks in.

    The entrypoint parses CLI args + env, resolves the id via
    :func:`_resolve_server_user_id`, and then calls this setter. Once
    set, every tool reads it via :func:`get_server_user_id` (directly or
    via the lifespan-context mirror).
    """

    global _SERVER_USER_ID
    _SERVER_USER_ID = user_id
    logger.info("MCP server user_id pinned to %s", user_id)


def get_server_user_id() -> PydanticObjectId:
    """Read the boot-pinned ``user_id``.

    Raises :class:`RuntimeError` if the server was never initialised
    via :func:`set_server_user_id`. Tools that want to fail loud rather
    than receive a ``None`` user_id can call this directly.
    """

    if _SERVER_USER_ID is None:
        raise RuntimeError(
            "MCP server user_id has not been initialised — "
            "call set_server_user_id() before mcp.run()."
        )
    return _SERVER_USER_ID


async def _resolve_server_user_id(
    *,
    user_id: PydanticObjectId | None,
    identifier: str | None,
) -> PydanticObjectId:
    """Resolve the server's pinned user_id.

    Resolution order (first hit wins; no silent fallback):

    1. ``user_id`` argument (parsed from ``--user-id <ObjectId>``) is
       returned as-is.
    2. ``identifier`` (from ``TREE_USER_IDENTIFIER=<email-or-handle>``)
       is looked up via ``User.find_one({"identifier": identifier})``.
       Missing user → :class:`RuntimeError` (the server does not
       auto-create — the #021 migration script seeds the user).
    3. Neither set → :class:`RuntimeError` ("server requires --user-id
       or TREE_USER_IDENTIFIER").
    """

    if user_id is not None:
        return user_id

    if identifier is not None:
        user = await User.find_one(User.identifier == identifier)
        if user is None:
            raise RuntimeError(
                f"MCP server boot: no User row with identifier={identifier!r}. "
                "Seed the user (run `make memory-signup`, see scripts/signup.py) "
                "before starting the server."
            )
        return user.id

    raise RuntimeError(
        "MCP server boot: neither --user-id nor TREE_USER_IDENTIFIER is set. "
        "Set one of them to the ObjectId / identifier of an existing User."
    )


async def _resolve_user_id_from_env() -> PydanticObjectId:
    """Convenience: resolve from CLI-less env-only context (e.g. tests).

    Reads ``TREE_USER_IDENTIFIER`` from the environment and delegates
    to :func:`_resolve_server_user_id`. Used by the lifespan as a last
    resort when the entrypoint never set the module-level pin (e.g.
    ``mcp dev`` style invocation that doesn't go through ``serve_mcp.py``).
    """

    identifier = os.environ.get("TREE_USER_IDENTIFIER") or None
    # ``user_id`` is explicitly absent here — only the env-var path is
    # available in this fallback. The resolver raises if both are unset.
    no_cli_user_id: PydanticObjectId | None = None
    return await _resolve_server_user_id(user_id=no_cli_user_id, identifier=identifier)


@lifespan
async def app_lifespan(server: FastMCP) -> AsyncGenerator[dict[str, Any], None]:
    """Initialize MongoDB, ML models, and the pinned user_id at startup.

    Also runs :func:`assert_settings_match_live_vector_index` so an
    embedding-dimension drift (settings vs the live Atlas Vector Search
    index) surfaces at boot rather than at first vector query.
    """

    # Configure Opik observability once at boot (no-op without OPIK_API_KEY).
    configure_opik()

    database = settings.mongo.mongo_initdb_database
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        database,
    )
    llm = get_llm()
    embedding_model = get_embedding_model()

    # Server-instance thread id: one harness session = one long-lived MCP server
    # process = one Opik thread. FastMCP's Context does not expose a stable
    # per-session id we can rely on across tool calls, so retrieval tools group
    # their traces under this UUID (mirrored onto the lifespan context). See the
    # SWE report for the thread-identity decision.
    thread_id = f"mcp-session-{uuid.uuid4()}"

    # ``_SERVER_USER_ID`` is normally pinned by ``serve_mcp.py`` before
    # ``mcp.run()`` ever fires the lifespan. If that didn't happen
    # (e.g. dev invocation skipping the entrypoint), fall back to the
    # env-only resolver — never to a magic default.
    global _SERVER_USER_ID
    if _SERVER_USER_ID is None:
        _SERVER_USER_ID = await _resolve_user_id_from_env()
    user_id = _SERVER_USER_ID

    # Ensure indexes (creates ``vector_index`` if absent), then assert
    # the live index dimension matches ``app_config.models.search_embedding.dimensions``. The
    # assertion is the loud-fail gate the plan calls for.
    #
    # On a serverless host (Prefect Horizon / Lambda) this boot work — creating
    # the Atlas vector index and polling mongot until it syncs — can run for
    # minutes and blocks port-8081 readiness past the 60s window, so the server
    # never starts. ``MCP_SKIP_INDEX_BOOTSTRAP=true`` skips it: indexes are
    # created by the indexing pipeline, and the MCP server only queries.
    if settings.mcp_skip_index_bootstrap:
        logger.info(
            "MCP_SKIP_INDEX_BOOTSTRAP set — skipping ensure_indexes + "
            "vector-index assertion (fast serverless boot)."
        )
    else:
        await ensure_indexes(
            client, database, embedding_model=embedding_model, user_id=user_id
        )
        await assert_settings_match_live_vector_index(client, database)

    logger.info(
        "MCP server ready (database=%s, user_id=%s, thread_id=%s)",
        database,
        user_id,
        thread_id,
    )
    try:
        yield {
            "client": client,
            "database": database,
            "llm": llm,
            "embedding_model": embedding_model,
            "user_id": user_id,
            "thread_id": thread_id,
        }
    finally:
        await client.close()
        # Flush batched Opik telemetry before the process exits (fail-open).
        flush_opik()
        logger.info("MCP server shut down")


mcp = FastMCP(
    "Tree Memory",
    instructions=(
        "Query and build a personal knowledge graph of documents, people, tasks, "
        "and preferences. Use 'query_memory' for flexible natural language "
        "queries. Use 'search_memory' as a reliable fallback for semantic similarity search. "
        "Use 'deep_search_memory' for broad exploration — it saves results to disk and "
        "returns a lightweight index; read individual files for details. "
        "Use 'search_web' for on-demand web searches that don't write to memory. "
        "Use 'ingest_url' to add web content, 'ingest_file' for local files, "
        "and 'ingest_conversation' to extract knowledge from conversations."
    ),
    lifespan=app_lifespan,
)

# FastMCP Cloud (Prefect Horizon) loads the configured entrypoint
# ``…/server.py:mcp`` BY FILE PATH, so this module runs under the name ``server``
# rather than the canonical package name ``tree.mcp.server``. The tool modules
# imported just below register on ``mcp`` via ``from tree.mcp.server import mcp``;
# without this alias that import would execute a SECOND, fresh copy of this file
# as ``tree.mcp.server`` and register all 13 tools on a DIFFERENT ``FastMCP``
# instance than the one Horizon serves — the deployed server then advertises 0
# tools (it always worked locally, where every caller already reaches this module
# through the package import). Aliasing this module as ``tree.mcp.server`` makes
# those imports resolve back to THIS instance. ``setdefault`` is a no-op on the
# normal package import path, where ``tree.mcp.server`` is already registered.
sys.modules.setdefault("tree.mcp.server", sys.modules[__name__])

import tree.mcp.tools  # noqa: E402, F401 — registers tools on `mcp`
