"""Unit tests for the shared node-text embedding module.

Covers the generic ``node_to_embedding_text`` builder (including a
byte-identical regression against the pre-refactor
``indexing.core._node_to_text`` layout), the ``embed_node_texts`` batch
helper, and the #044 real-time request batcher ``embed_in_batches``
(chunking by input-count AND token-budget caps, order preservation across
multiple requests).
"""

from typing import Any

import pytest

from tree.memory.embedding_text import (
    _embed_chunk_resilient,
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

    def test_strips_control_chars_and_surrogates(self) -> None:
        # Arrange: chunk content with C0 controls, DEL, a C1 char, and an
        # unpaired surrogate — the shapes Voyage 400s on.
        node: dict[str, Any] = {
            "_id": "u:chunk:c1",
            "type": "chunk",
            "name": "chunk\x00one",
            "properties": {"content": "good\x07text\x7f\x9fwith\ud800junk\nkept"},
        }

        # Act
        text = node_to_embedding_text(node)

        # Assert: control/surrogate chars stripped, ordinary text + newline kept.
        assert text == "chunk: chunkone\ngoodtextwithjunk\nkept"


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


# ---------------------------------------------------------------------------
# embed_in_batches — skip-and-continue on Voyage content rejections
# ---------------------------------------------------------------------------


class _PoisonEmbeddingModel(BaseEmbeddingModel):
    """400s on any batch containing a poison text; else returns indexed vectors.

    Models Voyage's content rejection: a 400 fails the whole request, so a
    resilient batcher must bisect to isolate and skip the poison input.
    """

    def __init__(self, poison: set[str]) -> None:
        self._poison = poison
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return 2

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from tree.models.exceptions import ExtractionError

        self.calls.append(list(texts))
        if any(t in self._poison for t in texts):
            raise ExtractionError(
                "Voyage multimodal API error 400: inputs contain invalid elements",
                status_code=400,
            )
        return [[1.0, 1.0] for _ in texts]


class _RateLimitedEmbeddingModel(BaseEmbeddingModel):
    """Always raises a 429-style exhaustion error (transient, must NOT skip)."""

    @property
    def dimensions(self) -> int:
        return 2

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from tree.models.exceptions import ExtractionError

        raise ExtractionError(
            "Voyage multimodal API error 429: rate-limit retries exhausted (...)",
            status_code=429,
        )


class TestEmbedInBatchesSkipsContentRejections:
    async def test_skips_poison_input_keeps_order(self) -> None:
        # Arrange: the middle text is rejected with a 400.
        model = _PoisonEmbeddingModel(poison={"bad"})
        texts = ["a", "bad", "c"]

        # Act
        vectors = await embed_in_batches(texts, model, max_inputs=1000)

        # Assert: good inputs embedded, poison skipped with an aligned [] slot.
        assert vectors == [[1.0, 1.0], [], [1.0, 1.0]]

    async def test_rate_limit_propagates_not_skipped(self) -> None:
        # Arrange
        from tree.models.exceptions import ExtractionError

        model = _RateLimitedEmbeddingModel()

        # Act / Assert: a 429 is transient — it must raise, never be skipped.
        with pytest.raises(ExtractionError, match="429"):
            await embed_in_batches(["a", "b"], model, max_inputs=1000)


class _IdentityEncodingPoisonModel(BaseEmbeddingModel):
    """Encodes each input's identity into its vector; 400s on any poison batch.

    Unlike ``_PoisonEmbeddingModel`` (which returns a uniform ``[1.0, 1.0]`` for
    every good input and so cannot detect a positional swap), this model returns
    a vector derived from the text itself — ``[ord(text[0])]`` — so the returned
    sequence is a fingerprint of which good input landed in which slot. A
    batcher that mis-aligned the bisected halves would produce a different
    sequence, so vector equality proves exact positional alignment.
    """

    def __init__(self, poison: set[str]) -> None:
        self._poison = poison
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return 1

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from tree.models.exceptions import ExtractionError

        self.calls.append(list(texts))
        if any(t in self._poison for t in texts):
            raise ExtractionError(
                "Voyage multimodal API error 400: inputs contain invalid elements",
                status_code=400,
            )
        return [[float(ord(t[0]))] for t in texts]


class TestEmbedInBatchesAlignmentAdversarial:
    """Adversarial: multi-poison bisection must keep good vectors aligned."""

    async def test_multi_poison_preserves_exact_alignment(self) -> None:
        # Arrange: poison at index 1 and index 4 of a 6-input chunk. Forcing a
        # single request (max_inputs high) makes the batcher bisect repeatedly.
        model = _IdentityEncodingPoisonModel(poison={"P1", "P4"})
        texts = ["a", "P1", "c", "d", "P4", "f"]

        # Act
        vectors = await embed_in_batches(texts, model, max_inputs=1000)

        # Assert: good inputs land in their own slots (fingerprinted by ord), the
        # two poison slots are empty placeholders — no drift, no swap, no drop.
        assert vectors == [
            [float(ord("a"))],
            [],
            [float(ord("c"))],
            [float(ord("d"))],
            [],
            [float(ord("f"))],
        ]
        assert len(vectors) == len(texts)

    async def test_all_poison_chunk_yields_all_placeholders(self) -> None:
        # Arrange: every input is rejected — full bisection down to singletons.
        model = _IdentityEncodingPoisonModel(poison={"x", "y", "z"})
        texts = ["x", "y", "z"]

        # Act
        vectors = await embed_in_batches(texts, model, max_inputs=1000)

        # Assert: each un-embeddable input gets its own aligned [] placeholder.
        assert vectors == [[], [], []]

    async def test_429_message_containing_400_still_propagates(self) -> None:
        # Arrange: a transient 429 whose human-readable message happens to contain
        # the digit-run "400" (token counts, Retry-After, request IDs, quota
        # numbers all interpolate the server body verbatim) must NOT be skipped.
        # The discriminator keys off the structured HTTP status, not the message,
        # so a 429 propagates even when "400" appears in its text.
        from tree.models.exceptions import ExtractionError

        class _Misleading400In429Model(BaseEmbeddingModel):
            @property
            def dimensions(self) -> int:
                return 2

            async def embed(self, texts: list[str]) -> list[list[float]]:
                raise ExtractionError(
                    "Voyage API error 429: rate-limit exhausted after 400 retries",
                    status_code=429,
                )

        # Act / Assert: regression guard — a transient 429 must raise, never be
        # bisected and silently dropped, regardless of "400" appearing in the body.
        with pytest.raises(ExtractionError, match="429"):
            await embed_in_batches(
                ["a", "b"], _Misleading400In429Model(), max_inputs=1000
            )

    async def test_status_less_400_message_still_propagates(self) -> None:
        # Arrange: an ExtractionError with NO structured status_code, whose
        # message contains "400", must NOT be treated as a content rejection.
        # Only a structured status_code == 400 is skippable; anything else
        # (including a status-less error) re-raises so no data is silently lost.
        from tree.models.exceptions import ExtractionError

        class _StatusLess400MessageModel(BaseEmbeddingModel):
            @property
            def dimensions(self) -> int:
                return 2

            async def embed(self, texts: list[str]) -> list[list[float]]:
                raise ExtractionError("some failure mentioning 400 in passing")

        with pytest.raises(ExtractionError, match="400"):
            await embed_in_batches(
                ["a", "b"], _StatusLess400MessageModel(), max_inputs=1000
            )


class TestEmbedChunkResilientDoesNotRateLimit:
    """ADR-002 §1 (amended): the ``voyage-embeddings`` rate limit lives at the
    real network POST inside the Voyage clients, NOT in ``embedding_text``.

    ``_embed_chunk_resilient`` must NOT import or call ``rate_limit`` — gating it
    here throttled zero-POST ``_CachedSingleEmbedding`` cache hits and timed out
    extraction. The per-client acquisition is asserted in the Voyage client test
    modules (``test_voyage_embedding.py`` / ``test_voyage_multimodal_embedding.py``).
    """

    def test_embedding_text_module_has_no_rate_limit_symbol(self) -> None:
        # Arrange / Act: import the module the chokepoint used to live in.
        import tree.memory.embedding_text as embedding_text_module

        # Assert: the rate-limit symbol and import are gone from this module —
        # the wrap relocated to the Voyage clients.
        assert not hasattr(embedding_text_module, "rate_limit")
        assert not hasattr(embedding_text_module, "_VOYAGE_EMBED_LIMIT")

    async def test_cached_model_chunk_issues_no_real_post_and_no_throttle(
        self,
    ) -> None:
        # Arrange: a no-network model (the ``_CachedSingleEmbedding`` shape) —
        # returns a pre-computed vector for every input, no Voyage client reached.
        class _CachedModel(BaseEmbeddingModel):
            @property
            def dimensions(self) -> int:
                return 2

            async def embed(self, texts: list[str]) -> list[list[float]]:
                return [[9.0, 9.0] for _ in texts]

        model = _CachedModel()

        # Act: route through the resilient chokepoint as the dedup path does.
        vectors = await _embed_chunk_resilient(model, ["entity text"])

        # Assert: the cached vector returns unchanged; because no Voyage client
        # is reached, no ``voyage-embeddings`` slot is ever acquired (the timeout
        # regression). The per-client acquisition is asserted in the client tests.
        assert vectors == [[9.0, 9.0]]


class TestDispatchConcurrencyDefault:
    """``dispatch_concurrency=1`` (default) keeps dispatch sequential.

    The knob is the seam to flip on only after the Voyage cap is lifted; at the
    default it must not change request count or ordering vs the pre-task code.
    """

    async def test_default_one_preserves_request_count_and_order(self) -> None:
        # Arrange: confirm the wired-in default is 1, then drive a multi-chunk
        # batch and assert the request count + global ordering are unchanged.
        from tree.config.app_config import app_config

        assert app_config.models.embedding_batch.dispatch_concurrency == 1

        model = _OrderEncodingEmbeddingModel()
        texts = [f"t{i}" for i in range(7)]

        # Act
        vectors = await embed_in_batches(texts, model, max_inputs=3)

        # Assert: identical to the pre-task sequential batcher — 3 + 3 + 1
        # requests, vectors in global input order.
        assert [len(c) for c in model.calls] == [3, 3, 1]
        assert vectors == [[float(i)] for i in range(7)]


class TestSanitizeForEmbedding:
    """Adversarial: sanitization strips only the chars Voyage 400s on."""

    def test_preserves_legitimate_unicode_tab_and_newline(self) -> None:
        from tree.memory.embedding_text import _sanitize_for_embedding

        # Arrange: smart quotes, emoji, accented letters, tab, newline, CR — all
        # legitimate and must survive sanitization untouched.
        text = "café “smart” \U0001f600\taccenté\nline\rret"

        # Act / Assert: no-op on clean-but-rich Unicode.
        assert _sanitize_for_embedding(text) == text

    def test_strips_each_invalid_class(self) -> None:
        from tree.memory.embedding_text import _sanitize_for_embedding

        # Arrange: one char from each stripped class — C0 (NUL, BEL, VT, FF),
        # DEL, C1 (0x80, 0x9f), and an unpaired surrogate (0xd800).
        text = "x\x00\x07\x0b\x0c\x1f\x7f\x80\x9f\ud800y"

        # Act / Assert: everything between the bookends is removed.
        assert _sanitize_for_embedding(text) == "xy"

    def test_no_op_on_plain_ascii(self) -> None:
        from tree.memory.embedding_text import _sanitize_for_embedding

        # Arrange / Act / Assert: ordinary text is untouched (cheap fast path).
        assert _sanitize_for_embedding("person: Bob\nrole: engineer") == (
            "person: Bob\nrole: engineer"
        )
