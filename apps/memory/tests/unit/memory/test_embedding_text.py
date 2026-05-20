"""Unit tests for the shared node-text embedding module.

Covers the generic ``node_to_embedding_text`` builder (including a
byte-identical regression against the pre-refactor
``indexing.core._node_to_text`` layout), the ``embed_node_texts`` batch
helper, and the #044 real-time request batcher ``embed_in_batches``
(chunking by input-count AND token-budget caps, order preservation across
multiple requests).
"""

from typing import Any

from tree.memory.embedding_text import (
    embed_in_batches,
    embed_node_texts,
    estimate_tokens,
    node_to_embedding_text,
)
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


class _OrderEncodingEmbeddingModel(BaseEmbeddingModel):
    """Encodes the GLOBAL input position into each returned vector.

    Each ``embed(chunk)`` call returns ``[[0.0], [1.0], ...]`` for the chunk
    (per-request, position-encoded). The model also stamps a monotonically
    increasing global offset so that a correct batcher — which concatenates
    per-chunk results in chunk order — yields ``[[0.0], [1.0], ..., [N-1.0]]``
    across the whole input. A batcher that reordered or dropped a chunk would
    produce a different sequence, so vector equality proves order preservation.
    """

    def __init__(self, dimensions: int = 1) -> None:
        self._dimensions = dimensions
        self.calls: list[list[str]] = []
        self._global_offset = 0

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = [
            [float(self._global_offset + i)] * self._dimensions
            for i, _ in enumerate(texts)
        ]
        self._global_offset += len(texts)
        return out


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

    async def test_batches_many_nodes_into_multiple_requests(self) -> None:
        # Arrange — 2,500 short node-texts, capped at 1000 inputs per request.
        model = _OrderEncodingEmbeddingModel()
        nodes: list[dict[str, Any]] = [
            {"type": "person", "name": f"p{i}", "properties": {}} for i in range(2500)
        ]

        # Act — override the caps explicitly so the test is independent of YAML.
        vectors = await embed_node_texts(nodes, model, max_inputs=1000)

        # Assert — 3 requests (1000 + 1000 + 500), 2500 vectors, original order.
        assert [len(c) for c in model.calls] == [1000, 1000, 500]
        assert len(vectors) == 2500
        assert vectors == [[float(i)] for i in range(2500)]


# ---------------------------------------------------------------------------
# estimate_tokens — conservative heuristic
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_empty_text_is_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_non_empty_text_is_at_least_one(self) -> None:
        assert estimate_tokens("a") >= 1

    def test_over_counts_relative_to_four_chars_per_token(self) -> None:
        # 300 chars: a 4-chars/token tokenizer would say ~75 tokens; our
        # conservative 3-chars/token heuristic over-counts (~100+) so we
        # stay safely under the API caps.
        text = "x" * 300
        assert estimate_tokens(text) > 75


# ---------------------------------------------------------------------------
# embed_in_batches — #044 real-time request batcher
# ---------------------------------------------------------------------------


class TestEmbedInBatches:
    async def test_empty_input_returns_empty_without_calling_model(self) -> None:
        # Arrange
        model = _OrderEncodingEmbeddingModel()

        # Act
        vectors = await embed_in_batches([], model)

        # Assert
        assert vectors == []
        assert model.calls == []

    async def test_splits_2500_short_texts_into_three_chunks_by_input_count(
        self,
    ) -> None:
        # Arrange — AC: 2,500 short texts → exactly 3 chunks (1000 + 1000 + 500)
        # by the 1000-input cap, with a generous token cap so only the count
        # cap fires.
        model = _OrderEncodingEmbeddingModel()
        texts = [f"t{i}" for i in range(2500)]

        # Act
        vectors = await embed_in_batches(
            texts,
            model,
            max_inputs=1000,
            max_total_tokens=10_000_000,
        )

        # Assert — three requests of the expected sizes.
        assert [len(c) for c in model.calls] == [1000, 1000, 500]
        # 2,500 vectors in original input order.
        assert len(vectors) == 2500
        assert vectors == [[float(i)] for i in range(2500)]

    async def test_splits_by_token_cap_even_under_input_count_cap(self) -> None:
        # Arrange — AC: long texts that blow the total-token cap split into
        # multiple chunks even though the count stays under max_inputs.
        # Each text ~ 3000 chars → ~1001 estimated tokens. With a 2500-token
        # total cap, only 2 texts fit per request (2002 < 2500, 3003 > 2500).
        model = _OrderEncodingEmbeddingModel()
        texts = ["x" * 3000 for _ in range(5)]

        # Act — input-count cap (1000) is far above the 5 texts, so any split
        # is driven purely by the token cap.
        vectors = await embed_in_batches(
            texts,
            model,
            max_inputs=1000,
            max_total_tokens=2500,
            max_input_tokens=32_000,
        )

        # Assert — split into >1 chunk by tokens; 2 texts per request → 3 chunks
        # (2 + 2 + 1). Crucially NOT a single request.
        assert len(model.calls) > 1
        assert [len(c) for c in model.calls] == [2, 2, 1]
        assert len(vectors) == 5
        assert vectors == [[float(i)] for i in range(5)]

    async def test_vectors_returned_in_input_order_across_chunks(self) -> None:
        # Arrange — AC: order-preservation. The mock returns an index-encoding
        # vector PER REQUEST; embed_in_batches must stitch chunks back so the
        # final list reflects the GLOBAL input order, not per-chunk order.
        model = _OrderEncodingEmbeddingModel()
        texts = [f"text-{i}" for i in range(7)]

        # Act — force 3 chunks of 3 + 3 + 1.
        vectors = await embed_in_batches(texts, model, max_inputs=3)

        # Assert — globally ordered 0..6 despite per-request resets.
        assert [len(c) for c in model.calls] == [3, 3, 1]
        assert vectors == [[float(i)] for i in range(7)]

    async def test_single_oversized_input_still_forms_a_request(self) -> None:
        # Arrange — a single text whose estimate exceeds the per-input cap.
        # It must still go out as its own request (relying on the model's
        # truncation=True), not block the batcher.
        model = _OrderEncodingEmbeddingModel()
        huge = "z" * 200_000  # ~66K estimated tokens, over the 32K per-input cap

        # Act
        vectors = await embed_in_batches(
            [huge],
            model,
            max_inputs=1000,
            max_total_tokens=320_000,
            max_input_tokens=32_000,
        )

        # Assert — exactly one request carrying the one (clamped) input.
        assert len(model.calls) == 1
        assert len(model.calls[0]) == 1
        assert vectors == [[0.0]]

    async def test_oversized_input_clamped_then_packed_with_neighbors(self) -> None:
        # Arrange — an oversized input clamps to max_input_tokens for
        # accounting, so it shares a request with following small texts when
        # the total cap allows.
        model = _OrderEncodingEmbeddingModel()
        huge = "z" * 200_000  # clamps to max_input_tokens (1000) for accounting
        texts = [huge, "small-a", "small-b"]

        # Act — total cap (5000) comfortably holds clamped(1000) + tiny + tiny.
        vectors = await embed_in_batches(
            texts,
            model,
            max_inputs=1000,
            max_total_tokens=5000,
            max_input_tokens=1000,
        )

        # Assert — a single request (clamping prevented an over-cap rollover).
        assert len(model.calls) == 1
        assert len(model.calls[0]) == 3
        assert vectors == [[0.0], [1.0], [2.0]]
