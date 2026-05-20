"""Shared node-text embedding for dedup and indexing.

This module is the single source of truth for the **generic node-text**
path: turn a knowledge-graph node ``dict`` into the text we embed, and
embed a batch of such nodes with the search embedding model. It lives at
the ``memory/`` layer (rather than under ``indexing/`` or ``extraction/``)
because both subpackages depend on it and neither should import the
other.

After this refactor the indexing backfill (``indexing.core.embed_nodes``)
routes through :func:`node_to_embedding_text`; #042 migrates generic
dedup onto the same function so the two call sites cannot drift — a node
embedded at creation and the same node re-embedded by the backfill
produce identical text in, hence identical vectors out.

INTENTIONALLY SEPARATE — do NOT fold in
----------------------------------------
PREFERENCE and FACT nodes do **not** use this generic node-text path.
``extraction.pipeline._dispatch_entity_write`` embeds
``properties.statement`` (PREFERENCE) and ``properties.object`` (FACT)
instead — the #032 supersession contract requires the stored vector to
be a statement<->statement (resp. object<->object) comparison so the
supersession resolver compares apples to apples. That statement-embedding
logic stays in ``_dispatch_entity_write`` and is OUT OF SCOPE for this
module. A future maintainer must NOT "unify" the two: replacing the
statement embedding with :func:`node_to_embedding_text` would silently
break preference/fact supersession.
"""

from typing import Any

from tree.models.base import BaseEmbeddingModel


def node_to_embedding_text(node: dict[str, Any]) -> str:
    """Build an embeddable text representation from a node document.

    Uses the node's surface ``name`` (or ``canonical_name`` if absent) as
    the headline token rather than ``_id``. Post-Phase-1 every ``_id``
    starts with ``"{user_id}:"`` — a 24-char ObjectId hex prefix that is
    constant per tenant and adds no semantic value (we already filter
    ``$vectorSearch`` by ``user_id`` server-side). Falling back to the
    ``_id`` only when both name fields are missing preserves backward
    compatibility with legacy rows.

    Layout (preserved verbatim from the pre-refactor
    ``indexing.core._node_to_text``): ``"{type}: {headline}"`` first, then
    one ``"{key}: {value}"`` line per non-empty, non-``content`` property,
    then the ``content`` value last (it may be long).

    This is the GENERIC node-text builder. PREFERENCE/FACT nodes embed
    ``statement``/``object`` elsewhere (see the module docstring) and must
    not be routed through here.
    """

    headline = node.get("name") or node.get("canonical_name") or node.get("_id", "")
    parts = [f"{node.get('type', '')}: {headline}"]
    props = node.get("properties", {})
    for key, value in props.items():
        if value and key != "content":
            parts.append(f"{key}: {value}")
    # Include content last (may be long).
    if props.get("content"):
        parts.append(str(props["content"]))
    return "\n".join(parts)


async def embed_node_texts(
    nodes: list[dict[str, Any]],
    embedding_model: BaseEmbeddingModel,
) -> list[list[float]]:
    """Embed a list of node documents via their generic node-text.

    Maps each node through :func:`node_to_embedding_text` and issues a
    single ``embedding_model.embed(...)`` call — the model already accepts
    a list of texts. Real-time request batching is layered on in #043; for
    now one call per batch is sufficient. The returned vectors are aligned
    positionally with ``nodes``. An empty input yields an empty list
    without calling the model.
    """

    if not nodes:
        return []

    texts = [node_to_embedding_text(node) for node in nodes]
    return await embedding_model.embed(texts)
