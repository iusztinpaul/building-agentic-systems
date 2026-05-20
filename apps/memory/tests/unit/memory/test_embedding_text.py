"""Unit tests for the shared node-text embedding module.

Covers the generic ``node_to_embedding_text`` builder (including a
byte-identical regression against the pre-refactor
``indexing.core._node_to_text`` layout) and the ``embed_node_texts``
batch helper.
"""

from typing import Any

from tree.memory.embedding_text import embed_node_texts, node_to_embedding_text
from tree.models.base import BaseEmbeddingModel


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingEmbeddingModel(BaseEmbeddingModel):
    """Records the texts it was asked to embed; returns indexed vectors."""

    def __init__(self, dimensions: int = 4) -> None:
        self._dimensions = dimensions
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        # One distinct vector per input, aligned positionally.
        return [[float(i)] * self._dimensions for i, _ in enumerate(texts)]


# ---------------------------------------------------------------------------
# node_to_embedding_text — byte-identical regression
# ---------------------------------------------------------------------------


class TestNodeToEmbeddingText:
    """Golden-literal regression: output must match the pre-refactor
    ``indexing.core._node_to_text`` exactly for each shape.

    The expected strings are hard-coded literals (not recomputed from the
    implementation) so a future edit to the builder that changes the
    layout fails this test rather than silently agreeing with itself.
    """

    def test_name_only(self) -> None:
        # Arrange
        node: dict[str, Any] = {
            "_id": "u:person:alice",
            "type": "person",
            "name": "Alice",
            "properties": {},
        }

        # Act
        text = node_to_embedding_text(node)

        # Assert
        assert text == "person: Alice"

    def test_name_with_properties(self) -> None:
        # Arrange
        node: dict[str, Any] = {
            "_id": "u:person:bob",
            "type": "person",
            "name": "Bob",
            "properties": {"role": "engineer", "team": "memory"},
        }

        # Act
        text = node_to_embedding_text(node)

        # Assert: type+headline first, then one line per non-content prop.
        assert text == "person: Bob\nrole: engineer\nteam: memory"

    def test_name_with_properties_and_content(self) -> None:
        # Arrange
        node: dict[str, Any] = {
            "_id": "u:chunk:c0",
            "type": "chunk",
            "name": "Chunk 0",
            "properties": {"source_type": "substack", "content": "Hello world body"},
        }

        # Act
        text = node_to_embedding_text(node)

        # Assert: content is appended LAST, after the other properties.
        assert text == "chunk: Chunk 0\nsource_type: substack\nHello world body"

    def test_headline_falls_back_to_canonical_name_then_id(self) -> None:
        # Arrange: no ``name`` -> canonical_name; no canonical_name -> _id.
        with_canonical: dict[str, Any] = {
            "_id": "u:person:carol",
            "type": "person",
            "canonical_name": "Carol",
            "properties": {},
        }
        with_only_id: dict[str, Any] = {
            "_id": "u:person:dave",
            "type": "person",
            "properties": {},
        }

        # Act / Assert
        assert node_to_embedding_text(with_canonical) == "person: Carol"
        assert node_to_embedding_text(with_only_id) == "person: u:person:dave"

    def test_missing_fields_yields_separator_only(self) -> None:
        # Arrange / Act
        text = node_to_embedding_text({})

        # Assert: backward-compat behavior from the pre-refactor builder.
        assert text == ": "


# ---------------------------------------------------------------------------
# embed_node_texts
# ---------------------------------------------------------------------------


class TestEmbedNodeTexts:
    async def test_embeds_each_node_text_in_a_single_call(self) -> None:
        # Arrange
        model = _RecordingEmbeddingModel(dimensions=3)
        nodes: list[dict[str, Any]] = [
            {"type": "person", "name": "Alice", "properties": {}},
            {
                "type": "chunk",
                "name": "Chunk 0",
                "properties": {"content": "body"},
            },
        ]

        # Act
        vectors = await embed_node_texts(nodes, model)

        # Assert: one embed() call carrying both node-texts, aligned output.
        assert model.calls == [["person: Alice", "chunk: Chunk 0\nbody"]]
        assert vectors == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]

    async def test_empty_input_returns_empty_without_calling_model(self) -> None:
        # Arrange
        model = _RecordingEmbeddingModel()

        # Act
        vectors = await embed_node_texts([], model)

        # Assert
        assert vectors == []
        assert model.calls == []
