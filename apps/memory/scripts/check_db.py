"""
Read-only MongoDB connectivity check against the configured target.

Builds the URI from ``MongoSettings`` exactly like every pipeline does
(honouring the ``.env.target`` switch the Makefile wires), pings the
server, and lists the configured database's collections with document
counts. Exits non-zero on failure so it can gate pipeline runs.

Usage:
    make memory-check-db

    # Direct invocation
    uv run python scripts/check_db.py
"""

import asyncio
import logging
import sys

from pymongo import AsyncMongoClient
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

from tree.config.settings import settings
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

_SERVER_SELECTION_TIMEOUT_MS = 8000


def _redacted_target() -> str:
    """Scheme + host (+ port for non-SRV) — never credentials."""

    mongo = settings.mongo
    if mongo.mongo_scheme == "mongodb+srv":
        return f"mongodb+srv://{mongo.mongo_host}"
    return f"mongodb://{mongo.mongo_host}:{mongo.mongo_port}"


async def check_db() -> bool:
    """Ping the configured MongoDB target and report what it contains."""

    mongo = settings.mongo
    target = _redacted_target()
    logger.info(
        "Checking MongoDB connectivity: %s (user=%s)",
        target,
        mongo.mongo_initdb_root_username,
    )

    client = AsyncMongoClient(
        mongo.mongo_uri.get_secret_value(),
        serverSelectionTimeoutMS=_SERVER_SELECTION_TIMEOUT_MS,
    )
    try:
        await client.admin.command("ping")
        logger.info("Ping OK.")

        database_names = await client.list_database_names()
        logger.info("Databases: %s", ", ".join(database_names))

        database = client[mongo.mongo_initdb_database]
        collections = sorted(await database.list_collection_names())
        if not collections:
            logger.warning(
                "Database '%s' has no collections.", mongo.mongo_initdb_database
            )
        for collection_name in collections:
            count = await database[collection_name].estimated_document_count()
            logger.info(
                "  %s.%s: %d docs",
                mongo.mongo_initdb_database,
                collection_name,
                count,
            )
        return True
    except OperationFailure as exc:
        logger.error(
            "FAILED — authentication/operation error against %s "
            "(check MONGO_INITDB_ROOT_USERNAME/PASSWORD match this host): %s",
            target,
            exc,
        )
        return False
    except ServerSelectionTimeoutError as exc:
        logger.error(
            "FAILED — %s unreachable (host typo, infra down, or for Atlas: "
            "your IP missing from the Network Access list): %s",
            target,
            exc,
        )
        return False
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(check_db()) else 1)
