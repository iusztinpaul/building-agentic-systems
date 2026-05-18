"""Tree's downstream subtype extensions on the POLE+O ontology.

Tree is the first customer of the
:func:`tree.entities.ontology.register_node_subtype` API: this module
layers Tree's personal-assistant vocabulary (``task`` / ``episode`` /
``topic`` / ``project``) on top of the canonical POLE+O parents
(``object`` and ``event``) registered in
:mod:`tree.entities.ontology`. Per `plan.md:155-173`, these are
**Tree-only** subtypes — NOT canonical POLE+O subtypes — and another
downstream consumer of this library is free to ignore or re-register
them.

Import side-effects: importing this module mutates
:data:`tree.entities.ontology.NODE_REGISTRY` (appends to the
``object`` / ``event`` parents' ``subtypes`` frozensets) and
:data:`tree.entities.ontology.SUBTYPE_EXTRAS` (registers
:class:`ProjectExtras` against ``("object", "project")``). The
canonical ontology module imports this file at the bottom of its
own module-level execution so the extensions land before any
:class:`tree.entities.knowledge_graph.KnowledgeGraphEntry` row is
validated.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from tree.entities.ontology import register_node_subtype


class ExternalRef(BaseModel):
    """Pointer to a richly-tracked record in a third-party system.

    Used by :class:`ProjectExtras` so an ``object/project`` node in the
    knowledge graph stays a lightweight handle while the live
    canonical state — title, status, milestones, etc. — is owned by
    the user's project / task manager (Linear, Notion, Todoist,
    GitHub, ...). Set via direct writes (MCP tool / sync job), not
    LLM extraction.
    """

    system: str = Field(
        description=(
            "Identifier of the external task / project manager "
            "(e.g. 'linear', 'notion', 'todoist', 'github')."
        ),
    )
    id: str = Field(
        description=(
            "Stable identifier the external system uses for this project "
            "(e.g. Linear project UUID, Notion page id, GitHub repo full name)."
        ),
    )
    url: str | None = Field(
        default=None,
        description=(
            "Optional canonical URL pointing at the project in the "
            "external system; surfaced in UIs that want a one-click "
            "jump-out."
        ),
    )


class ProjectExtras(BaseModel):
    """Extra properties layered on top of :class:`ObjectProperties` when
    a node is registered as ``(type='object', subtype='project')``.

    Only adds the optional ``external_ref`` handle today; the rest of
    the project's state lives in the third-party system and is fetched
    on demand by downstream agents.
    """

    external_ref: ExternalRef | None = Field(
        default=None,
        description=(
            "Lightweight handle to the richly-tracked record in the "
            "user's task / project manager. None for purely internal "
            "projects with no external mirror."
        ),
    )


# ---------------------------------------------------------------------------
# Subtype registrations
# ---------------------------------------------------------------------------
#
# Calling ``register_node_subtype`` mutates ``NODE_REGISTRY`` at import
# time. Re-registering the same subtype on the same parent is idempotent
# (the spec stores a frozenset; the union is a no-op when the subtype is
# already present). The ``description`` argument is accepted for
# forward-compat with the subtype-aware prompt landing in #030.

register_node_subtype(
    "object",
    "task",
    description="Action item or conversational throwaway extracted from a "
    "chunk (e.g. 'ship the demo by Friday'). Default Tree subtype for "
    "anything imperative or todo-shaped.",
)

register_node_subtype(
    "event",
    "episode",
    description="Retrospective life or work experience the user describes "
    "(e.g. 'first day at the new job', 'the launch outage'). Distinct from "
    "the canonical POLE+O event subtypes — episodes are user-narrative-shaped, "
    "not enumerable taxonomy.",
)

register_node_subtype(
    "object",
    "topic",
    description="Subject matter discussed in content (e.g. 'distributed "
    "systems', 'macroeconomics'). Used by the chunk-mentions edges so the "
    "graph carries thematic structure on top of named entities.",
)

register_node_subtype(
    "object",
    "project",
    description="Pointer to an externally-tracked project (Linear, Notion, "
    "GitHub, ...). Only carries an ``ExternalRef`` plus the canonical "
    "object properties; the rich state lives in the external system.",
    extra_properties=ProjectExtras,
)
