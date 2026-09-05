"""In-memory shapes the RAG layer passes between its stages.

These are the SPLITTER's output — not rows. The persisted shape is
:class:`tree.entities.memory.MemoryEntry` (``type="chunk"`` with
``subtype="parent" | "child"``, ``parent_id`` and ``chunk_index``); the loader
(#108) maps one :class:`ParentChunk` plus its children onto those rows.
"""

from __future__ import annotations

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
