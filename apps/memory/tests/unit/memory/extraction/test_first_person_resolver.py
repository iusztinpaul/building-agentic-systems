"""Unit tests for the first-person resolver.

The resolver runs after the LLM emits and before entity resolution. It
redirects any ``person`` node whose name or alias matches the active
user's known surface forms to ``name="self"``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from beanie import PydanticObjectId

from tree.entities.knowledge_graph import NodeType
from tree.memory.extraction.first_person_resolver import redirect_first_person
from tree.memory.types import ExtractedNode


def _make_user(
    *,
    identifier: str = "user@example.com",
    name: str | None = None,
    aliases: list[str] | None = None,
) -> MagicMock:
    """Build a User-like mock with the attributes the resolver inspects."""

    user = MagicMock(name="User")
    user.id = PydanticObjectId("507f1f77bcf86cd799439011")
    user.identifier = identifier
    user.attributes = {}
    if name is not None:
        user.attributes["name"] = name
    if aliases is not None:
        user.attributes["aliases"] = aliases
    return user


class TestExactMatch:
    def test_name_match_redirects_to_self(self) -> None:
        user = _make_user(name="Paul")
        nodes = [ExtractedNode(name="paul", type=NodeType.PERSON, properties={})]

        out = redirect_first_person(nodes, user)

        assert out[0].name == "self"
        # The original surface form is preserved in aliases for traceability.
        assert "paul" in out[0].properties.get("aliases", [])


class TestAliasMatch:
    def test_user_alias_match_redirects(self) -> None:
        user = _make_user(name="Paul", aliases=["pbi"])
        nodes = [ExtractedNode(name="pbi", type=NodeType.PERSON, properties={})]

        out = redirect_first_person(nodes, user)

        assert out[0].name == "self"

    def test_node_alias_match_redirects(self) -> None:
        """When the extracted node carries aliases, any alias hitting the
        user's known surface forms triggers the redirect."""

        user = _make_user(name="Paul")
        nodes = [
            ExtractedNode(
                name="someone",
                type=NodeType.PERSON,
                properties={"aliases": ["paul"]},
            )
        ]

        out = redirect_first_person(nodes, user)

        assert out[0].name == "self"


class TestCaseInsensitivity:
    def test_mixed_case_matches(self) -> None:
        user = _make_user(name="Paul")
        nodes = [ExtractedNode(name="PAUL", type=NodeType.PERSON, properties={})]

        out = redirect_first_person(nodes, user)

        assert out[0].name == "self"


class TestNoMatch:
    def test_unrelated_name_passes_through(self) -> None:
        user = _make_user(name="Paul")
        nodes = [ExtractedNode(name="alice", type=NodeType.PERSON, properties={})]

        out = redirect_first_person(nodes, user)

        assert out[0].name == "alice"

    def test_non_person_node_ignored(self) -> None:
        """A TASK node named 'paul' is never redirected — the resolver
        scopes to ``NodeType.PERSON``."""

        user = _make_user(name="Paul")
        nodes = [ExtractedNode(name="paul", type=NodeType.TASK, properties={})]

        out = redirect_first_person(nodes, user)

        assert out[0].name == "paul"


class TestEmptyAttributes:
    def test_user_with_no_known_aliases_is_noop(self) -> None:
        """When ``User.attributes`` is empty and ``identifier`` is also
        a stub, no redirect can happen."""

        user = MagicMock(name="User")
        user.id = PydanticObjectId("507f1f77bcf86cd799439011")
        user.identifier = ""
        user.attributes = {}
        nodes = [ExtractedNode(name="paul", type=NodeType.PERSON, properties={})]

        out = redirect_first_person(nodes, user)

        # Nothing changes.
        assert out[0].name == "paul"

    def test_identifier_match_still_works(self) -> None:
        """``User.identifier`` is part of the matching set, so even if
        attributes is empty, an identifier hit triggers a redirect."""

        user = _make_user(identifier="paul")
        user.attributes = {}
        nodes = [ExtractedNode(name="paul", type=NodeType.PERSON, properties={})]

        out = redirect_first_person(nodes, user)

        assert out[0].name == "self"


class TestIdempotency:
    def test_second_pass_is_noop(self) -> None:
        """Running the resolver on its own output is a no-op."""

        user = _make_user(name="Paul")
        nodes = [ExtractedNode(name="paul", type=NodeType.PERSON, properties={})]

        first = redirect_first_person(nodes, user)
        # Snapshot the post-pass state.
        first_state = [(n.name, dict(n.properties)) for n in first]

        second = redirect_first_person(first, user)
        second_state = [(n.name, dict(n.properties)) for n in second]

        assert first_state == second_state
        assert second[0].name == "self"

    def test_already_self_passes_through(self) -> None:
        """A node already at ``name='self'`` is not re-aliased."""

        user = _make_user(name="Paul")
        nodes = [ExtractedNode(name="self", type=NodeType.PERSON, properties={})]
        before_aliases = list(nodes[0].properties.get("aliases", []))

        out = redirect_first_person(nodes, user)

        assert out[0].name == "self"
        # Aliases not mutated.
        assert out[0].properties.get("aliases", []) == before_aliases


class TestMultipleNodes:
    def test_only_matching_nodes_redirect(self) -> None:
        user = _make_user(name="Paul")
        nodes = [
            ExtractedNode(name="paul", type=NodeType.PERSON, properties={}),
            ExtractedNode(name="alice", type=NodeType.PERSON, properties={}),
            ExtractedNode(name="paul", type=NodeType.TASK, properties={}),
        ]

        out = redirect_first_person(nodes, user)

        assert out[0].name == "self"
        assert out[1].name == "alice"
        assert out[2].name == "paul"  # TASK — never redirected
