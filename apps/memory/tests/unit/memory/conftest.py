"""A tiny in-memory stand-in for the ``memory`` collection.

The retrieval tests need two things a plain ``MagicMock`` can't give at once:
the EXACT aggregate pipelines that hit Mongo (so filter shapes are pinned) and
rows that are actually filtered by those pipelines (so "a parent row is never a
seed" is a behavioural claim, not a spelling claim). ``FakeMemoryCollection``
evaluates the handful of operators our pipelines use — equality, ``$in``,
``$nor``, ``$text`` (substring), ``$limit`` — and records every call.

It is deliberately NOT a Mongo emulator: anything beyond those operators raises,
so a future pipeline that grows a new stage fails loudly here instead of being
silently ignored by the double.
"""

from __future__ import annotations

from typing import Any

import pytest


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
            if row.get(key) not in expected["$in"]:
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

    def find(self, query: dict[str, Any]) -> FakeCursor:
        self.find_filters.append(query)
        return FakeCursor([row for row in self.rows if matches(row, query)])

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
            return rows[: stage["limit"]]
        rows = [row for row in self.rows if matches(row, head["$match"])]
        for stage in pipeline:
            if "$limit" in stage:
                rows = rows[: stage["$limit"]]
        return rows


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


@pytest.fixture
def make_document_row():
    """A ``document`` row: metadata root, never embedded."""

    def _make(user_id: Any, node_id: str = "doc1", **properties: Any) -> dict:
        return _node_row(
            user_id,
            node_id,
            "document",
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
