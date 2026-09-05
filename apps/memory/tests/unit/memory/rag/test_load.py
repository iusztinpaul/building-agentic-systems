"""Unit tests for the RAG load stage (``tree.memory.rag.load``).

Mostly pure op building: each test inspects the ``UpdateOne`` ops the loader
would send, not a live collection (the whole-worker "what landed in Mongo"
claims live in ``tests/unit/memory/test_pipeline.py``).

``TestPropertiesAreReplacedInMongo`` is the deliberate exception: whether a
pipeline ``$set`` REPLACES or MERGES an embedded document is a MongoDB server
semantic, so that one claim is only worth asserting against a real
``unit_tests_twin`` collection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tests.unit.conftest import TEST_DATABASE
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.memory import (
    MEMORY_COLLECTION,
    RAG_NODE_TYPES,
    NodeType,
    build_node_id,
)
from tree.memory.rag.embedding import child_embedding_text
from tree.memory.rag.load import (
    _build_node_op,
    build_rag_row_ops,
    child_chunk_name,
    child_row_id,
    document_row_id,
    load_rag_rows,
    parent_chunk_name,
    parent_row_id,
)
from tree.memory.rag.types import ChildChunk, ParentChunk

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_DOCUMENT_ID = "507f1f77bcf86cd799439012"
_URI = "https://example.com/article"
_TITLE = "Memory for AI Agents"


def _parents(*shape: int) -> list[ParentChunk]:
    """A hierarchy with ``shape[i]`` children under parent ``i``."""

    return [
        ParentChunk(
            index=parent_index,
            content=f"parent {parent_index} content",
            heading_path=["Memory", f"Section {parent_index}"],
            children=[
                ChildChunk(
                    index=child_index, content=f"child {parent_index}.{child_index}"
                )
                for child_index in range(n_children)
            ],
        )
        for parent_index, n_children in enumerate(shape)
    ]


def _ops(*shape: int, child_vectors: dict[str, list[float]] | None = None) -> list[Any]:
    return build_rag_row_ops(
        user_id=_USER_ID,
        document_id=_DOCUMENT_ID,
        source_uri=_URI,
        source_type="web",
        title=_TITLE,
        date="2026-01-01",
        parents=_parents(*shape),
        child_vectors=child_vectors or {},
    )


def _set_stage(op: Any) -> dict[str, Any]:
    """The ``$set`` payload of an aggregation-pipeline ``UpdateOne``."""

    return op._doc[0]["$set"]


def _by_id(ops: list[Any]) -> dict[str, dict[str, Any]]:
    return {op._filter["_id"]: _set_stage(op) for op in ops}


def _properties(row: dict[str, Any]) -> dict[str, Any]:
    """The ``properties`` payload, unwrapped from its ``$literal`` guard.

    Asserting the wrapper here rather than in one test keeps every row-shape
    assertion honest: drop the ``$literal`` and MongoDB silently turns the
    replace into a per-key merge (see ``TestPropertiesAreReplacedInMongo``).
    """

    assert set(row["properties"]) == {"$literal"}
    return row["properties"]["$literal"]


# ---------------------------------------------------------------------------
# Deterministic names + ids
# ---------------------------------------------------------------------------


class TestRowNames:
    def test_parent_name_is_uri_and_position(self) -> None:
        assert parent_chunk_name(_URI, 2) == f"{_URI}#parent-2"

    def test_child_name_nests_under_its_parent(self) -> None:
        assert child_chunk_name(_URI, 2, 5) == f"{_URI}#parent-2#child-5"

    def test_ids_are_tenant_scoped_node_ids(self) -> None:
        assert document_row_id(_USER_ID, _URI) == build_node_id(
            _USER_ID, "document", _URI
        )
        assert parent_row_id(_USER_ID, _URI, 0) == build_node_id(
            _USER_ID, "chunk", f"{_URI}#parent-0"
        )
        assert child_row_id(_USER_ID, _URI, 0, 1) == build_node_id(
            _USER_ID, "chunk", f"{_URI}#parent-0#child-1"
        )

    def test_ids_are_stable_across_calls(self) -> None:
        # Determinism IS the idempotency guarantee: the same document must
        # always upsert onto the same ``_id``s.
        assert [op._filter["_id"] for op in _ops(2)] == [
            op._filter["_id"] for op in _ops(2)
        ]


# ---------------------------------------------------------------------------
# The three row shapes
# ---------------------------------------------------------------------------


class TestBuildRagRowOps:
    def test_emits_one_document_row_plus_the_whole_hierarchy(self) -> None:
        ops = _ops(3, 2)

        # 1 document + 2 parents + 5 children.
        assert len(ops) == 8

    def test_a_parentless_document_still_gets_its_document_row(self) -> None:
        # The row is the provenance anchor the coordinator's pending-doc
        # resolution reads — without it an empty document stays "pending".
        ops = _ops()

        assert len(ops) == 1
        assert ops[0]._filter["_id"] == document_row_id(_USER_ID, _URI)

    def test_every_emitted_type_is_a_rag_node_type(self) -> None:
        rows = _by_id(_ops(2, 2))

        assert {row["type"] for row in rows.values()} <= RAG_NODE_TYPES

    def test_document_row_shape(self) -> None:
        row = _by_id(_ops(1))[document_row_id(_USER_ID, _URI)]

        assert row["type"] == "document"
        assert row["name"] == _URI
        assert row["subtype"] is None
        assert row["parent_id"] is None
        assert row["chunk_index"] is None
        assert row["embedding"] == []
        assert _properties(row) == {
            "source_type": "web",
            "source_uri": _URI,
            "title": _TITLE,
            "date": "2026-01-01",
        }

    def test_parent_row_shape(self) -> None:
        row = _by_id(_ops(1, 1))[parent_row_id(_USER_ID, _URI, 1)]

        assert row["type"] == "chunk"
        assert row["subtype"] == "parent"
        assert row["parent_id"] == document_row_id(_USER_ID, _URI)
        assert row["chunk_index"] == 1
        # Parents are NEVER embedded — a vector here would surface them as
        # search hits and defeat parent-document retrieval.
        assert row["embedding"] == []
        assert _properties(row) == {
            "source_type": "web",
            "source_uri": _URI,
            "date": "2026-01-01",
            "title": _TITLE,
            "heading_path": ["Memory", "Section 1"],
            "content": "parent 1 content",
        }

    def test_child_row_shape(self) -> None:
        row = _by_id(_ops(2))[child_row_id(_USER_ID, _URI, 0, 1)]

        assert row["type"] == "chunk"
        assert row["subtype"] == "child"
        assert row["parent_id"] == parent_row_id(_USER_ID, _URI, 0)
        assert row["chunk_index"] == 1
        assert _properties(row)["content"] == "child 0.1"
        # A child inherits its parent's heading path and the document title so
        # the indexing backfill rebuilds the embedding text without a join.
        assert _properties(row)["heading_path"] == ["Memory", "Section 0"]
        assert _properties(row)["title"] == _TITLE

    def test_child_carries_the_vector_of_its_contextual_header_text(self) -> None:
        text = child_embedding_text(
            title=_TITLE, heading_path=["Memory", "Section 0"], content="child 0.0"
        )
        vector = [0.25, 0.5]

        row = _by_id(_ops(1, child_vectors={text: vector}))[
            child_row_id(_USER_ID, _URI, 0, 0)
        ]

        assert row["embedding"] == vector

    def test_child_without_a_vector_is_written_unembedded(self) -> None:
        # Left for the indexing backfill instead of failing the load.
        row = _by_id(_ops(1, child_vectors={}))[child_row_id(_USER_ID, _URI, 0, 0)]

        assert row["embedding"] == []

    def test_rows_preserve_created_at_and_union_sources_on_re_upsert(self) -> None:
        row = _by_id(_ops(1))[document_row_id(_USER_ID, _URI)]

        assert row["created_at"] == {"$ifNull": ["$created_at", row["updated_at"]]}
        assert row["sources"] == {
            "$setUnion": [
                {"$ifNull": ["$sources", []]},
                [PydanticObjectId(_DOCUMENT_ID)],
            ]
        }

    def test_properties_are_wrapped_in_literal_so_mongo_replaces_them(self) -> None:
        # A bare dict here is an object SPECIFICATION: Mongo would assign it key
        # by key and keep every pre-existing key, so a stale ``content`` from an
        # older chunking config would survive and poison retrieval. ``$literal``
        # makes it a constant, which overwrites the sub-document wholesale.
        # ``TestPropertiesAreReplacedInMongo`` proves the server behaviour.
        row = _by_id(_ops(1))[parent_row_id(_USER_ID, _URI, 0)]

        assert row["properties"] == {"$literal": _properties(row)}
        assert "$mergeObjects" not in str(row["properties"])

    def test_every_op_is_an_upsert(self) -> None:
        assert all(op._upsert for op in _ops(2, 2))


class TestRagNodeTypeGuard:
    """ADR-006 §1: ``rag/`` writes ``RAG_NODE_TYPES`` node rows and NOTHING else.

    The two halves of that claim — no foreign node type in, no ``kind: edge``
    out — are what keeps ``rag`` mode edge-free without the loader knowing the
    graph layer exists.
    """

    def test_a_non_rag_node_type_raises(self) -> None:
        with pytest.raises(ValueError, match="only writes"):
            _build_node_op(
                user_id=_USER_ID,
                node_id="x",
                node_type="person",
                name="alice",
                subtype=None,
                parent_id=None,
                chunk_index=None,
                properties={},
                embedding=[],
                source_document_id=_DOCUMENT_ID,
                now=None,
            )

    @pytest.mark.parametrize(
        "node_type", sorted(set(NodeType) - {NodeType(t) for t in RAG_NODE_TYPES})
    )
    def test_every_non_rag_node_type_of_the_ontology_is_rejected(
        self, node_type: NodeType
    ) -> None:
        """Not just ``person`` — EVERY graph-owned node type is refused.

        Parametrised off the ontology so a new ``NodeType`` is covered the day
        it is added, instead of silently becoming loadable from ``rag``.
        """

        with pytest.raises(ValueError, match="only writes"):
            _build_node_op(
                user_id=_USER_ID,
                node_id="x",
                node_type=node_type.value,
                name="whatever",
                subtype=None,
                parent_id=None,
                chunk_index=None,
                properties={},
                embedding=[],
                source_document_id=_DOCUMENT_ID,
                now=None,
            )

    def test_the_edge_kind_is_rejected_like_any_other_foreign_type(self) -> None:
        """``kind`` is not a node type, so it cannot sneak in through ``type``."""

        with pytest.raises(ValueError, match="only writes"):
            _build_node_op(
                user_id=_USER_ID,
                node_id="x",
                node_type="part_of",
                name="child -> parent",
                subtype=None,
                parent_id=None,
                chunk_index=None,
                properties={},
                embedding=[],
                source_document_id=_DOCUMENT_ID,
                now=None,
            )

    def test_no_op_of_a_full_hierarchy_is_an_edge_row(self) -> None:
        """Every op the builder emits is ``kind: node`` with no edge endpoints.

        ``part_of`` / ``next`` are graphrag-only (ADR-006 §3): a rag-mode run
        must leave the ``memory`` collection with zero ``kind: edge`` rows, and
        this is where that starts.
        """

        rows = _by_id(_ops(2, 3))

        assert {row["kind"] for row in rows.values()} == {"node"}
        assert not any(
            key in row
            for row in rows.values()
            for key in ("source_node_id", "target_node_id")
        )


# ---------------------------------------------------------------------------
# The single write
# ---------------------------------------------------------------------------


class TestLoadRagRows:
    async def test_flushes_every_op_in_one_unordered_bulk_write(self) -> None:
        collection = MagicMock(name="collection")
        collection.bulk_write = AsyncMock(return_value=MagicMock())
        database = MagicMock(name="database")
        database.__getitem__.return_value = collection
        ops = _ops(2, 2)

        written = await load_rag_rows(database=database, ops=ops)

        assert written == len(ops)
        database.__getitem__.assert_called_with(MEMORY_COLLECTION)
        collection.bulk_write.assert_awaited_once_with(ops, ordered=False)

    async def test_empty_op_list_skips_the_round_trip(self) -> None:
        # ``bulk_write([])`` raises, so an empty run must not call it at all.
        collection = MagicMock(name="collection")
        collection.bulk_write = AsyncMock(return_value=MagicMock())
        database = MagicMock(name="database")
        database.__getitem__.return_value = collection

        written = await load_rag_rows(database=database, ops=[])

        assert written == 0
        collection.bulk_write.assert_not_awaited()


# ---------------------------------------------------------------------------
# Replace-vs-merge — the one claim only a real server can settle
# ---------------------------------------------------------------------------


class TestPropertiesAreReplacedInMongo:
    """``properties`` is REPLACED on re-upsert, against a real MongoDB.

    Whether a pipeline ``$set`` replaces or merges an embedded document is a
    server semantic: a bare dict is an object specification and merges key by
    key (reproduced on MongoDB 8.2.5), which is why ``_build_node_op`` wraps the
    payload in ``$literal``. Drop that wrapper and
    ``test_a_stale_property_key_is_dropped_on_re_upsert`` fails while every pure
    op-shape test above still passes — the reason this class talks to Mongo.
    """

    @pytest.fixture
    async def database(self) -> Any:
        client = await init_mongodb(
            settings.mongo.mongo_uri.get_secret_value(), TEST_DATABASE
        )
        database = client[TEST_DATABASE]
        yield database
        await database[MEMORY_COLLECTION].delete_many({})

    async def test_a_stale_property_key_is_dropped_on_re_upsert(self, database) -> None:
        # A row written by an OLDER chunking config: one key the loader no
        # longer emits, one it does. Both must give way to this run's payload.
        row_id = parent_row_id(_USER_ID, _URI, 0)
        await database[MEMORY_COLLECTION].insert_one(
            {
                "_id": row_id,
                "user_id": _USER_ID,
                "kind": "node",
                "type": "chunk",
                "name": parent_chunk_name(_URI, 0),
                "properties": {
                    "content": "text from a previous chunking config",
                    "stale_key": "poison",
                },
                "sources": [PydanticObjectId(_DOCUMENT_ID)],
                "created_at": datetime(2020, 1, 1, tzinfo=UTC),
                "updated_at": datetime(2020, 1, 1, tzinfo=UTC),
            }
        )

        await load_rag_rows(database=database, ops=_ops(1))

        row = await database[MEMORY_COLLECTION].find_one({"_id": row_id})
        assert row["properties"] == {
            "source_type": "web",
            "source_uri": _URI,
            "date": "2026-01-01",
            "title": _TITLE,
            "heading_path": ["Memory", "Section 0"],
            "content": "parent 0 content",
        }
        assert "stale_key" not in row["properties"]

    async def test_the_replace_still_preserves_created_at_and_unions_sources(
        self, database
    ) -> None:
        # The replace must stay surgical: provenance (``sources``) accumulates
        # and the original ``created_at`` survives, exactly as on a plain re-run.
        row_id = parent_row_id(_USER_ID, _URI, 0)
        created_at = datetime(2020, 1, 1, tzinfo=UTC)
        earlier_source = PydanticObjectId()
        await database[MEMORY_COLLECTION].insert_one(
            {
                "_id": row_id,
                "user_id": _USER_ID,
                "kind": "node",
                "type": "chunk",
                "name": parent_chunk_name(_URI, 0),
                "properties": {"content": "text from a previous chunking config"},
                "sources": [earlier_source],
                "created_at": created_at,
                "updated_at": created_at,
            }
        )

        await load_rag_rows(database=database, ops=_ops(1))

        row = await database[MEMORY_COLLECTION].find_one({"_id": row_id})
        assert row["created_at"] == created_at
        assert row["updated_at"] > created_at
        assert set(row["sources"]) == {earlier_source, PydanticObjectId(_DOCUMENT_ID)}
