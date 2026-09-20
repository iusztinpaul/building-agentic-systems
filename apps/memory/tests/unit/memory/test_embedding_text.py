"""Unit tests for the shared node-text embedding module.

Covers the generic ``node_to_embedding_text`` builder (including a
byte-identical regression against the pre-refactor
``indexing.core._node_to_text`` layout), the per-type
``entity_embedding_text`` chooser and the ONE prospective-row builder
``prospective_entity_embedding_text`` both inline writers call, the
``embed_texts`` seam the indexing backfill calls, and the #044 real-time
request batcher ``embed_in_batches`` (chunking by input-count AND
token-budget caps, order preservation across multiple requests).
"""

import ast
from pathlib import Path
from typing import Any

import pytest

import tree.memory
from tree.entities.memory import NodeType
from tree.memory import embedding_text
from tree.memory.embedding_text import (
    _embed_chunk_resilient,
    embed_in_batches,
    embed_texts,
    entity_embedding_text,
    estimate_tokens,
    node_to_embedding_text,
    prospective_entity_embedding_text,
)
from tree.memory.graph import add_entity as add_entity_module
from tree.memory.pipeline import prospective_entity_embedding_text as pipeline_builder
from tree.models.base import BaseEmbeddingModel, EmbeddingRole

_SRC_DIR = Path(tree.memory.__file__).parents[1]


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

    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
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
        # One entry per request, so a role that reaches only the FIRST chunk
        # is visible as a shorter/mixed list rather than a passing test.
        self.roles: list[EmbeddingRole | None] = []
        self._global_offset = 0

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
        self.calls.append(list(texts))
        self.roles.append(input_type)
        out = [
            [float(self._global_offset + i)] * self._dimensions
            for i, _ in enumerate(texts)
        ]
        self._global_offset += len(texts)
        return out


# ---------------------------------------------------------------------------
# entity_embedding_text — FACT ``object`` vs legacy ``object_`` precedence
# ---------------------------------------------------------------------------


class TestFactObjectPrecedence:
    """``object`` wins on PRESENCE, never on truthiness.

    A row written across the ``object_`` → ``object`` rename can carry BOTH: the
    current value under ``object`` and a STALE pre-rename value under
    ``object_``. The old ``properties.get("object") or properties.get("object_")``
    read the stale one whenever the current value was falsy — so the row embedded
    on text the user has since replaced (and the indexing backfill, reading the
    same properties, would keep reproducing it after an **Embedding reset**).
    A blank ``object`` means "this row has no usable object text": it falls back
    to the generic node-text, which still surfaces every property, including
    ``object_``, in a labelled line rather than presenting the stale value AS the
    row's meaning.
    """

    def _fact_row(self, properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": NodeType.FACT.value,
            "name": "paul-lives-in",
            "canonical_name": "paul-lives-in",
            "properties": properties,
        }

    def test_current_object_wins_over_the_legacy_one(self) -> None:
        row = self._fact_row({"object": "new", "object_": "old"})

        assert entity_embedding_text(row) == "new"

    def test_legacy_object_is_read_when_object_is_absent(self) -> None:
        row = self._fact_row({"object_": "old"})

        assert entity_embedding_text(row) == "old"

    def test_legacy_object_is_read_when_object_is_none(self) -> None:
        # ``None`` is "not written", not "written blank" — the legacy column is
        # the only value this row has.
        row = self._fact_row({"object": None, "object_": "old"})

        assert entity_embedding_text(row) == "old"

    @pytest.mark.parametrize(
        "blank_object",
        ["", "  ", "\x00", "\x00 \ud800"],
        ids=["empty", "whitespace", "control-char", "all-invalid"],
    )
    def test_a_blank_object_falls_back_to_node_text_not_to_the_legacy_one(
        self, blank_object: str
    ) -> None:
        # The regression this class exists for: the stale ``object_`` must NOT
        # become the embedded text just because the current value is blank.
        row = self._fact_row({"object": blank_object, "object_": "old"})

        text = entity_embedding_text(row)

        assert text == node_to_embedding_text(row)
        assert text != "old"

    def test_a_non_string_object_falls_back_to_node_text(self) -> None:
        # A falsy non-string (``0``, ``False``, ``[]``) is still a PRESENT
        # value: it may not hand the row over to the legacy column either.
        row = self._fact_row({"object": 0, "object_": "old"})

        text = entity_embedding_text(row)

        assert text == node_to_embedding_text(row)
        assert text != "old"


# ---------------------------------------------------------------------------
# prospective_entity_embedding_text — ONE persisted-row builder
# ---------------------------------------------------------------------------


def _function_definitions(path: Path) -> set[str]:
    """Every ``def``/``async def`` name defined in the file at ``path``."""

    module_ast = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(module_ast)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


class TestProspectiveEntityEmbeddingText:
    """The persisted-row shape is built in ONE place, for both inline writers.

    ``add_entity`` (dedup query vector, reused as the persisted vector) and the
    pipeline's task ④ (the ``_CachedSingleEmbedding`` lookup key) used to each
    assemble the same ``{type, name, canonical_name, properties-minus-aliases-
    and-confidence}`` dict by hand, promising "byte-for-byte" agreement in a
    docstring. Two copies is how the key and the embedded text drift apart; one
    function makes the promise structural.
    """

    def test_prospective_entity_text_is_one_function(self) -> None:
        # Arrange: the two private builders that used to hold the duplicate.
        duplicated = {"_embeddable_text", "_entity_embeddable_text"}

        # Act: scan the shipped source, not just the two modules under test — a
        # third hand-built copy anywhere is the same bug.
        offenders = {
            f"{path.relative_to(_SRC_DIR)}::{name}"
            for path in sorted(_SRC_DIR.rglob("*.py"))
            if "__pycache__" not in path.parts
            for name in _function_definitions(path) & duplicated
        }

        assert not offenders, f"the persisted-row dict is still built in {offenders}"
        # Both call sites reach the SAME object, not two same-looking ones.
        assert (
            add_entity_module.prospective_entity_embedding_text
            is prospective_entity_embedding_text
        )
        assert pipeline_builder is prospective_entity_embedding_text

    @pytest.mark.parametrize(
        "entity_type,properties",
        [
            (NodeType.PERSON, {"role": "researcher"}),
            (NodeType.PREFERENCE, {"statement": "prefers dark mode"}),
            (NodeType.FACT, {"subject": "paul", "object": "Bucharest"}),
        ],
        ids=["generic", "preference", "fact"],
    )
    def test_output_equals_entity_embedding_text_on_the_persisted_row(
        self, entity_type: NodeType, properties: dict[str, Any]
    ) -> None:
        # Arrange: the inline path sees ``aliases`` / ``confidence`` under
        # ``properties``; the persisted row never does (``_upsert_node`` promotes
        # them to top-level columns), so the two texts agree only if the builder
        # strips them.
        inline_properties = {**properties, "aliases": ["AK"], "confidence": 0.9}
        persisted_row = {
            "type": entity_type.value,
            "name": "incoming name",
            "canonical_name": "canonical name",
            "properties": properties,
        }

        text = prospective_entity_embedding_text(
            entity_type=entity_type,
            name="incoming name",
            canonical_name="canonical name",
            properties=inline_properties,
        )

        assert text == entity_embedding_text(persisted_row)
        assert "AK" not in text
        assert "confidence" not in text


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
        node: dict[str, Any] = {
            "_id": "u:person:alice",
            "type": "person",
            "name": "Alice",
            "properties": {},
        }

        text = node_to_embedding_text(node)

        assert text == "person: Alice"

    def test_name_with_properties(self) -> None:
        node: dict[str, Any] = {
            "_id": "u:person:bob",
            "type": "person",
            "name": "Bob",
            "properties": {"role": "engineer", "team": "memory"},
        }

        text = node_to_embedding_text(node)

        # Assert: type+headline first, then one line per non-content prop.
        assert text == "person: Bob\nrole: engineer\nteam: memory"

    def test_name_with_properties_and_content(self) -> None:
        node: dict[str, Any] = {
            "_id": "u:chunk:c0",
            "type": "chunk",
            "name": "Chunk 0",
            "properties": {"source_type": "substack", "content": "Hello world body"},
        }

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

        assert node_to_embedding_text(with_canonical) == "person: Carol"
        assert node_to_embedding_text(with_only_id) == "person: u:person:dave"

    def test_missing_fields_yields_separator_only(self) -> None:
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

        text = node_to_embedding_text(node)

        # Assert: control/surrogate chars stripped, ordinary text + newline kept.
        assert text == "chunk: chunkone\ngoodtextwithjunk\nkept"


# ---------------------------------------------------------------------------
# embed_texts — the seam the indexing backfill calls
# ---------------------------------------------------------------------------


class TestEmbedTexts:
    """``embed_texts`` is the whole embedding API next to the text builder.

    The backfill builds its own texts (a **Child chunk** embeds its
    **Contextual header**, an entity row its ``node_to_embedding_text``) and
    hands them here, so these cases drive it with node-texts — the caps,
    request count and positional alignment are what the backfill relies on.
    """

    async def test_embeds_each_node_text_in_a_single_call(self) -> None:
        model = _RecordingEmbeddingModel(dimensions=3)
        nodes: list[dict[str, Any]] = [
            {"type": "person", "name": "Alice", "properties": {}},
            {
                "type": "chunk",
                "name": "Chunk 0",
                "properties": {"content": "body"},
            },
        ]

        vectors = await embed_texts([node_to_embedding_text(n) for n in nodes], model)

        # Assert: one embed() call carrying both node-texts, aligned output.
        assert model.calls == [["person: Alice", "chunk: Chunk 0\nbody"]]
        assert vectors == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]

    async def test_control_chars_never_reach_the_model(self) -> None:
        # The sanitizer runs in the text builder, so what the seam sends is
        # already free of the C0/C1/surrogate shapes Voyage 400s on.
        model = _RecordingEmbeddingModel(dimensions=3)
        node: dict[str, Any] = {
            "type": "chunk",
            "name": "chunk\x00one",
            "properties": {"content": "good\x07text\ud800junk"},
        }

        await embed_texts([node_to_embedding_text(node)], model)

        assert model.calls == [["chunk: chunkone\ngoodtextjunk"]]

    async def test_empty_input_returns_empty_without_calling_model(self) -> None:
        model = _RecordingEmbeddingModel()

        vectors = await embed_texts([], model)

        assert vectors == []
        assert model.calls == []

    async def test_batches_many_texts_into_multiple_requests(self) -> None:
        # Arrange — 2,500 short node-texts, capped at 1000 inputs per request.
        model = _OrderEncodingEmbeddingModel()
        texts = [
            node_to_embedding_text({"type": "person", "name": f"p{i}"})
            for i in range(2500)
        ]

        # Act — override the caps explicitly so the test is independent of YAML.
        vectors = await embed_texts(texts, model, max_inputs=1000)

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
        model = _OrderEncodingEmbeddingModel()

        vectors = await embed_in_batches([], model)

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

    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
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

    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
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

        vectors = await embed_in_batches(texts, model, max_inputs=1000)

        # Assert: good inputs embedded, poison skipped with an aligned [] slot.
        assert vectors == [[1.0, 1.0], [], [1.0, 1.0]]

    async def test_rate_limit_propagates_not_skipped(self) -> None:
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
        # One entry per request, INCLUDING the bisected halves — that is where
        # a role would get dropped if the recursion forgot to pass it on.
        self.roles: list[EmbeddingRole | None] = []

    @property
    def dimensions(self) -> int:
        return 1

    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
        from tree.models.exceptions import ExtractionError

        self.calls.append(list(texts))
        self.roles.append(input_type)
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

            async def embed(
                self, texts: list[str], input_type: EmbeddingRole | None = None
            ) -> list[list[float]]:
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

            async def embed(
                self, texts: list[str], input_type: EmbeddingRole | None = None
            ) -> list[list[float]]:
                raise ExtractionError("some failure mentioning 400 in passing")

        with pytest.raises(ExtractionError, match="400"):
            await embed_in_batches(
                ["a", "b"], _StatusLess400MessageModel(), max_inputs=1000
            )


class TestInputTypeThreading:
    """The **Embedding role** must survive every hop of the batcher (ADR-009 §5).

    Between the caller and ``.embed(...)`` sit two loops that can silently drop
    it: the per-chunk dispatch in ``embed_in_batches`` and the bisect recursion
    in ``_embed_chunk_resilient``. A role that reaches only the first request
    would mix ``document`` and role-less vectors inside ONE persisted batch —
    invisible in the vector shape, fatal to retrieval.
    """

    async def test_every_chunk_carries_the_role(self) -> None:
        # Arrange: 5 texts under a 2-input cap -> 3 requests.
        model = _OrderEncodingEmbeddingModel()
        texts = [f"t{i}" for i in range(5)]

        vectors = await embed_in_batches(
            texts, model, input_type="document", max_inputs=2
        )

        # Assert: three requests, EACH carrying the role; vectors still aligned.
        assert [len(c) for c in model.calls] == [2, 2, 1]
        assert model.roles == ["document", "document", "document"]
        assert vectors == [[float(i)] for i in range(5)]

    async def test_bisect_preserves_the_role(self) -> None:
        # Arrange: one poison input inside a single request, so the 400 handler
        # bisects — the halves are separate ``.embed`` calls.
        model = _IdentityEncodingPoisonModel(poison={"P1"})
        texts = ["a", "P1", "c", "d"]

        vectors = await embed_in_batches(
            texts, model, input_type="document", max_inputs=1000
        )

        # Assert: the poison slot keeps its aligned ``[]`` placeholder ...
        assert vectors == [
            [float(ord("a"))],
            [],
            [float(ord("c"))],
            [float(ord("d"))],
        ]
        # ... and every request of the recursion carried the role.
        assert len(model.calls) > 1
        assert model.roles == ["document"] * len(model.calls)

    async def test_default_is_role_less(self) -> None:
        # Symmetric callers (resolution) pass nothing and must stay role-less —
        # the default may never quietly become ``document``.
        model = _OrderEncodingEmbeddingModel()

        await embed_in_batches(["a", "b"], model, max_inputs=1)

        assert model.roles == [None, None]

    async def test_embed_texts_forwards_the_role(self) -> None:
        # ``embed_texts`` is the seam the indexing backfill calls; it resolves
        # the caps from YAML and must not swallow the role on the way.
        model = _OrderEncodingEmbeddingModel()

        await embed_texts(["a", "b"], model, input_type="document", max_inputs=1)

        assert model.roles == ["document", "document"]


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

            async def embed(
                self, texts: list[str], input_type: EmbeddingRole | None = None
            ) -> list[list[float]]:
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

        vectors = await embed_in_batches(texts, model, max_inputs=3)

        # Assert: identical to the pre-task sequential batcher — 3 + 3 + 1
        # requests, vectors in global input order.
        assert [len(c) for c in model.calls] == [3, 3, 1]
        assert vectors == [[float(i)] for i in range(7)]


class TestSanitizationIsDelegatedToTheCleaningModule:
    """ONE definition: the sanitizer lives in ``tree.memory.rag.cleaning``."""

    def test_module_no_longer_defines_a_private_sanitizer(self) -> None:
        # A second copy of the regex is how train/serve drift starts.
        assert not hasattr(embedding_text, "_sanitize_for_embedding")
        assert not hasattr(embedding_text, "_INVALID_EMBED_CHARS_RE")

    def test_node_text_strips_control_chars_and_surrogates(self) -> None:
        # Arrange: one char from each stripped class — C0 (NUL, BEL, VT, FF),
        # DEL, C1 (0x80, 0x9f), and an unpaired surrogate (0xd800).
        node = {
            "type": "person",
            "name": "Bo\x00b",
            "properties": {"content": "x\x07\x0b\x0c\x1f\x7f\x80\x9f\ud800y"},
        }

        text = node_to_embedding_text(node)

        assert text == "person: Bob\nxy"

    def test_node_text_preserves_legitimate_unicode(self) -> None:
        # Smart quotes, emoji and accents are legitimate and must survive.
        node = {"type": "person", "name": "café “smart” \U0001f600 accenté"}

        assert node_to_embedding_text(node) == "person: café “smart” \U0001f600 accenté"
