"""A tiny in-memory stand-in for the ``memory`` collection.

The retrieval tests need two things a plain ``MagicMock`` can't give at once:
the EXACT aggregate pipelines that hit Mongo (so filter shapes are pinned) and
rows that are actually filtered by those pipelines (so "a parent row is never a
seed" is a behavioural claim, not a spelling claim). ``FakeMemoryCollection``
evaluates the handful of operators our pipelines use — equality, ``$or``, ``$in``
(against a scalar, or ANY element of a list field, as Mongo does for
``sources``), ``$nor``, ``$text`` (substring), ``$limit``, ``$graphLookup``
(breadth-first, ``restrictSearchWithMatch`` applied) and a ``$setUnion``
``$project`` — plus exclusion
projections (``{"embedding": 0}``), stamps the ``_search_score`` both legs read
off ``$meta``, and records every call.

It is deliberately NOT a Mongo emulator: anything beyond those operators raises,
so a future pipeline that grows a new stage fails loudly here instead of being
silently ignored by the double.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from beanie import PydanticObjectId


class FakeCursor:
    """Async-iterable cursor over a fixed list of rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    async def __aiter__(self):  # pragma: no cover - trivial
        for row in self._rows:
            yield row

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        """What ``listSearchIndexes`` callers use instead of iterating."""

        return list(self._rows) if length is None else list(self._rows[:length])


def matches(row: dict[str, Any], query: dict[str, Any]) -> bool:
    """Evaluate the query-operator subset our retrieval pipelines emit."""

    for key, expected in query.items():
        if key == "$nor":
            if any(matches(row, sub) for sub in expected):
                return False
        elif key == "$or":
            if not any(matches(row, sub) for sub in expected):
                return False
        elif key == "$text":
            haystack = " ".join(
                [
                    str(row.get("name", "")),
                    str((row.get("properties") or {}).get("content", "")),
                ]
            ).lower()
            if expected["$search"].lower() not in haystack:
                return False
        elif isinstance(expected, dict):
            if set(expected) != {"$in"}:
                raise NotImplementedError(f"unsupported operator: {expected}")
            value = row.get(key)
            # Mongo array semantics: ``$in`` on a list field matches when ANY
            # element is in the list. Python equality keeps an ObjectId and
            # its hex string apart, exactly like Mongo's BSON comparison.
            candidates = value if isinstance(value, list) else [value]
            if not any(candidate in expected["$in"] for candidate in candidates):
                return False
        elif row.get(key) != expected:
            return False
    return True


# The ``listSearchIndexes`` entry a collection has AFTER the indexing pipeline
# ran: mongot serves queries from it. The default, because every pipeline-shape
# test stands for a properly indexed collection.
_QUERYABLE_VECTOR_INDEX: dict[str, Any] = {
    "name": "vector_index",
    "status": "READY",
    "queryable": True,
}


# What a row scores when it does not say otherwise: a clear match, well above
# ``query.min_vector_score`` (0.70 today), so only a test that CARES about the
# gate has to spell a score out.
_DEFAULT_SEARCH_SCORE = 0.9


class FakeMemoryCollection:
    """Records aggregate pipelines / find filters and applies them to ``rows``."""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        search_indexes: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rows = list(rows or [])
        self.pipelines: list[list[dict[str, Any]]] = []
        self.find_filters: list[dict[str, Any]] = []
        self.find_projections: list[dict[str, Any] | None] = []
        # ``search_indexes=[]`` is the never-indexed / dropped-index state;
        # ``[{"status": "BUILDING", "queryable": False}]`` the mid-build one.
        self.search_indexes = (
            [_QUERYABLE_VECTOR_INDEX]
            if search_indexes is None
            else list(search_indexes)
        )
        self.search_index_probes: list[str | None] = []

    async def aggregate(self, pipeline: list[dict[str, Any]]) -> FakeCursor:
        self.pipelines.append(pipeline)
        return FakeCursor(self._apply(pipeline))

    async def list_search_indexes(self, name: str | None = None) -> FakeCursor:
        """The Atlas-Search index catalogue, recorded so a test can assert the
        probe did NOT run on a leg that returned hits."""

        self.search_index_probes.append(name)
        return FakeCursor(
            [
                index
                for index in self.search_indexes
                if name is None or index.get("name") == name
            ]
        )

    def find(
        self, query: dict[str, Any], projection: dict[str, Any] | None = None
    ) -> FakeCursor:
        self.find_filters.append(query)
        self.find_projections.append(projection)
        rows = [row for row in self.rows if matches(row, query)]
        if projection:
            if set(projection.values()) != {0}:
                raise NotImplementedError(f"unsupported projection: {projection}")
            rows = [
                {k: v for k, v in row.items() if k not in projection} for row in rows
            ]
        return FakeCursor(rows)

    @property
    def vector_pipeline(self) -> list[dict[str, Any]]:
        """The single ``$vectorSearch`` pipeline that was issued."""

        return self._one("$vectorSearch")

    @property
    def text_pipeline(self) -> list[dict[str, Any]]:
        """The single ``$text``-carrying ``$match`` pipeline that was issued."""

        return self._one("$match")

    def _one(self, first_stage_key: str) -> list[dict[str, Any]]:
        found = [p for p in self.pipelines if first_stage_key in p[0]]
        assert len(found) == 1, f"expected exactly one {first_stage_key} pipeline"
        return found[0]

    def _apply(self, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        head = pipeline[0]
        if "$vectorSearch" in head:
            stage = head["$vectorSearch"]
            # Rows without a vector are absent from the vector index — which is
            # exactly why parent chunks (``embedding: []``) can't be seeds.
            rows = [
                row
                for row in self.rows
                if row.get("embedding") and matches(row, stage["filter"])
            ]
            rows = rows[: stage["limit"]]
        else:
            rows = [row for row in self.rows if matches(row, head["$match"])]
            for stage in pipeline:
                if "$limit" in stage:
                    rows = rows[: stage["$limit"]]
                elif "$graphLookup" in stage:
                    lookup = stage["$graphLookup"]
                    rows = [
                        {**row, lookup["as"]: self._walk(row, lookup)} for row in rows
                    ]
                elif "$project" in stage and _is_set_union(stage["$project"]):
                    rows = [_project_set_union(row, stage["$project"]) for row in rows]
        return [self._score(row) for row in rows]

    def _walk(
        self, start: dict[str, Any], lookup: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """``$graphLookup``: breadth-first over ``self.rows`` up to ``maxDepth``.

        Depth 0 matches ``connectToField == startWith``; every hop after that
        matches ``connectToField`` against the ``connectFromField`` values just
        found. ``restrictSearchWithMatch`` filters every candidate; each row is
        returned once.
        """

        frontier = {start[lookup["startWith"].removeprefix("$")]}
        found: dict[Any, dict[str, Any]] = {}
        for _depth in range(lookup["maxDepth"] + 1):
            hits = [
                row
                for row in self.rows
                if row["_id"] not in found
                and row.get(lookup["connectToField"]) in frontier
                and matches(row, lookup.get("restrictSearchWithMatch", {}))
            ]
            found.update((row["_id"], row) for row in hits)
            frontier = {row.get(lookup["connectFromField"]) for row in hits}
        return list(found.values())

    @staticmethod
    def _score(row: dict[str, Any]) -> dict[str, Any]:
        """Emulate ``$addFields: {_search_score: {$meta: ...}}``.

        Both legs stamp that field in Mongo, and the vector-leg gate
        (``query.min_vector_score``) READS it — a double that dropped the stage
        would make every gated test pass for the wrong reason. A row may declare
        its own ``_search_score`` to stand for a specific similarity; anything
        else scores ``_DEFAULT_SEARCH_SCORE`` (a clear match). The copy matters:
        stamping in place would leak the vector leg's score into ``self.rows``
        and into the text leg of the SAME query.
        """

        return {
            "_search_score": row.get("_search_score", _DEFAULT_SEARCH_SCORE),
            **row,
        }


def _is_set_union(projection: dict[str, Any]) -> bool:
    return all(
        isinstance(spec, dict) and set(spec) == {"$setUnion"}
        for spec in projection.values()
    )


def _project_set_union(
    row: dict[str, Any], projection: dict[str, Any]
) -> dict[str, Any]:
    """``{"$project": {key: {"$setUnion": ["$a", "$b"]}}}``, deduplicated on ``_id``."""

    projected: dict[str, Any] = {"_id": row["_id"]}
    for key, spec in projection.items():
        union: dict[Any, dict[str, Any]] = {}
        for field in spec["$setUnion"]:
            for item in row.get(field.removeprefix("$"), []):
                union.setdefault(item["_id"], item)
        projected[key] = list(union.values())
    return projected


@pytest.fixture
def make_collection():
    """Factory: ``make_collection([row, ...])`` → :class:`FakeMemoryCollection`."""

    return FakeMemoryCollection


# ---------------------------------------------------------------------------
# Row builders — the shapes ``tree.memory.rag.load`` writes
# ---------------------------------------------------------------------------


def _node_row(user_id: Any, node_id: str, node_type: str, **overrides: Any) -> dict:
    row = {
        "_id": node_id,
        "user_id": user_id,
        "kind": "node",
        "type": node_type,
        "subtype": None,
        "name": node_id,
        "parent_id": None,
        "chunk_index": None,
        "properties": {},
        "embedding": [],
    }
    row.update(overrides)
    return row


def chunk_star_rows(user_id: Any) -> list[dict]:
    """``doc1`` with parents p1..p3; p1's children c1a/c1b, p3's child c3a.

    ``p1 -next-> p2`` and ``c1b -next-> c3a`` make p2 and c3a reachable WITHOUT
    their ``part_of`` edges on the last hop (seed ``["p1", "c1b"]``, one hop):
    the hop is 6 nodes + 5 edges, the ``part_of`` closure adds p3 and 3 edges.
    A plain function so ``tests/unit/mcp`` can run the real ``expand_graph``.
    """

    def edge(source: str, target: str, edge_type: str = "part_of") -> dict:
        return {
            "_id": f"{source}>{target}",
            "user_id": user_id,
            "kind": "edge",
            "type": edge_type,
            "source_node_id": source,
            "target_node_id": target,
            "sources": [],
        }

    def chunk(node_id: str, subtype: str, parent_id: str) -> dict:
        return _node_row(
            user_id, node_id, "chunk", subtype=subtype, parent_id=parent_id
        )

    return [
        _node_row(user_id, "doc1", "document"),
        chunk("p1", "parent", "doc1"),
        chunk("p2", "parent", "doc1"),
        chunk("p3", "parent", "doc1"),
        chunk("c1a", "child", "p1"),
        chunk("c1b", "child", "p1"),
        chunk("c3a", "child", "p3"),
        edge("p1", "doc1"),
        edge("p2", "doc1"),
        edge("p3", "doc1"),
        edge("c1a", "p1"),
        edge("c1b", "p1"),
        edge("c3a", "p3"),
        edge("p1", "p2", "next"),
        edge("c1b", "c3a", "next"),
    ]


@pytest.fixture
def make_document_row():
    """A ``document`` row: metadata root, never embedded.

    ``sources`` defaults to one fresh ObjectId (the ``documents`` row it was
    loaded from), so every document carries the provenance the **Full graph**
    keys on; ``created_at`` is set only when a test passes one.
    """

    def _make(
        user_id: Any,
        node_id: str = "doc1",
        *,
        created_at: datetime | None = None,
        sources: list[Any] | None = None,
        **properties: Any,
    ) -> dict:
        extra: dict[str, Any] = {
            "sources": [PydanticObjectId()] if sources is None else sources
        }
        if created_at is not None:
            extra["created_at"] = created_at
        return _node_row(
            user_id,
            node_id,
            "document",
            **extra,
            properties={
                "source_type": "file",
                "source_uri": f"file://{node_id}",
                "title": "Memory for AI Agents",
                "date": "2026-09-05",
                **properties,
            },
        )

    return _make


@pytest.fixture
def make_parent_row():
    """A **Parent chunk** row: has content and a heading path, no vector."""

    def _make(
        user_id: Any,
        node_id: str = "p1",
        *,
        document_id: str = "doc1",
        content: str = "parent content",
        heading_path: list[str] | None = None,
        chunk_index: int = 0,
        **properties: Any,
    ) -> dict:
        return _node_row(
            user_id,
            node_id,
            "chunk",
            subtype="parent",
            parent_id=document_id,
            chunk_index=chunk_index,
            properties={
                "content": content,
                "heading_path": heading_path or ["Retrieval"],
                "title": "Memory for AI Agents",
                "source_type": "file",
                "source_uri": "file://doc1",
                "date": "2026-09-05",
                **properties,
            },
        )

    return _make


@pytest.fixture
def make_child_row():
    """A **Child chunk** row: the only chunk level carrying an ``embedding``."""

    def _make(
        user_id: Any,
        node_id: str = "c0",
        *,
        parent_id: str = "p1",
        content: str = "child content",
        chunk_index: int = 0,
    ) -> dict:
        return _node_row(
            user_id,
            node_id,
            "chunk",
            subtype="child",
            parent_id=parent_id,
            chunk_index=chunk_index,
            properties={"content": content},
            embedding=[0.1, 0.2, 0.3, 0.4],
        )

    return _make


@pytest.fixture
def make_entity_row():
    """An LLM-extracted entity row — a valid graphrag seed, never a chunk."""

    def _make(
        user_id: Any,
        node_id: str = "e1",
        *,
        node_type: str = "person",
        name: str = "alice",
    ) -> dict:
        return _node_row(
            user_id,
            node_id,
            node_type,
            name=name,
            properties={"content": name},
            embedding=[0.1, 0.2, 0.3, 0.4],
        )

    return _make
