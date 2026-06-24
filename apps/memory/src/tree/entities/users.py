"""User tenant-identity entity.

Every ``Document`` and ``KnowledgeGraphEntry`` carries a top-level
``user_id`` that references a ``User._id`` (landed in #018). This module
is intentionally small — it lands the Beanie model and the
``after_insert`` hook that auto-creates the user's ``person:self`` node
in the ``knowledge_graph`` collection.

The hook contract (per ``plan.md`` Phase 1, decisions #1 and #3):

* The active user is represented in the KG by a single ``person`` node
  with ``_id = "{user_id}:person:self"``.
* That node carries ``properties.is_active_user=True``. This flag is the
  **single source of truth** for "who am I?" — there is intentionally no
  ``User.self_person_id`` field, so the two sources cannot drift.
* The write is an idempotent ``$setOnInsert`` upsert keyed by ``_id``.
  Re-firing the hook on an existing user is a no-op.

Since #018 the node id is built via the canonical
:func:`tree.entities.knowledge_graph.build_node_id` (which embeds
``user_id``); the row also stamps the indexed ``user_id`` field for
fast filtered reads.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from beanie import Document as BeanieDocument
from beanie import Indexed, Insert, PydanticObjectId, after_event
from pydantic import Field

from tree.entities.knowledge_graph import (
    KnowledgeGraphEntry,
    NodeType,
    build_node_id,
)

if TYPE_CHECKING:
    from pymongo.asynchronous.database import AsyncDatabase

logger = logging.getLogger(__name__)

# The KG collection that carries the ``person:self`` active-user flag.
_KG_COLLECTION = "knowledge_graph"


# ---------------------------------------------------------------------------
# User Beanie model
# ---------------------------------------------------------------------------


class User(BeanieDocument):
    """Tenant identity. Every ``Document`` and ``KnowledgeGraphEntry``
    carries the referencing user's ``_id`` in its ``user_id`` field
    (landing in #018).

    There is intentionally NO ``self_person_id`` field. The user's
    representation inside their own KG is the node at
    ``_id = "{user_id}:person:self"``, identified by
    ``properties.is_active_user=True``. Keeping that flag the single
    source of truth eliminates two-source drift.
    """

    identifier: Indexed(str, unique=True)
    """Stable external handle (e.g. email or OIDC ``sub``). Free string
    for now; auth wiring lands later."""

    attributes: dict[str, Any] = Field(default_factory=dict)
    """Display name, locale, prefs, etc. The self-person hook uses
    ``attributes.get('name', identifier)`` as ``canonical_name``."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "users"

    @after_event(Insert)
    async def after_insert(self) -> None:
        """Idempotent self-person creation.

        Writes a ``person`` KG node with::

            _id          = "{self.id}:person:self"
            kind         = "node"
            type         = NodeType.PERSON
            name         = "self"
            canonical_name = attributes["name"] or identifier
            properties   = {"is_active_user": True, **attributes}

        The write uses ``$setOnInsert`` so re-firing this hook on an
        existing user (e.g. via the #021 migration script) is a no-op
        for the self-person node.
        """

        node_id = build_node_id(self.id, NodeType.PERSON, "self")
        now = datetime.now(UTC)

        # Flag first so attribute keys cannot shadow it. Mirror the user's
        # attributes onto the node so downstream "what does the user know
        # about themselves?" queries have something to surface.
        properties: dict[str, Any] = {**(self.attributes or {}), "is_active_user": True}

        canonical_name = self.attributes.get("name") or self.identifier

        payload: dict[str, Any] = {
            "id": node_id,
            "user_id": self.id,
            "kind": "node",
            "type": NodeType.PERSON.value,
            # #028: ``person`` now carries the closed POLE+O subtype
            # vocabulary ``{"individual", "alias", "persona"}``; the
            # seed self-person is the canonical ``individual``.
            "subtype": "individual",
            "name": "self",
            "canonical_name": canonical_name,
            "properties": properties,
            "embedding": [],
            "aliases": [],
            "confidence": 1.0,
            "sources": [],
            "created_at": now,
            "updated_at": now,
        }

        collection = KnowledgeGraphEntry.get_pymongo_collection()
        await collection.update_one(
            {"_id": node_id},
            {"$setOnInsert": payload},
            upsert=True,
        )

        logger.info(
            "Self-person node upserted for user_id=%s at _id=%s",
            self.id,
            node_id,
        )


# ---------------------------------------------------------------------------
# Active-user enumeration
# ---------------------------------------------------------------------------


async def select_active_user_ids(
    *,
    database: AsyncDatabase,
) -> list[PydanticObjectId]:
    """Return the ``user_id`` of every active user, most-stable order.

    The project's active-user signal is the KG ``person:self`` node carrying
    ``properties.is_active_user=True`` (one per :class:`User`; see above).
    Enumerating off that flag — rather than off the raw ``users`` collection —
    means a user without a materialized self-person node (mid-migration,
    soft-disabled) is skipped, matching the "who am I?" single-source-of-truth
    contract.

    Returned ids are de-duplicated and sorted by their string form so the
    fan-out order is deterministic across runs (handy for tests / logs).
    Shared by the scheduled dream consolidation and the scheduled data pipeline,
    which both fan out per active tenant.
    """

    collection = database[_KG_COLLECTION]
    cursor = collection.find(
        {
            "kind": "node",
            "type": NodeType.PERSON.value,
            "name": "self",
            "properties.is_active_user": True,
        },
        {"user_id": 1},
    )
    seen: set[PydanticObjectId] = set()
    out: list[PydanticObjectId] = []
    async for doc in cursor:
        uid = doc.get("user_id")
        if uid is None or uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    out.sort(key=str)
    return out
