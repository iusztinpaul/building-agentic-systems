"""Unit tests for the **Contextual header** (``tree.memory.rag.embedding``).

``child_embedding_text`` builds the ONLY text a **Child chunk** is embedded on
(ADR-006 §4). The stored ``properties.content`` stays the raw child text — the
header exists so a 256-token fragment carries the document title and the section
it came from into the vector, which the fragment itself does not say.
"""

from __future__ import annotations

import pytest

from tree.memory.rag.embedding import child_embedding_text


class TestChildEmbeddingText:
    def test_title_and_heading_path_are_prefixed(self) -> None:
        text = child_embedding_text(
            title="Memory for Agents",
            heading_path=["Retrieval", "Parents"],
            content="body",
        )

        assert text == "Memory for Agents\nRetrieval > Parents\n\nbody"

    def test_no_title_and_no_headings_returns_the_content(self) -> None:
        text = child_embedding_text(title=None, heading_path=[], content="body")

        assert text == "body"

    def test_title_only(self) -> None:
        text = child_embedding_text(
            title="Memory for Agents", heading_path=[], content="body"
        )

        assert text == "Memory for Agents\n\nbody"

    def test_heading_path_only(self) -> None:
        text = child_embedding_text(
            title=None, heading_path=["Retrieval", "Parents"], content="body"
        )

        assert text == "Retrieval > Parents\n\nbody"

    def test_single_heading_needs_no_separator(self) -> None:
        text = child_embedding_text(
            title=None, heading_path=["Retrieval"], content="body"
        )

        assert text == "Retrieval\n\nbody"

    @pytest.mark.parametrize("title", ["", "   "])
    def test_blank_title_is_treated_as_absent(self, title: str) -> None:
        """A source with an empty title must not prepend an empty line — that
        would embed a leading newline as if it were context."""

        text = child_embedding_text(title=title, heading_path=[], content="body")

        assert text == "body"

    def test_blank_heading_entries_are_dropped(self) -> None:
        text = child_embedding_text(
            title=None, heading_path=["Retrieval", "  ", "Parents"], content="body"
        )

        assert text == "Retrieval > Parents\n\nbody"

    def test_control_characters_are_stripped(self) -> None:
        """Voyage 400s on control characters and lone surrogates — the header
        goes through the SAME ``strip_invalid_chars`` as every other embedded
        text, with no second copy of the regex."""

        text = child_embedding_text(
            title="Memory\x00 for Agents",
            heading_path=["Retr\ud800ieval"],
            content="bo\x07dy",
        )

        assert text == "Memory for Agents\nRetrieval\n\nbody"

    def test_newlines_inside_content_survive(self) -> None:
        text = child_embedding_text(
            title=None, heading_path=[], content="first line\nsecond line"
        )

        assert text == "first line\nsecond line"

    def test_content_is_not_otherwise_rewritten(self) -> None:
        """The header is additive: the child's own text is passed through as-is
        (the Clean step already ran upstream)."""

        content = "It stores  parents without vectors."

        text = child_embedding_text(
            title="Memory for AI Agents",
            heading_path=["Parent-document retrieval"],
            content=content,
        )

        assert text.endswith(content)

    def test_is_keyword_only(self) -> None:
        with pytest.raises(TypeError):
            child_embedding_text("Memory", [], "body")  # type: ignore[misc]
