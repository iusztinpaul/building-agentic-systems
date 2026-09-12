"""In-memory shapes the RAG layer passes between its stages.

Two families live here:

* The SPLITTER's output — :class:`ParentChunk` / :class:`ChildChunk`. Not rows:
  the persisted shape is :class:`tree.entities.memory.MemoryEntry`
  (``type="chunk"`` with ``subtype="parent" | "child"``, ``parent_id`` and
  ``chunk_index``); the loader (#108) maps one :class:`ParentChunk` plus its
  children onto those rows.
* The RETRIEVAL side's output — :class:`ScoredHit` (what
  :func:`tree.memory.rag.search.hybrid_search` returns) and the
  **Parent-document retrieval** result tree :class:`RetrievalResult` ->
  :class:`RetrievedParent` -> :class:`MatchedChild` / :class:`DocumentMeta`.
  These ARE built from rows, but they are the OUTPUT contract of the rag layer
  (consumed by the CLI, the MCP ``search_memory`` tool and, for the seed half,
  by :mod:`tree.memory.graph.retrieval`), so a raw Mongo dict never leaks past
  :func:`tree.memory.rag.retrieval.retrieve_parents`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChildChunk(BaseModel):
    """A **Child chunk**: the small, embedded search unit (default 256 tokens).

    Carries no ``heading_path`` of its own — a child inherits its parent's
    (ADR-006 §6), so the **Contextual header** is built from the parent's path
    at embed time.
    """

    index: int = Field(
        description="0-based position among the siblings of ONE parent chunk."
    )
    content: str = Field(
        description="Raw child text — always a substring of the parent's content."
    )


class ParentChunk(BaseModel):
    """A **Parent chunk**: the large retrieval / LLM-extraction unit (4096 tokens).

    Never embedded. ``children`` are produced by re-splitting THIS chunk's
    ``content`` with the same strategy, so a child never crosses a parent
    boundary.
    """

    index: int = Field(description="0-based position of this parent in the document.")
    content: str = Field(description="Raw parent text.")
    heading_path: list[str] = Field(
        default_factory=list,
        description=(
            "Markdown heading stack in force at this parent's first token, "
            "outermost first (e.g. ['Memory', 'Parent retrieval']). Always "
            "empty under the fixed_tokens strategy."
        ),
    )
    children: list[ChildChunk] = Field(
        default_factory=list,
        description="The child chunks this parent was split into, in order.",
    )


# ---------------------------------------------------------------------------
# Retrieval shapes
# ---------------------------------------------------------------------------


class ScoredHit(BaseModel):
    """One fused hit of :func:`tree.memory.rag.search.hybrid_search`.

    ``doc`` is the RAW memory row (the hybrid search is the last place that
    speaks Mongo dicts) and ``score`` is its reciprocal-rank-fusion score —
    NOT a similarity: RRF sums ``1 / (k + rank)`` across the vector and text
    result lists, so values are small (~0.03 for a rank-0 hit at ``k=60``) and
    only comparable WITHIN one query.
    """

    doc: dict[str, Any] = Field(description="The raw ``memory`` row that matched.")
    score: float = Field(description="Fused RRF score; higher is better.")


SearchMode = Literal["hybrid", "text_only", "vector_only"]
"""**Search mode** — which legs of the hybrid search answered (ADR-008 §3).

A leg that RAN and matched nothing is still ``hybrid``: "empty" is a result,
"raised" is a mode. Both legs raising is
:class:`tree.memory.rag.search.SearchUnavailableError`, not a mode. NOT the
**Memory mode** (``rag`` / ``graphrag``): that is what the server IS, this is
how ONE query went.
"""


class HybridSearchResult(BaseModel):
    """What :func:`tree.memory.rag.search.hybrid_search` returns.

    The mode travels WITH the hits so a caller cannot read a degraded result as
    a complete one — pre-ADR-008 a dead leg was swallowed into ``[]`` and
    "Mongo is down" read exactly like "nothing matches".
    """

    hits: list[ScoredHit] = Field(
        default_factory=list, description="Fused hits, best score first."
    )
    search_mode: SearchMode = Field(
        default="hybrid",
        description=(
            "Which legs answered: ``hybrid`` = both ran, ``text_only`` = the "
            "vector leg was unavailable, ``vector_only`` = the text leg was "
            "unavailable."
        ),
    )


class DocumentMeta(BaseModel):
    """The ``document`` row's metadata, attached to every retrieved parent.

    Read from the document row referenced by the parent's ``parent_id`` — not
    from the chunk's denormalised copy — so a retrieval result always reports
    the CURRENT document metadata.
    """

    document_id: str = Field(description="``_id`` of the ``document`` row.")
    title: str | None = Field(default=None, description="Document title, if known.")
    source_type: str | None = Field(
        default=None, description="e.g. ``substack``, ``youtube``, ``file``."
    )
    source_uri: str | None = Field(
        default=None, description="Canonical URI the document was ingested from."
    )
    date: str | None = Field(
        default=None, description="Publication date as stored on the document row."
    )


class MatchedChild(BaseModel):
    """One **Child chunk** that actually matched the query.

    Kept alongside its parent so a caller can show WHY the parent ranked (and,
    later, feed only the matching passages to an LLM) without a second query.
    """

    child_id: str = Field(description="``_id`` of the child-chunk row.")
    chunk_index: int | None = Field(
        default=None, description="Position of the child among its parent's children."
    )
    content: str = Field(description="Raw child text (``properties.content``).")
    score: float = Field(description="The child's fused RRF score.")


class RetrievedParent(BaseModel):
    """One **Parent chunk** returned by **Parent-document retrieval**."""

    parent_id: str = Field(description="``_id`` of the parent-chunk row.")
    chunk_index: int | None = Field(
        default=None, description="Position of the parent in its document."
    )
    heading_path: list[str] = Field(
        default_factory=list,
        description="Markdown heading stack at the parent's start, outermost first.",
    )
    content: str = Field(description="Full parent text — the retrieval unit.")
    score: float = Field(
        description="Best fused score among ``matched_children`` — the rank key."
    )
    document: DocumentMeta = Field(description="Metadata of the owning document.")
    matched_children: list[MatchedChild] = Field(
        default_factory=list, description="Every matched child, best score first."
    )


RetrievalOutcome = Literal["found", "nothing_found"]
"""**Retrieval outcome** — did the search find anything at all (ADR-008 §3)?

``nothing_found`` iff ZERO hits survived the seed search, where the vector leg
was gated on ``query.min_vector_score`` before fusion. It is computed on the
hits, BEFORE parent grouping: a hit whose parent row is missing is a data
problem (logged, dropped), not "nothing found". NOT a **Search mode**: that says
which legs answered, this says whether the answer is empty.
"""


class RetrievalResult(BaseModel):
    """What ``rag`` mode returns instead of a graph: ranked parents, no edges."""

    parents: list[RetrievedParent] = Field(
        default_factory=list, description="Best-scoring parent first."
    )
    outcome: RetrievalOutcome = Field(
        default="found",
        description=(
            "``nothing_found`` iff the seed search kept no hits (the vector leg "
            "gated on ``query.min_vector_score``, the text leg ungated); "
            "``found`` otherwise."
        ),
    )
    search_mode: SearchMode = Field(
        default="hybrid",
        description=(
            "The **Search mode** the seed search ran in, copied off "
            "``HybridSearchResult``: ``text_only`` / ``vector_only`` means one "
            "leg was unavailable, so these parents may miss matches."
        ),
    )
