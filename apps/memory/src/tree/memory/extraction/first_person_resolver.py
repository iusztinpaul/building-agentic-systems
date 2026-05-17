"""Redirect LLM-extracted PERSON nodes that match the active user to ``self``.

When the LLM extracts a ``person`` whose surface form (or any alias)
matches the active user's known aliases — sourced from
``User.attributes['name']``, ``User.attributes.get('aliases', [])``, and
``User.identifier`` — we redirect that node's ``name`` to ``"self"`` so it
shares the canonical ``person:self`` id with the user's self-person row.

Rationale (plan.md Phase 1 — first-person resolver): the user appears in
their own KG exactly once, at ``_id = "{user_id}:person:self"``. If the
LLM emits a ``person:paul`` node for the user named "Paul", the two rows
would race for the same logical entity. Redirecting at extraction time
keeps the contract clean: ``person:self`` is the only ``person`` row that
ever represents the user.

The function is idempotent: a node already named ``"self"`` is a no-op,
and re-running the resolver on its own output produces the same result.
Empty ``User.attributes`` (no display name, no aliases) → no redirect
possible; every emission passes through unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tree.entities.knowledge_graph import NodeType

if TYPE_CHECKING:
    from tree.entities.users import User
    from tree.memory.types import ExtractedNode

logger = logging.getLogger(__name__)


def _user_known_aliases(user: User) -> set[str]:
    """Return the case-folded union of the user's name + identifier + aliases.

    Empty / falsy values are dropped. Returns an empty set when the user
    has no display name, no aliases, and no identifier — the caller treats
    "no aliases" as "no redirect possible".
    """

    attributes = user.attributes or {}
    aliases: set[str] = set()

    name = attributes.get("name")
    if isinstance(name, str) and name.strip():
        aliases.add(name.strip().lower())

    for alias in attributes.get("aliases", []) or []:
        if isinstance(alias, str) and alias.strip():
            aliases.add(alias.strip().lower())

    identifier = getattr(user, "identifier", None)
    if isinstance(identifier, str) and identifier.strip():
        aliases.add(identifier.strip().lower())

    return aliases


def redirect_first_person(
    nodes: list[ExtractedNode],
    user: User,
) -> list[ExtractedNode]:
    """Redirect any LLM-emitted ``person`` matching ``user`` to ``name="self"``.

    Match rule: case-insensitive equality between the node's ``name`` (or
    any value in ``node.properties.get('aliases', [])``) and the union of
    ``user.attributes['name']``, ``user.attributes.get('aliases', [])``,
    and ``user.identifier``.

    Idempotent: nodes already at ``name="self"`` pass through unchanged.
    Re-running on the same input produces the same output.
    """

    aliases = _user_known_aliases(user)
    if not aliases:
        # No known surface forms for this user → nothing to redirect.
        return nodes

    redirected = 0
    for node in nodes:
        if node.type != NodeType.PERSON:
            continue
        if node.name == "self":
            # Idempotent — already redirected.
            continue

        node_aliases_raw = node.properties.get("aliases", []) or []
        node_aliases = {
            a.strip().lower()
            for a in node_aliases_raw
            if isinstance(a, str) and a.strip()
        }
        candidates = {node.name.strip().lower(), *node_aliases}

        if candidates & aliases:
            original_name = node.name
            existing_aliases = list(node_aliases_raw)
            if original_name and original_name not in existing_aliases:
                existing_aliases.append(original_name)
            node.properties["aliases"] = existing_aliases
            node.name = "self"
            redirected += 1
            logger.info(
                "first_person_resolver: redirected person %r to 'self' for user_id=%s",
                original_name,
                user.id,
            )

    if redirected:
        logger.info(
            "first_person_resolver: redirected %d person node(s) to 'self' for user_id=%s",
            redirected,
            user.id,
        )
    return nodes
