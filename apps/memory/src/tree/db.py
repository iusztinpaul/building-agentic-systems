"""MongoDB bootstrap and the **Mode reset**.

``init_mongodb`` owns which collections exist and which **Memory mode**'s index
set ``memory`` carries; ``reset_memory_mode`` is its inverse for the two
collections whose CONTENTS depend on that mode.

What a **Mode reset** keeps, and why:

* ``documents`` — the Data Pipeline's output, the thing to re-ingest. Every
  **Document** becomes pending by itself: it counts as ingested iff a ``memory``
  row lists its ``_id`` in ``sources``, and those rows are gone.
* ``users`` + ``sessions`` — the tenant list and the current-user session;
  ``User.ensure_self_person`` re-creates ``person:self`` at the next graphrag run.
* ``knowledge_graph_meta_state`` — the dream watermark is a START timestamp, and
  the rebuilt rows carry a newer ``updated_at``, so the next dream run sees them.
* ``extraction_rejections`` / ``extraction_dropped_fields`` — audit history;
  their stale ``chunk_id`` references are harmless, nothing joins on them.
"""

import logging

from beanie import init_beanie
from pydantic import BaseModel
from pymongo import AsyncMongoClient

from tree.config.app_config import MemoryMode, app_config
from tree.config.settings import settings
from tree.entities.clusters import MEMORY_CLUSTERS_COLLECTION, MemoryCluster
from tree.entities.documents import Document
from tree.entities.extraction_audit import (
    ExtractionDroppedField,
    ExtractionRejection,
)
from tree.entities.memory import MEMORY_COLLECTION, MemoryEntry, memory_indexes
from tree.entities.meta_state import KnowledgeGraphMetaState
from tree.entities.sessions import Session
from tree.entities.users import User

logger = logging.getLogger(__name__)

ALL_DOCUMENT_MODELS = [
    Document,
    MemoryEntry,
    MemoryCluster,
    KnowledgeGraphMetaState,
    Session,
    User,
    ExtractionRejection,
    ExtractionDroppedField,
]


async def init_mongodb(uri: str, database: str) -> AsyncMongoClient:
    client = AsyncMongoClient(uri, tz_aware=True)
    # Beanie reads ``Settings.indexes`` at init: bind the configured mode's set
    # first, so every entry point creates exactly that mode's classic indexes.
    MemoryEntry.Settings.indexes = memory_indexes(app_config.memory.mode)
    await init_beanie(database=client[database], document_models=ALL_DOCUMENT_MODELS)

    return client


# The two collections whose contents depend on ``memory.mode``: the rows
# themselves (ADR-006 §1) and the clusters derived from them (ADR-007 §3).
MODE_BOUND_COLLECTIONS: tuple[str, ...] = (
    MEMORY_COLLECTION,
    MEMORY_CLUSTERS_COLLECTION,
)
_KEPT_COLLECTIONS: tuple[str, ...] = (Document.Settings.name, User.Settings.name)


class ModeResetReport(BaseModel):
    """What a **Mode reset** saw (counted BEFORE any drop) and where it ran."""

    env_target: str
    target: str
    database: str
    configured_mode: MemoryMode
    dropped: dict[str, int]
    kept: dict[str, int]
    pending_documents: int
    dry_run: bool


async def reset_memory_mode(
    client: AsyncMongoClient, database: str, *, env_target: str, dry_run: bool
) -> ModeResetReport:
    """**Mode reset** for ALL users: drop ``MODE_BOUND_COLLECTIONS``.

    Counts are exact (``count_documents``), not estimated — they are the
    operator's evidence. Dropping a missing collection is a no-op, so the reset
    is idempotent. Raw collections only: booting Beanie here would recreate the
    old mode's indexes right before the drop. Indexes need no code either way —
    Beanie recreates the configured mode's classic set at the next
    ``init_mongodb``, ``ensure_indexes`` the ``vector_index`` at the next
    indexing run.
    """

    db = client[database]
    dropped = {
        name: await db[name].count_documents({}) for name in MODE_BOUND_COLLECTIONS
    }
    kept = {name: await db[name].count_documents({}) for name in _KEPT_COLLECTIONS}
    pending_documents = await db[Document.Settings.name].count_documents(
        {"content": {"$ne": None}}
    )

    if not dry_run:
        for name, rows in dropped.items():
            await db.drop_collection(name)
            logger.info("Dropped %s (%d rows)", name, rows)

    return ModeResetReport(
        env_target=env_target,
        target=settings.mongo.redacted_target(),
        database=database,
        configured_mode=app_config.memory.mode,
        dropped=dropped,
        kept=kept,
        pending_documents=pending_documents,
        dry_run=dry_run,
    )
