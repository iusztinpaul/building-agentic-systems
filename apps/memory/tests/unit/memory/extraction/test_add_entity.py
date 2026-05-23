"""Unit tests for ``add_entity`` with a mocked Motor collection.

The integration suite verifies the actual aggregation semantics against
Atlas-local. These unit tests focus on the **dispatch contract**:

* ``_id`` derivation by ``dedup.action``.
* The 3x3 (strategy x action) matrix of expected ``update_one`` calls.
* SAME_AS edge writes happen iff ``action == "flagged"``.
* ``applied_strategy`` is stamped iff ``action == "merged"``.
* Short-circuit when ``resolve=False, deduplicate=False``.
* Input validation (empty name, out-of-range confidence).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.entities.knowledge_graph import NodeType
from tree.memory.embedding_text import node_to_embedding_text
from tree.memory.extraction.add_entity import add_entity
from tree.memory.extraction.dedup import (
    DeduplicationConfig,
    DeduplicationResult,
    MergeStrategy,
)
from tree.memory.resolution.types import ResolvedEntity


# A stable user_id used across the suite. Real ``PydanticObjectId`` so
# the prospective_id includes a realistic 24-hex-char prefix.
_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_PH = str(_USER_ID)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_database(mocker) -> tuple[Any, Any]:
    """Build a mocked database whose ``[name]`` returns a mocked collection.

    The collection has ``update_one`` and ``aggregate`` as ``AsyncMock``s.
    """

    collection = MagicMock(name="kg_collection")
    collection.update_one = AsyncMock(return_value=MagicMock())

    async def _empty_async_iter():
        for _ in []:
            yield _

    collection.aggregate = AsyncMock(return_value=_empty_async_iter())

    database = MagicMock(name="database")
    database.__getitem__.return_value = collection
    return database, collection


def _make_resolver(canonical_name: str = "alice", match_type: str = "exact") -> Any:
    """Build a resolver mock whose ``resolve`` returns a canned ResolvedEntity."""

    resolver = MagicMock(name="resolver")
    resolver.resolve = AsyncMock(
        return_value=ResolvedEntity(
            original_name="raw",
            canonical_name=canonical_name,
            entity_type=NodeType.PERSON,
            confidence=0.9,
            match_type=match_type,  # type: ignore[arg-type]
        )
    )
    return resolver


def _make_embedding_model() -> Any:
    """Embedding model that always returns one 8-dim zero vector."""
    model = MagicMock(name="embedding_model")
    model.embed = AsyncMock(return_value=[[0.0] * 8])
    return model


def _patch_dedupe_entity(mocker, dedup_result: DeduplicationResult) -> Any:
    """Patch ``tree.memory.extraction.add_entity.dedupe_entity`` to return ``dedup_result``."""
    return mocker.patch(
        "tree.memory.extraction.add_entity.dedupe_entity",
        new=AsyncMock(return_value=dedup_result),
    )


def _count_same_as_calls(collection: Any) -> int:
    """Count ``update_one`` calls whose target ``_id`` matches a SAME_AS edge pattern."""

    count = 0
    for call in collection.update_one.call_args_list:
        args, kwargs = call
        filter_arg = args[0] if args else kwargs.get("filter")
        candidate_id = filter_arg.get("_id") if isinstance(filter_arg, dict) else None
        if isinstance(candidate_id, str) and "|same_as|" in candidate_id:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestAddEntityInputValidation:
    async def test_empty_name_rejected(self, mocker) -> None:
        database, _ = _make_database(mocker)
        with pytest.raises(ValueError, match="non-empty"):
            await add_entity(
                database=database,
                embedding_model=_make_embedding_model(),
                resolver=_make_resolver(),
                user_id=_USER_ID,
                name="   ",
                entity_type=NodeType.PERSON,
                properties={},
                source_id="src1",
                dedup_config=DeduplicationConfig(),
            )

    async def test_confidence_above_one_rejected(self, mocker) -> None:
        database, _ = _make_database(mocker)
        with pytest.raises(ValueError, match="confidence"):
            await add_entity(
                database=database,
                embedding_model=_make_embedding_model(),
                resolver=_make_resolver(),
                user_id=_USER_ID,
                name="alice",
                entity_type=NodeType.PERSON,
                properties={"confidence": 1.5},
                source_id="src1",
                dedup_config=DeduplicationConfig(),
            )

    async def test_confidence_below_zero_rejected(self, mocker) -> None:
        database, _ = _make_database(mocker)
        with pytest.raises(ValueError, match="confidence"):
            await add_entity(
                database=database,
                embedding_model=_make_embedding_model(),
                resolver=_make_resolver(),
                user_id=_USER_ID,
                name="alice",
                entity_type=NodeType.PERSON,
                properties={"confidence": -0.1},
                source_id="src1",
                dedup_config=DeduplicationConfig(),
            )

    async def test_missing_user_id_raises_type_error(self, mocker) -> None:
        """``add_entity`` refuses to run without ``user_id``."""

        database, _ = _make_database(mocker)
        with pytest.raises(TypeError, match="user_id"):
            await add_entity(  # type: ignore[call-arg]
                database=database,
                embedding_model=_make_embedding_model(),
                resolver=_make_resolver(),
                name="alice",
                entity_type=NodeType.PERSON,
                properties={},
                source_id="src1",
                dedup_config=DeduplicationConfig(),
            )


# ---------------------------------------------------------------------------
# Short-circuit
# ---------------------------------------------------------------------------


class TestAddEntityShortCircuit:
    async def test_resolve_false_dedup_false_single_upsert(self, mocker) -> None:
        # Arrange
        database, collection = _make_database(mocker)
        resolver = _make_resolver()
        embedding_model = _make_embedding_model()
        dedupe_spy = _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))

        # Act
        target_id, resolved, dedup_result = await add_entity(
            database=database,
            embedding_model=embedding_model,
            resolver=resolver,
            user_id=_USER_ID,
            name="Alice",
            entity_type=NodeType.PERSON,
            properties={"email": "a@x.com"},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
            resolve=False,
            deduplicate=False,
        )

        # Assert — single upsert at the canonical _id, prefixed with user_id.
        assert target_id == f"{_PH}:person:alice"
        assert resolved.match_type == "none"
        assert dedup_result.action == "none"
        assert collection.update_one.call_count == 1
        assert _count_same_as_calls(collection) == 0
        # The upserted document carries the bound user_id.
        node_call = collection.update_one.call_args_list[0]
        set_stage = node_call.args[1][0]["$set"]
        assert set_stage["user_id"] == _USER_ID
        # Resolver and dedup must not have been called.
        resolver.resolve.assert_not_called()
        dedupe_spy.assert_not_called()
        embedding_model.embed.assert_not_called()


# ---------------------------------------------------------------------------
# 3x3 strategy x action matrix
# ---------------------------------------------------------------------------


_ALL_STRATEGIES = [
    MergeStrategy.KEEP_PRIMARY,
    MergeStrategy.MERGE_PROPERTIES,
    MergeStrategy.KEEP_ALIASES,
]


class TestAddEntityMergedAction:
    """``action == "merged"`` → one update on the canonical, no SAME_AS edge.

    ``applied_strategy`` is set on the returned ``DeduplicationResult``.
    """

    @pytest.mark.parametrize("strategy", _ALL_STRATEGIES)
    async def test_merged_emits_single_update_at_canonical(
        self, mocker, strategy: MergeStrategy
    ) -> None:
        # Arrange
        database, collection = _make_database(mocker)
        dedup_result = DeduplicationResult(
            action="merged",
            matched_node_id="person:alice_canonical",
            matched_node_name="alice canonical",
            similarity_score=0.97,
            match_type="embedding",
        )
        _patch_dedupe_entity(mocker, dedup_result)
        config = DeduplicationConfig(merge_strategy=strategy)

        # Act
        target_id, _resolved, returned = await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=_USER_ID,
            name="alice corp",
            entity_type=NodeType.PERSON,
            properties={"description": "a longer description"},
            source_id="src1",
            dedup_config=config,
        )

        # Assert
        assert target_id == "person:alice_canonical"
        assert returned.applied_strategy is strategy
        # Exactly one update_one (the merge), and it targets the canonical.
        assert collection.update_one.call_count == 1
        merge_args = collection.update_one.call_args_list[0]
        assert merge_args.args[0] == {"_id": "person:alice_canonical"}
        # No SAME_AS edge written.
        assert _count_same_as_calls(collection) == 0


class TestAddEntityFlaggedAction:
    """``action == "flagged"`` → upsert the new node + one SAME_AS edge."""

    @pytest.mark.parametrize("strategy", _ALL_STRATEGIES)
    async def test_flagged_emits_node_and_same_as_edge(
        self, mocker, strategy: MergeStrategy
    ) -> None:
        # Arrange
        database, collection = _make_database(mocker)
        dedup_result = DeduplicationResult(
            action="flagged",
            matched_node_id="person:alice_smith",
            matched_node_name="alice smith",
            similarity_score=0.88,
            match_type="embedding",
        )
        _patch_dedupe_entity(mocker, dedup_result)
        config = DeduplicationConfig(merge_strategy=strategy)

        # Act
        target_id, _resolved, returned = await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=_USER_ID,
            name="alyce smyth",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=config,
        )

        # Assert
        assert target_id == f"{_PH}:person:alyce smyth"
        assert returned.applied_strategy is None  # only set on merged
        # Two upserts: the new node + the SAME_AS edge.
        assert collection.update_one.call_count == 2
        # First call upserts at the new node id.
        node_args = collection.update_one.call_args_list[0]
        assert node_args.args[0] == {"_id": f"{_PH}:person:alyce smyth"}
        # Second call upserts the SAME_AS edge.
        edge_args = collection.update_one.call_args_list[1]
        assert (
            edge_args.args[0]["_id"]
            == f"{_PH}:person:alyce smyth|same_as|person:alice_smith"
        )
        # Edge payload carries pending status + confidence + match_type and user_id.
        edge_update = edge_args.args[1]
        assert edge_update["$setOnInsert"]["properties.status"] == "pending"
        assert edge_update["$setOnInsert"]["user_id"] == _USER_ID
        assert edge_update["$set"]["properties.confidence"] == 0.88
        assert edge_update["$set"]["properties.match_type"] == "embedding"
        assert edge_args.kwargs.get("upsert") is True
        # Exactly one SAME_AS edge.
        assert _count_same_as_calls(collection) == 1


class TestAddEntityNoneAction:
    """``action == "none"`` → upsert the new node, no SAME_AS edge."""

    @pytest.mark.parametrize("strategy", _ALL_STRATEGIES)
    async def test_none_emits_node_only(self, mocker, strategy: MergeStrategy) -> None:
        # Arrange
        database, collection = _make_database(mocker)
        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))
        config = DeduplicationConfig(merge_strategy=strategy)

        # Act
        target_id, _resolved, returned = await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(canonical_name="apple inc"),
            user_id=_USER_ID,
            name="apple",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=config,
        )

        # Assert
        assert target_id == f"{_PH}:person:apple"
        assert returned.applied_strategy is None
        # Exactly one update_one (the new node), no SAME_AS edge.
        assert collection.update_one.call_count == 1
        node_args = collection.update_one.call_args_list[0]
        assert node_args.args[0] == {"_id": f"{_PH}:person:apple"}
        assert _count_same_as_calls(collection) == 0


# ---------------------------------------------------------------------------
# canonical_name written on the new node
# ---------------------------------------------------------------------------


class TestAddEntityCanonicalNameWritten:
    async def test_canonical_name_from_resolver_on_none(self, mocker) -> None:
        """On ``action="none"``, ``canonical_name`` comes from the resolver."""

        # Arrange
        database, collection = _make_database(mocker)
        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))

        # Act
        await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(canonical_name="apple inc", match_type="exact"),
            user_id=_USER_ID,
            name="apple",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert — the $set stage carries canonical_name="apple inc".
        node_call = collection.update_one.call_args_list[0]
        pipeline = node_call.args[1]
        set_stage = pipeline[0]["$set"]
        assert set_stage["canonical_name"] == "apple inc"
        # user_id is stamped on the upsert.
        assert set_stage["user_id"] == _USER_ID


# ---------------------------------------------------------------------------
# Self-match exclusion
# ---------------------------------------------------------------------------


class TestAddEntitySelfMatchExclusion:
    """When dedup returns a self-match (top candidate == prospective_id),
    ``add_entity`` must NOT merge into itself.
    """

    async def test_self_match_is_filtered(self, mocker) -> None:
        # Arrange — dedupe says "merged" with matched_node_id == prospective_id.
        database, collection = _make_database(mocker)
        dedup_result = DeduplicationResult(
            action="merged",
            matched_node_id=f"{_PH}:person:alice",
            matched_node_name="alice",
            similarity_score=1.0,
            match_type="embedding",
        )
        _patch_dedupe_entity(mocker, dedup_result)

        # Act — prospective_id is also the user_id-prefixed person:alice.
        target_id, _resolved, returned = await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=_USER_ID,
            name="Alice",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert — fall back to the new-node path (action="none").
        assert target_id == f"{_PH}:person:alice"
        assert returned.action == "none"
        # No merge into the same node, no SAME_AS edge.
        assert collection.update_one.call_count == 1
        assert _count_same_as_calls(collection) == 0


# ---------------------------------------------------------------------------
# disabled-config short-circuit
# ---------------------------------------------------------------------------


class TestAddEntityDedupDisabled:
    async def test_dedup_config_disabled_skips_dedupe_entity(self, mocker) -> None:
        # Arrange
        database, collection = _make_database(mocker)
        dedupe_spy = _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))
        config = DeduplicationConfig(enabled=False)

        # Act
        target_id, _resolved, dedup_result = await add_entity(
            database=database,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=_USER_ID,
            name="alice",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=config,
        )

        # Assert — dedupe_entity not called; behaves like action="none".
        assert target_id == f"{_PH}:person:alice"
        assert dedup_result.action == "none"
        assert collection.update_one.call_count == 1
        dedupe_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Two-user isolation
# ---------------------------------------------------------------------------


class TestAddEntityTwoUserIsolation:
    """Two users writing the same name produce two distinct rows."""

    async def test_two_users_distinct_ids(self, mocker) -> None:
        user_a = PydanticObjectId("507f1f77bcf86cd799439011")
        user_b = PydanticObjectId("507f1f77bcf86cd799439022")

        database_a, collection_a = _make_database(mocker)
        database_b, collection_b = _make_database(mocker)

        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))

        target_a, _, _ = await add_entity(
            database=database_a,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=user_a,
            name="alice",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )
        target_b, _, _ = await add_entity(
            database=database_b,
            embedding_model=_make_embedding_model(),
            resolver=_make_resolver(),
            user_id=user_b,
            name="alice",
            entity_type=NodeType.PERSON,
            properties={},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        assert target_a != target_b
        assert target_a.startswith(f"{user_a}:")
        assert target_b.startswith(f"{user_b}:")


# ---------------------------------------------------------------------------
# #042 — node-text dedup embedding + reuse on the new node
# ---------------------------------------------------------------------------


class _RecordingEmbeddingModel:
    """Embedding model that records every text it is asked to embed.

    Returns a deterministic per-text vector (8-dim, seeded by ``hash``) so
    the test can assert (a) WHICH text was embedded and (b) that the
    persisted vector equals the dedup-query vector (same embed call).
    """

    def __init__(self) -> None:
        self.embedded_texts: list[str] = []

    @property
    def dimensions(self) -> int:
        return 8

    @staticmethod
    def _vec(text: str) -> list[float]:
        h = abs(hash(text))
        return [((h >> (i * 4)) & 0xF) / 15.0 for i in range(8)]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded_texts.extend(texts)
        return [self._vec(t) for t in texts]


class TestAddEntityNodeTextEmbedding:
    """``add_entity`` embeds the prospective node's NODE-TEXT (generic types)
    for the dedup query and reuses that vector as the persisted embedding.
    """

    async def test_dedup_query_vector_is_node_text_not_name(self, mocker) -> None:
        # Arrange — capture the embedding passed to dedupe_entity.
        database, _collection = _make_database(mocker)
        model = _RecordingEmbeddingModel()
        dedupe = mocker.patch(
            "tree.memory.extraction.add_entity.dedupe_entity",
            new=AsyncMock(return_value=DeduplicationResult(action="none")),
        )

        properties = {"role": "researcher", "org": "OpenAI"}
        # Act
        await add_entity(
            database=database,
            embedding_model=model,
            resolver=_make_resolver(canonical_name="Andrej Karpathy"),
            user_id=_USER_ID,
            name="Andrej Karpathy",
            entity_type=NodeType.PERSON,
            properties=properties,
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert — the text embedded is the node-text, NOT the bare name.
        expected_text = node_to_embedding_text(
            {
                "type": "person",
                "name": "Andrej Karpathy",
                "canonical_name": "Andrej Karpathy",
                "properties": properties,
            }
        )
        assert model.embedded_texts == [expected_text]
        assert model.embedded_texts[0] != "Andrej Karpathy"
        # And dedupe_entity received the node-text vector.
        passed_vec = dedupe.await_args.kwargs["embedding"]
        assert passed_vec == _RecordingEmbeddingModel._vec(expected_text)

    async def test_new_node_persists_same_vector_no_second_embed(self, mocker) -> None:
        # Arrange — action="none" → a new node is created.
        database, collection = _make_database(mocker)
        model = _RecordingEmbeddingModel()
        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))

        properties = {"role": "researcher"}
        # Act
        await add_entity(
            database=database,
            embedding_model=model,
            resolver=_make_resolver(canonical_name="Andrej Karpathy"),
            user_id=_USER_ID,
            name="Andrej Karpathy",
            entity_type=NodeType.PERSON,
            properties=properties,
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert — exactly ONE embed call for this node (no recompute).
        assert len(model.embedded_texts) == 1
        node_text = model.embedded_texts[0]
        expected_vec = _RecordingEmbeddingModel._vec(node_text)
        # The persisted embedding equals the dedup-query vector.
        node_call = collection.update_one.call_args_list[0]
        set_stage = node_call.args[1][0]["$set"]
        # ``embedding`` is written via ``$ifNull`` so unwrap the literal.
        persisted = set_stage["embedding"]["$ifNull"][1]
        assert persisted == expected_vec

    async def test_preference_embeds_statement_not_node_text(self, mocker) -> None:
        # Arrange
        database, _collection = _make_database(mocker)
        model = _RecordingEmbeddingModel()
        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))

        statement = "prefers dark mode in the editor"
        await add_entity(
            database=database,
            embedding_model=model,
            resolver=_make_resolver(canonical_name="prefers-dark-mode"),
            user_id=_USER_ID,
            name="prefers-dark-mode",
            entity_type=NodeType.PREFERENCE,
            properties={"statement": statement},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert — the statement text is embedded, not the node-text/slug.
        assert model.embedded_texts == [statement]

    async def test_fact_embeds_object_not_node_text(self, mocker) -> None:
        database, _collection = _make_database(mocker)
        model = _RecordingEmbeddingModel()
        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))

        obj = "the capital of France is Paris"
        await add_entity(
            database=database,
            embedding_model=model,
            resolver=_make_resolver(canonical_name="france-capital"),
            user_id=_USER_ID,
            name="france-capital",
            entity_type=NodeType.FACT,
            properties={"object": obj},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        assert model.embedded_texts == [obj]


# ---------------------------------------------------------------------------
# #055 — dedup embed routed through the single guarded chokepoint
# ---------------------------------------------------------------------------


class TestAddEntityRoutesThroughChokepoint:
    """ADR-002 §1 (amended): the dedup embed goes through ``_embed_chunk_resilient``.

    ``add_entity`` must NOT call ``embedding_model.embed(...)`` directly — it
    routes through ``_embed_chunk_resilient`` for the Voyage-400 bisect-and-skip
    resilience. The shared ``voyage-embeddings`` rate limit lives at the real
    network POST inside the Voyage clients, NOT in this path. Routing must
    preserve the previous degrade semantics.
    """

    async def test_routes_through_embed_chunk_resilient_not_direct_embed(
        self, mocker
    ) -> None:
        # Arrange — spy the chokepoint; keep dedup on so the embed path runs.
        database, _collection = _make_database(mocker)
        model = _make_embedding_model()
        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))
        chokepoint = mocker.patch(
            "tree.memory.extraction.add_entity._embed_chunk_resilient",
            new=AsyncMock(return_value=[[0.0] * 8]),
        )

        # Act
        await add_entity(
            database=database,
            embedding_model=model,
            resolver=_make_resolver(canonical_name="Andrej Karpathy"),
            user_id=_USER_ID,
            name="Andrej Karpathy",
            entity_type=NodeType.PERSON,
            properties={"role": "researcher"},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert — embed went through the chokepoint, not a direct .embed call.
        chokepoint.assert_awaited_once()
        passed_model, passed_texts = chokepoint.await_args.args
        assert passed_model is model
        assert isinstance(passed_texts, list) and len(passed_texts) == 1
        model.embed.assert_not_called()

    async def test_voyage_400_placeholder_degrades_to_empty_embedding(
        self, mocker
    ) -> None:
        # Arrange — a poison input: the chokepoint bisects to a singleton and
        # returns the aligned empty placeholder ``[[]]`` (Voyage 400 skip). The
        # previous inline behavior degraded such a result to ``embedding = []``;
        # that must be preserved now that the call routes through the chokepoint.
        database, _collection = _make_database(mocker)
        model = _make_embedding_model()
        dedupe = _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))
        mocker.patch(
            "tree.memory.extraction.add_entity._embed_chunk_resilient",
            new=AsyncMock(return_value=[[]]),
        )

        # Act
        await add_entity(
            database=database,
            embedding_model=model,
            resolver=_make_resolver(canonical_name="poison-entity"),
            user_id=_USER_ID,
            name="poison-entity",
            entity_type=NodeType.PERSON,
            properties={"role": "researcher"},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert — the empty placeholder degraded to embedding = [] (unchanged
        # behavior), and dedupe still ran with that empty embedding.
        assert dedupe.await_args.kwargs["embedding"] == []

    async def test_empty_chokepoint_result_also_degrades_to_empty(self, mocker) -> None:
        # Arrange — defensive: a falsy result (``[]``) must also degrade to
        # ``embedding = []`` exactly as ``embedded[0] if embedded else []`` did.
        database, _collection = _make_database(mocker)
        model = _make_embedding_model()
        dedupe = _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))
        mocker.patch(
            "tree.memory.extraction.add_entity._embed_chunk_resilient",
            new=AsyncMock(return_value=[]),
        )

        # Act
        await add_entity(
            database=database,
            embedding_model=model,
            resolver=_make_resolver(canonical_name="empty-entity"),
            user_id=_USER_ID,
            name="empty-entity",
            entity_type=NodeType.PERSON,
            properties={"role": "researcher"},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert
        assert dedupe.await_args.kwargs["embedding"] == []


# ---------------------------------------------------------------------------
# #055 — HEADLINE regression: a cache-hit dedup acquires NO rate-limit slot
# (the timeout the amendment fixes), a cache-miss DOES.
# ---------------------------------------------------------------------------


def _make_mock_voyage_session():
    """Build an aiohttp session double that 200s with a 1-dim vector per input."""

    def _post(_url, *, json, headers):  # noqa: A002 - aiohttp kwarg name
        n = len(json.get("input") or json.get("inputs") or [])
        resp = AsyncMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={"data": [{"embedding": [0.7]}] * n})
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    sess = AsyncMock()
    sess.post = MagicMock(side_effect=_post)
    sess.__aenter__ = AsyncMock(return_value=sess)
    sess.__aexit__ = AsyncMock(return_value=False)
    return sess


class TestCachedDedupAcquiresNoRateLimitSlot:
    """ADR-002 §1 amendment: the extraction hot path injects a
    ``_CachedSingleEmbedding`` into ``add_entity`` so the per-entity dedup embed
    reuses a pre-computed vector with ZERO network I/O.

    A cache HIT must therefore acquire NO ``voyage-embeddings`` slot — gating it
    serialized ~40 zero-POST lookups behind the 3-RPM throttle and timed out
    extraction. A cache MISS (a real Voyage client) DOES acquire a slot. The
    ``rate_limit`` symbol lives only in the Voyage client modules now, so we spy
    on it there and drive the full ``add_entity`` dedup path with each model.
    """

    async def test_cache_hit_via_cached_single_embedding_acquires_no_slot(
        self, mocker
    ) -> None:
        # Arrange — the real cache shim the pipeline injects on a cache hit.
        from tree.memory.extraction.pipeline import _CachedSingleEmbedding

        text_rate_limit = mocker.patch(
            "tree.models.voyage_embedding.rate_limit", new_callable=AsyncMock
        )
        mm_rate_limit = mocker.patch(
            "tree.models.voyage_multimodal_embedding.rate_limit",
            new_callable=AsyncMock,
        )
        database, _collection = _make_database(mocker)
        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))
        cached_model = _CachedSingleEmbedding([0.42] * 8)

        # Act — drive the dedup embed path with the cached (no-network) model.
        await add_entity(
            database=database,
            embedding_model=cached_model,
            resolver=_make_resolver(canonical_name="Cached Entity"),
            user_id=_USER_ID,
            name="Cached Entity",
            entity_type=NodeType.PERSON,
            properties={"role": "researcher"},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert — the cached vector never reached a Voyage client, so NO slot
        # was acquired in either client (the timeout regression is fixed).
        text_rate_limit.assert_not_awaited()
        mm_rate_limit.assert_not_awaited()

    async def test_cache_miss_via_real_voyage_client_acquires_a_slot(
        self, mocker
    ) -> None:
        # Arrange — a real Voyage text client (cache miss): the dedup embed must
        # reach the network POST and acquire exactly one slot.
        from tree.models.voyage_embedding import VoyageTextEmbeddingModel

        text_rate_limit = mocker.patch(
            "tree.models.voyage_embedding.rate_limit", new_callable=AsyncMock
        )
        database, _collection = _make_database(mocker)
        _patch_dedupe_entity(mocker, DeduplicationResult(action="none"))
        real_model = VoyageTextEmbeddingModel(api_key="key", model="voyage-3.5")
        sess = _make_mock_voyage_session()
        mocker.patch("aiohttp.ClientSession", return_value=sess)

        # Act
        await add_entity(
            database=database,
            embedding_model=real_model,
            resolver=_make_resolver(canonical_name="Fresh Entity"),
            user_id=_USER_ID,
            name="Fresh Entity",
            entity_type=NodeType.PERSON,
            properties={"role": "researcher"},
            source_id="src1",
            dedup_config=DeduplicationConfig(),
        )

        # Assert — the real POST acquired exactly one slot, with documented args.
        text_rate_limit.assert_awaited_once_with(
            "voyage-embeddings", occupy=1, strict=False
        )
