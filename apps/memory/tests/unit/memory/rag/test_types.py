"""Unit tests for the splitter's output shapes (``tree.memory.rag.types``)."""

from __future__ import annotations

from tree.memory.rag.types import ChildChunk, ParentChunk


class TestChildChunk:
    def test_requires_index_and_content(self) -> None:
        child = ChildChunk(index=2, content="body")

        assert (child.index, child.content) == (2, "body")

    def test_carries_no_heading_path_of_its_own(self) -> None:
        """A child inherits its parent's heading path (ADR-006 §6) — storing a
        second copy per child would be denormalisation nobody reads."""

        assert "heading_path" not in ChildChunk.model_fields


class TestParentChunk:
    def test_heading_path_and_children_default_to_empty(self) -> None:
        parent = ParentChunk(index=0, content="body")

        assert parent.heading_path == []
        assert parent.children == []

    def test_round_trips_through_model_dump(self) -> None:
        parent = ParentChunk(
            index=0,
            content="body",
            heading_path=["Memory", "Retrieval"],
            children=[ChildChunk(index=0, content="body")],
        )

        assert ParentChunk.model_validate(parent.model_dump()) == parent

    def test_defaults_are_not_shared_between_instances(self) -> None:
        first = ParentChunk(index=0, content="a")
        second = ParentChunk(index=1, content="b")

        first.heading_path.append("Memory")

        assert second.heading_path == []
