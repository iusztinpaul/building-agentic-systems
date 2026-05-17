"""Tenant-locked reader for ``knowledge_graph``.

Every read of the ``knowledge_graph`` collection in production code MUST
go through :class:`KGQuery`. The class binds a ``user_id`` in its
constructor; every method derives its ``user_id`` filter from
``self.user_id`` and **silently drops any caller-supplied ``user_id``** in
``filter=`` dicts. This eliminates the "forgot to include ``user_id``"
class of bug at the call-site level.

A CI grep (``make memory-check-kgquery-discipline``, wired into
pre-commit) enforces that raw ``KnowledgeGraphEntry.find(...)`` /
``KnowledgeGraphEntry.find_one(...)`` calls do not appear outside this
module, the ``tree.entities.users`` self-person hook, and the (future)
migration script.
"""

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId

from tree.entities.knowledge_graph import EdgeType, KnowledgeGraphEntry, NodeType

logger = logging.getLogger(__name__)


class KGQuery:
    """Tenant-locked reader for ``knowledge_graph``.

    Every method derives its ``user_id`` filter from ``self.user_id``,
    never from caller-supplied ``filter`` dicts. Passing ``user_id`` in a
    ``filter`` keyword is silently stripped (with a debug log) so that
    even a maliciously-crafted argument cannot leak rows from another
    tenant.
    """

    def __init__(self, user_id: PydanticObjectId | None) -> None:
        # ``user_id`` is typed as optional so the runtime guard below is
        # honest: real callers (e.g. a freshly-instantiated ``User`` whose
        # ``.id`` hasn't been populated yet) DO pass ``None``, even though
        # most code paths bind a non-None id. The check converts the
        # silent-leak risk ("filtering by ``user_id=None`` would return
        # rows from any document missing the field") into a loud, early
        # failure.
        if user_id is None:
            raise ValueError("KGQuery requires a non-None user_id")
        self.user_id: PydanticObjectId = user_id

    # ------------------------------------------------------------------
    # Filter helpers
    # ------------------------------------------------------------------

    def _scrub_user_id(self, filter: dict[str, Any] | None) -> dict[str, Any]:
        """Strip ``user_id`` from caller-supplied filters and warn in DEBUG."""

        if not filter:
            return {}
        if "user_id" in filter:
            logger.debug(
                "KGQuery: stripping caller-supplied user_id filter (have %r, "
                "using bound %s)",
                filter["user_id"],
                self.user_id,
            )
        return {k: v for k, v in filter.items() if k != "user_id"}

    # ------------------------------------------------------------------
    # Node reads
    # ------------------------------------------------------------------

    async def find_nodes(
        self,
        type: NodeType | None = None,
        name: str | None = None,
        filter: dict[str, Any] | None = None,
    ) -> list[KnowledgeGraphEntry]:
        """Return every node row matching the supplied predicates.

        ``user_id=self.user_id`` and ``kind="node"`` are added unconditionally.
        Caller-supplied ``user_id`` is silently stripped.
        """

        f: dict[str, Any] = {"user_id": self.user_id, "kind": "node"}
        if type is not None:
            f["type"] = type.value
        if name is not None:
            f["name"] = name
        f.update(self._scrub_user_id(filter))
        return await KnowledgeGraphEntry.find(f).to_list()

    async def find_node_by_id(self, node_id: str) -> KnowledgeGraphEntry | None:
        """Return the node with the given ``_id`` if it belongs to ``self.user_id``."""

        return await KnowledgeGraphEntry.find_one(
            {"_id": node_id, "user_id": self.user_id, "kind": "node"}
        )

    async def find_self_person(self) -> KnowledgeGraphEntry | None:
        """Return the user's ``person:self`` node (``properties.is_active_user=True``).

        The flag is the single source of truth for "who am I?" — see
        :mod:`tree.entities.users` for the upsert contract.
        """

        return await KnowledgeGraphEntry.find_one(
            {
                "user_id": self.user_id,
                "kind": "node",
                "type": NodeType.PERSON.value,
                "properties.is_active_user": True,
            }
        )

    # ------------------------------------------------------------------
    # Edge reads
    # ------------------------------------------------------------------

    async def find_edges(
        self,
        type: EdgeType | None = None,
        source_node_id: str | None = None,
        target_node_id: str | None = None,
        filter: dict[str, Any] | None = None,
    ) -> list[KnowledgeGraphEntry]:
        """Return every edge row matching the supplied predicates.

        ``user_id=self.user_id`` and ``kind="edge"`` are added unconditionally.
        """

        f: dict[str, Any] = {"user_id": self.user_id, "kind": "edge"}
        if type is not None:
            f["type"] = type.value
        if source_node_id is not None:
            f["source_node_id"] = source_node_id
        if target_node_id is not None:
            f["target_node_id"] = target_node_id
        f.update(self._scrub_user_id(filter))
        return await KnowledgeGraphEntry.find(f).to_list()

    async def find_neighbors(
        self,
        node_id: str,
        edge_types: list[EdgeType] | None = None,
        max_hops: int = 1,
    ) -> list[KnowledgeGraphEntry]:
        """Return every edge incident to ``node_id`` (within ``max_hops``).

        ``max_hops`` is enforced via repeated 1-hop traversals so the
        caller does not pay for an explicit ``$graphLookup`` when one hop
        is enough.
        """

        if max_hops < 1:
            return []

        type_filter: dict[str, Any] = {}
        if edge_types is not None:
            type_filter["type"] = {"$in": [t.value for t in edge_types]}

        frontier: set[str] = {node_id}
        visited_nodes: set[str] = {node_id}
        all_edges: list[KnowledgeGraphEntry] = []
        seen_edge_ids: set[str] = set()

        for _ in range(max_hops):
            if not frontier:
                break
            hop_filter = {
                "user_id": self.user_id,
                "kind": "edge",
                "$or": [
                    {"source_node_id": {"$in": list(frontier)}},
                    {"target_node_id": {"$in": list(frontier)}},
                ],
                **type_filter,
            }
            edges = await KnowledgeGraphEntry.find(hop_filter).to_list()
            next_frontier: set[str] = set()
            for edge in edges:
                if edge.id in seen_edge_ids:
                    continue
                seen_edge_ids.add(edge.id)
                all_edges.append(edge)
                if edge.source_node_id and edge.source_node_id not in visited_nodes:
                    next_frontier.add(edge.source_node_id)
                if edge.target_node_id and edge.target_node_id not in visited_nodes:
                    next_frontier.add(edge.target_node_id)
            visited_nodes |= next_frontier
            frontier = next_frontier

        return all_edges
