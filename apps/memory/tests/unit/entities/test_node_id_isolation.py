"""Tenant-scoped ``_id`` shape and isolation tests for #018.

The new ``build_node_id(user_id, type, name)`` signature is the
correctness boundary for multi-tenancy: cross-user collisions are
impossible by construction. These tests assert the exact ``_id``
shape, that two distinct ``user_id``s under the same ``(type, name)``
produce distinct ids, and that the prefix is recoverable from the
serialised string (useful for migrations + debugging).
"""

from __future__ import annotations

from beanie import PydanticObjectId

from tree.entities.knowledge_graph import (
    EdgeType,
    NodeType,
    build_edge_id,
    build_node_id,
)


class TestBuildNodeIdShape:
    def test_returns_user_type_name_triple(self) -> None:
        user_id = PydanticObjectId()

        result = build_node_id(user_id, NodeType.PERSON, "alice")

        assert result == f"{user_id}:person:alice"

    def test_user_id_appears_as_leading_segment(self) -> None:
        user_id = PydanticObjectId()
        result = build_node_id(user_id, NodeType.TASK, "ship it")

        # The id is splittable on ":" and the first segment is exactly user_id.
        head, rest = result.split(":", 1)
        assert head == str(user_id)
        assert rest == "task:ship it"

    def test_chunk_id_round_trip(self) -> None:
        # Chunk names can contain extra colons (URL + "#chunk-N"). The
        # builder must not split on them — only the first two ":" are
        # structural.
        user_id = PydanticObjectId()
        name = "https://example.com#chunk-0"

        result = build_node_id(user_id, NodeType.CHUNK, name)

        assert result == f"{user_id}:chunk:{name}"
        # Reverse-parse: user_id is the prefix up to the first ":".
        assert result.startswith(f"{user_id}:")
        assert result.split(":", 2)[2] == name


class TestNodeIdIsolation:
    def test_same_name_under_two_users_yields_distinct_ids(self) -> None:
        user_a = PydanticObjectId()
        user_b = PydanticObjectId()

        id_a = build_node_id(user_a, NodeType.PERSON, "alice")
        id_b = build_node_id(user_b, NodeType.PERSON, "alice")

        assert id_a != id_b
        # Both end with the same suffix; only the leading segment differs.
        assert id_a.endswith(":person:alice")
        assert id_b.endswith(":person:alice")

    def test_same_user_two_different_types_yields_distinct_ids(self) -> None:
        user_id = PydanticObjectId()

        person_id = build_node_id(user_id, NodeType.PERSON, "ship")
        task_id = build_node_id(user_id, NodeType.TASK, "ship")

        assert person_id != task_id


class TestBuildEdgeIdShapePreserved:
    def test_edge_id_unchanged_signature(self) -> None:
        # Edges are tenant-scoped by construction: both endpoint ids
        # already carry the user prefix.
        user_id = PydanticObjectId()
        src = build_node_id(user_id, NodeType.PERSON, "alice")
        tgt = build_node_id(user_id, NodeType.TASK, "write a book")

        result = build_edge_id(src, EdgeType.TODO, tgt)

        assert result == f"{src}|todo|{tgt}"
        # The edge id encodes the user prefix on both endpoints — a
        # cross-user edge would be obvious by inspection.
        assert result.count(f"{user_id}:") == 2
