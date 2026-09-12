"""The **Contextual header**: what a **Child chunk** is actually embedded on.

ADR-006 §4. A 256-token child says "It stores parents without vectors." and
nothing about which document or section it came from, so the vector misses every
query phrased in the document's own vocabulary. ``child_embedding_text``
prefixes the document title and the parent's heading path::

    Memory for AI Agents
    Retrieval > Parent-document retrieval

    It stores parents without vectors.

This is the ONLY text embedded for a child. The stored ``properties.content``
stays the RAW child text, and both header inputs are denormalised onto the child
row (``properties.title`` / ``properties.heading_path``) so the indexing backfill
rebuilds a byte-identical string without joining back to the document.
"""

from __future__ import annotations

from tree.memory.rag.cleaning import strip_invalid_chars

_HEADING_SEPARATOR = " > "


def child_embedding_text(
    *, title: str | None, heading_path: list[str], content: str
) -> str:
    """Build the embedding text for one child chunk.

    Empty header parts are omitted rather than emitted blank: a document with no
    title and no headings embeds its plain content, never a leading empty line
    (which would spend tokens on punctuation and pull unrelated chunks
    together in vector space).

    The result goes through ``strip_invalid_chars`` — the ONE definition, shared
    with :mod:`tree.memory.embedding_text` — because Voyage's embeddings
    endpoint 400s on control characters and unpaired surrogates.
    """

    header_lines: list[str] = []
    if title and title.strip():
        header_lines.append(title.strip())

    headings = [heading.strip() for heading in heading_path if heading.strip()]
    if headings:
        header_lines.append(_HEADING_SEPARATOR.join(headings))

    if not header_lines:
        return strip_invalid_chars(content)

    return strip_invalid_chars("\n".join(header_lines) + "\n\n" + content)
