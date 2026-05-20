"""Unit tests for the six-task extraction pipeline.

Each test exercises a single task body (``task.fn``) with mocked Mongo /
embedding / LLM dependencies. Behavior that requires Prefect's task runtime
(caching, retries, mapping) is verified in
``tests/integration/memory/test_extraction_pipeline.py`` against the live
flow.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.entities.knowledge_graph import NodeType
from tree.memory.extraction.dedup import DeduplicationConfig, DeduplicationResult
from tree.memory.embedding_text import node_to_embedding_text
from tree.memory.extraction.pipeline import (
    _CachedSingleEmbedding,
    _dedupe_entities,
    _dispatch_entity_write,
    _embed_entities,
    _entity_embeddable_text,
    _extract_chunks_and_structural,
    _llm_extract_entities,
    _resolve_entities,
    embed_entities_task,
    extract_chunks_and_structural_task,
    llm_extract_entities_task,
    memory_extraction,
    run_extraction_for_documents,
)
from tree.memory.resolution.composite import CompositeResolver
from tree.memory.resolution.types import ResolvedEntity
from tree.models.base import BaseEmbeddingModel
from tree.memory.types import (
    ChunkedDocument,
    DedupDecision,
    EmbeddingMap,
    ExtractedNode,
    ExtractionResult,
    RawExtraction,
    ResolutionOutput,
    WriteSummary,
    make_entity_key,
)


# A stable user_id used across the unit suite.
_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_PH = str(_USER_ID)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_document(
    *,
    doc_id: str = "507f1f77bcf86cd799439011",
    content: str = "Alice ships ML pipelines.",
    source_uri: str = "https://example.com/a",
) -> Any:
    """Build a minimal Document-like object that ``_extract_chunks_and_structural`` accepts."""

    doc = MagicMock(name="Document")
    doc.id = doc_id
    doc.source_uri = source_uri
    doc.source_type = MagicMock(value="huggingface")
    doc.date = None
    doc.content = content
    doc.references = []
    return doc


def _async_cursor(items: list[dict[str, Any]]) -> Any:
    """Build a mocked ``cursor`` that supports ``async for`` and ``.limit(...)``."""

    async def _aiter():
        for it in items:
            yield it

    cursor = MagicMock(name="cursor")
    cursor.__aiter__ = lambda self: _aiter()
    cursor.limit = MagicMock(return_value=cursor)
    return cursor


def _make_database_with_candidates(
    candidates_by_type: dict[NodeType, list[dict[str, Any]]],
) -> Any:
    """Build a database mock whose ``[col].find(...)`` returns the seeded candidates."""

    def _find(query: dict[str, Any], projection: dict[str, Any] | None = None):
        ntype = query.get("type")
        for t, docs in candidates_by_type.items():
            if t.value == ntype:
                return _async_cursor(docs)
        return _async_cursor([])

    collection = MagicMock(name="kg_collection")
    collection.find = MagicMock(side_effect=_find)
    collection.update_one = AsyncMock(return_value=MagicMock())

    database = MagicMock(name="database")
    database.__getitem__.return_value = collection
    return database


# ---------------------------------------------------------------------------
# Task ① — extract_chunks_and_structural
# ---------------------------------------------------------------------------


class TestExtractChunksAndStructuralTask:
    async def test_empty_content_returns_empty_chunks(self) -> None:
        doc = _make_document(content="")
        chunked = await _extract_chunks_and_structural(doc)
        assert chunked.chunk_texts == []
        assert chunked.chunk_ids == []
        assert chunked.structural.nodes == []
        assert chunked.structural.edges == []

    async def test_content_produces_chunks_and_structural_doc_node(self) -> None:
        doc = _make_document(content="some content " * 50)
        chunked = await _extract_chunks_and_structural(doc)
        assert len(chunked.chunk_texts) >= 1
        # Exactly one DOCUMENT node in the structural payload.
        doc_nodes = [n for n in chunked.structural.nodes if n.type == NodeType.DOCUMENT]
        assert len(doc_nodes) == 1
        assert doc_nodes[0].name == doc.source_uri

    async def test_task_decorator_is_registered(self) -> None:
        # Identity check on the registered task name.
        assert (
            extract_chunks_and_structural_task.name == "extract-chunks-and-structural"
        )


# ---------------------------------------------------------------------------
# Task ② — llm_extract_entities
# ---------------------------------------------------------------------------


class TestLlmExtractEntitiesTask:
    async def test_no_chunks_returns_empty(self) -> None:
        chunked = ChunkedDocument(
            document_id="d1",
            source_uri="u1",
            source_type="huggingface",
            chunk_texts=[],
            chunk_ids=[],
        )
        raw = await _llm_extract_entities(chunked)
        assert raw.extracted.nodes == []
        assert raw.extracted.edges == []

    async def test_calls_llm_per_chunk(self, mocker) -> None:
        from tree.models.fake_model import FakeLLM

        fake = FakeLLM(
            responses=[
                {
                    "nodes": [{"name": "alice", "type": "person", "properties": {}}],
                    "edges": [],
                }
            ]
            * 3
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.get_llm",
            return_value=fake,
        )
        chunked = ChunkedDocument(
            document_id="d1",
            source_uri="u1",
            source_type="huggingface",
            chunk_texts=["c1", "c2", "c3"],
            chunk_ids=["cid-0", "cid-1", "cid-2"],
        )

        raw = await _llm_extract_entities(chunked)

        assert fake.call_count == 3
        assert len(raw.extracted.nodes) == 3

    async def test_task_decorator_is_registered(self) -> None:
        assert llm_extract_entities_task.name == "llm-extract-entities"


# ---------------------------------------------------------------------------
# Task ③ — resolve_entities
# ---------------------------------------------------------------------------


class TestResolveEntitiesTask:
    async def test_empty_input_returns_empty_output(self, mocker) -> None:
        resolver = MagicMock(spec=CompositeResolver)
        database = _make_database_with_candidates({})
        out = await _resolve_entities([], database, resolver, _USER_ID)
        assert out.entities == []
        assert out.resolved_by_key == {}

    async def test_candidate_fetch_includes_user_id_filter(self, mocker) -> None:
        """The candidate cursor must be issued with ``user_id`` in the query."""

        resolver = MagicMock(spec=CompositeResolver)
        resolver.resolve_with_types = AsyncMock(return_value=[])

        candidates = [
            {"_id": "person:p0", "name": "p0", "canonical_name": None, "aliases": []}
        ]
        database = _make_database_with_candidates({NodeType.PERSON: candidates})

        raw = RawExtraction(
            document_id="d1",
            source_uri="u1",
            chunked=ChunkedDocument(
                document_id="d1", source_uri="u1", source_type="huggingface"
            ),
            extracted=ExtractionResult(
                nodes=[ExtractedNode(name="probe", type=NodeType.PERSON, properties={})]
            ),
        )

        await _resolve_entities([raw], database, resolver, _USER_ID)

        collection = database.__getitem__.return_value
        find_call = collection.find.call_args_list[0]
        find_filter = find_call.args[0]
        assert find_filter["user_id"] == _USER_ID

    async def test_candidate_fetch_uses_set_union_and_records_name_to_owner_id(
        self, mocker
    ) -> None:
        """Carry-forward: candidate names include both ``name`` and non-null
        ``canonical_name``, and ``name_to_owner_id`` maps each variant back to
        its owning ``_id``."""

        resolver = MagicMock(spec=CompositeResolver)
        resolver.resolve_with_types = AsyncMock(
            return_value=[
                ResolvedEntity(
                    original_name="john smith",
                    canonical_name="John Smith",
                    entity_type=NodeType.PERSON,
                    confidence=1.0,
                    match_type="exact",
                )
            ]
        )

        database = _make_database_with_candidates(
            {
                NodeType.PERSON: [
                    {
                        "_id": "person:jean smith",
                        "name": "Jean Smith",
                        "canonical_name": "John Smith",
                        "aliases": [],
                    }
                ]
            }
        )

        raw = RawExtraction(
            document_id="d1",
            source_uri="u1",
            chunked=ChunkedDocument(
                document_id="d1", source_uri="u1", source_type="huggingface"
            ),
            extracted=ExtractionResult(
                nodes=[
                    ExtractedNode(
                        name="john smith", type=NodeType.PERSON, properties={}
                    )
                ]
            ),
        )

        out = await _resolve_entities([raw], database, resolver, _USER_ID)

        # Resolver was called with a candidate list whose name-set INCLUDES
        # both "Jean Smith" and "John Smith".
        call = resolver.resolve_with_types.await_args
        existing = call.kwargs["existing_entities"]
        person_candidates = existing[NodeType.PERSON]
        assert "Jean Smith" in person_candidates
        assert "John Smith" in person_candidates

        # The reverse map ties both surface forms back to the existing _id.
        assert out.name_to_owner_id["person|Jean Smith"] == "person:jean smith"
        assert out.name_to_owner_id["person|John Smith"] == "person:jean smith"

    async def test_candidate_cap_emits_warning(self, caplog, monkeypatch) -> None:
        """When the candidate fetch returns >= cap docs, a WARNING is logged.

        The cap is applied MongoDB-side via ``.limit(...)``; the mock cursor
        returns whatever we hand it, so we seed exactly ``cap`` rows and
        assert the log line and the recorded count.
        """

        monkeypatch.setenv("TREE_EXTRACTION__RESOLUTION__MAX_CANDIDATES_PER_TYPE", "5")

        resolver = MagicMock(spec=CompositeResolver)
        resolver.resolve_with_types = AsyncMock(return_value=[])

        candidates = [
            {
                "_id": f"person:p{i}",
                "name": f"Person {i}",
                "canonical_name": None,
                "aliases": [],
            }
            for i in range(5)
        ]
        database = _make_database_with_candidates({NodeType.PERSON: candidates})

        raw = RawExtraction(
            document_id="d1",
            source_uri="u1",
            chunked=ChunkedDocument(
                document_id="d1", source_uri="u1", source_type="huggingface"
            ),
            extracted=ExtractionResult(
                nodes=[ExtractedNode(name="probe", type=NodeType.PERSON, properties={})]
            ),
        )

        import logging

        caplog.set_level(logging.WARNING)
        out = await _resolve_entities([raw], database, resolver, _USER_ID)
        assert out.candidates_seen_by_type["person"] == 5
        assert any(
            "PERSON candidate fetch hit cap (5)" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# Task ④ — embed_entities (batched, #044)
# ---------------------------------------------------------------------------


class TestEmbedEntitiesTask:
    async def test_embeds_all_texts_in_one_batched_call(self, mocker) -> None:
        # #044: task ④ embeds EVERY run node-text in a single batched call
        # (via embed_in_batches over the SEARCH model) and returns a
        # ``text -> vector`` map.
        model = MagicMock()
        model.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
        search_factory = mocker.patch(
            "tree.memory.extraction.pipeline.get_search_embedding_model",
            return_value=model,
        )

        texts = [
            "person: Andrej Karpathy\nrole: researcher",
            "person: Yann LeCun\nrole: researcher",
        ]
        result = await _embed_entities(texts)

        assert result == {
            texts[0]: [0.1, 0.2, 0.3],
            texts[1]: [0.4, 0.5, 0.6],
        }
        # ONE embed() call carrying BOTH node-texts (not one call per text).
        model.embed.assert_awaited_once_with(texts)
        search_factory.assert_called_once()

    async def test_empty_input_returns_empty_without_calling_model(
        self, mocker
    ) -> None:
        model = MagicMock()
        model.embed = AsyncMock()
        mocker.patch(
            "tree.memory.extraction.pipeline.get_search_embedding_model",
            return_value=model,
        )

        result = await _embed_entities([])

        assert result == {}
        model.embed.assert_not_awaited()

    async def test_task_decorator_uses_inputs_cache(self) -> None:
        # Cache policy is registered on the decorated task; identity check on
        # the registered name. The cache itself is exercised in the integration
        # suite (Prefect runtime required).
        assert embed_entities_task.name == "embed-entities"


# ---------------------------------------------------------------------------
# Task ⑤ — dedupe_entities
# ---------------------------------------------------------------------------


class TestDedupeEntitiesTask:
    async def test_disabled_config_short_circuits(self, mocker) -> None:
        cfg = DeduplicationConfig(enabled=False)
        resolved = ResolutionOutput(
            entities=[("alice", NodeType.PERSON)],
            resolved_by_key={
                "d1|person|alice": ResolvedEntity(
                    original_name="alice",
                    canonical_name="alice",
                    entity_type=NodeType.PERSON,
                    confidence=1.0,
                    match_type="exact",
                )
            },
        )
        embeddings = EmbeddingMap(vectors={"alice": [0.1] * 8})

        database = MagicMock()
        dedupe_spy = mocker.patch(
            "tree.memory.extraction.pipeline.dedupe_entity",
            new=AsyncMock(),
        )

        out = await _dedupe_entities(resolved, embeddings, database, cfg, _USER_ID)
        assert out.decisions["d1|person|alice"].action == "none"
        dedupe_spy.assert_not_called()

    async def test_self_match_is_dropped(self, mocker) -> None:
        cfg = DeduplicationConfig()
        # ``dedupe_entity`` returns a "merged" with the same id as prospective.
        mocker.patch(
            "tree.memory.extraction.pipeline.dedupe_entity",
            new=AsyncMock(
                return_value=DeduplicationResult(
                    action="merged",
                    matched_node_id=f"{_PH}:person:alice",
                    matched_node_name="alice",
                    similarity_score=1.0,
                    match_type="embedding",
                )
            ),
        )

        resolved = ResolutionOutput(
            entities=[("alice", NodeType.PERSON)],
            resolved_by_key={
                "d1|person|alice": ResolvedEntity(
                    original_name="alice",
                    canonical_name="alice",
                    entity_type=NodeType.PERSON,
                    confidence=1.0,
                    match_type="exact",
                )
            },
        )
        embeddings = EmbeddingMap(vectors={"alice": [0.1] * 8})
        out = await _dedupe_entities(resolved, embeddings, MagicMock(), cfg, _USER_ID)
        assert out.decisions["d1|person|alice"].action == "none"


# ---------------------------------------------------------------------------
# Config alignment validator
# ---------------------------------------------------------------------------


class TestConfigAlignmentValidator:
    """The cross-key validator must reject a misaligned config at flow entry."""

    async def test_type_strict_disagreement_raises_value_error(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("TREE_EXTRACTION__RESOLUTION__TYPE_STRICT", "true")
        monkeypatch.setenv("TREE_EXTRACTION__DEDUP__MATCH_SAME_TYPE_ONLY", "false")

        with pytest.raises(ValueError, match="type_strict.*match_same_type_only"):
            # Drive directly through the helper that the flow calls at entry.
            await run_extraction_for_documents(
                ["507f1f77bcf86cd799439011"],
                user_id=_USER_ID,
                client=MagicMock(),
                database_name="test",
            )

    async def test_dedup_threshold_inversion_raises_value_error(
        self, monkeypatch
    ) -> None:
        # auto_merge_threshold MUST be strictly greater than flag_threshold.
        monkeypatch.setenv("TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD", "0.5")
        monkeypatch.setenv("TREE_EXTRACTION__DEDUP__FLAG_THRESHOLD", "0.8")

        with pytest.raises(ValueError, match="auto_merge_threshold.*flag_threshold"):
            await run_extraction_for_documents(
                ["507f1f77bcf86cd799439011"],
                user_id=_USER_ID,
                client=MagicMock(),
                database_name="test",
            )


# ---------------------------------------------------------------------------
# _CachedSingleEmbedding — the inline cache wrapper used by task ⑥
# ---------------------------------------------------------------------------


class TestCachedSingleEmbedding:
    async def test_returns_seeded_vector_regardless_of_input(self) -> None:
        wrapper = _CachedSingleEmbedding([0.1, 0.2])
        # Two different inputs both return the same vector — used in apply_writes
        # to keep ``add_entity`` from re-paying for an embedding the flow already
        # has.
        out = await wrapper.embed(["any name"])
        assert out == [[0.1, 0.2]]
        out2 = await wrapper.embed(["another name"])
        assert out2 == [[0.1, 0.2]]


# ---------------------------------------------------------------------------
# Flow registration
# ---------------------------------------------------------------------------


class TestPipelineExports:
    """AC: ``pipeline.py`` exports exactly one flow and six tasks."""

    def test_six_tasks_exported(self) -> None:
        from tree.memory.extraction import pipeline

        expected = {
            "extract_chunks_and_structural_task",
            "llm_extract_entities_task",
            "resolve_entities_task",
            "embed_entities_task",
            "dedupe_entities_task",
            "apply_writes_task",
        }
        for name in expected:
            assert hasattr(pipeline, name), f"Missing task export: {name}"

    def test_flow_exported(self) -> None:
        from tree.memory.extraction import pipeline

        assert hasattr(pipeline, "memory_extraction")
        # External name unchanged (referenced by the orchestrator deployment).
        assert memory_extraction.name == "memory-extraction-etl"


# ---------------------------------------------------------------------------
# Required-user_id contract on the flow signature
# ---------------------------------------------------------------------------


class TestRequiredUserIdSignature:
    """The flow refuses to start without a ``user_id`` argument.

    The Prefect ``@flow`` decorator preserves the underlying function's
    signature; calling ``.fn(...)`` without ``user_id`` triggers Python's
    standard ``TypeError: missing 1 required positional argument``.
    """

    async def test_flow_missing_user_id_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="user_id"):
            await memory_extraction.fn(document_ids=["x"])  # type: ignore[call-arg]

    async def test_run_helper_missing_user_id_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="user_id"):
            await run_extraction_for_documents(  # type: ignore[call-arg]
                ["x"],
                client=MagicMock(),
                database_name="test",
            )


# ---------------------------------------------------------------------------
# #042 — node-text embeddable-text selection + reuse plumbing
# ---------------------------------------------------------------------------


class TestEntityEmbeddableText:
    """``_entity_embeddable_text`` mirrors ``add_entity._embeddable_text``."""

    def test_generic_type_returns_node_text(self) -> None:
        properties = {"role": "researcher"}
        text = _entity_embeddable_text(
            entity_type=NodeType.PERSON,
            name="Andrej Karpathy",
            canonical_name="Andrej Karpathy",
            properties=properties,
        )
        expected = node_to_embedding_text(
            {
                "type": "person",
                "name": "Andrej Karpathy",
                "canonical_name": "Andrej Karpathy",
                "properties": properties,
            }
        )
        assert text == expected
        assert text != "Andrej Karpathy"

    def test_generic_type_strips_aliases_and_confidence(self) -> None:
        # ``aliases`` / ``confidence`` are top-level columns on the stored
        # row, so they must not leak into the embeddable node-text.
        text = _entity_embeddable_text(
            entity_type=NodeType.PERSON,
            name="Andrej Karpathy",
            canonical_name="Andrej Karpathy",
            properties={"role": "researcher", "aliases": ["AK"], "confidence": 0.9},
        )
        assert "AK" not in text
        assert "confidence" not in text
        assert "researcher" in text

    def test_preference_returns_statement(self) -> None:
        text = _entity_embeddable_text(
            entity_type=NodeType.PREFERENCE,
            name="prefers-dark-mode",
            canonical_name="prefers-dark-mode",
            properties={"statement": "prefers dark mode"},
        )
        assert text == "prefers dark mode"

    def test_fact_returns_object(self) -> None:
        text = _entity_embeddable_text(
            entity_type=NodeType.FACT,
            name="france-capital",
            canonical_name="france-capital",
            properties={"object": "Paris"},
        )
        assert text == "Paris"

    def test_preference_without_statement_falls_back_to_node_text(self) -> None:
        # A malformed preference (no statement) is still embeddable.
        text = _entity_embeddable_text(
            entity_type=NodeType.PREFERENCE,
            name="prefers-dark-mode",
            canonical_name="prefers-dark-mode",
            properties={},
        )
        assert text.startswith("preference: prefers-dark-mode")


class TestDispatchEntityWriteReusesVector:
    """Task ⑥ reuses the task-④ vector via ``_CachedSingleEmbedding`` and
    NEVER re-embeds the node inside apply-writes (AC: no second embed call).
    """

    async def test_no_second_embed_call_reuses_node_text_vector(self, mocker) -> None:
        # Arrange — capture the embedding ``add_entity`` ends up using.
        captured: dict[str, Any] = {}

        async def _fake_add_entity(*args: Any, **kwargs: Any) -> Any:
            model = kwargs["embedding_model"]
            captured["vector"] = (await model.embed(["ignored"]))[0]
            captured["model"] = model
            return ("target-id", None, DeduplicationResult(action="none"))

        mocker.patch(
            "tree.memory.extraction.pipeline.add_entity",
            new=AsyncMock(side_effect=_fake_add_entity),
        )

        node = ExtractedNode(
            name="Andrej Karpathy",
            type=NodeType.PERSON,
            properties={"role": "researcher"},
        )
        resolved_entity = ResolvedEntity(
            original_name="Andrej Karpathy",
            canonical_name="Andrej Karpathy",
            entity_type=NodeType.PERSON,
            confidence=1.0,
            match_type="exact",
        )
        key = make_entity_key("d1", NodeType.PERSON, "Andrej Karpathy")
        node_text = _entity_embeddable_text(
            entity_type=NodeType.PERSON,
            name="Andrej Karpathy",
            canonical_name="Andrej Karpathy",
            properties={"role": "researcher"},
        )
        resolved = ResolutionOutput(
            resolved_by_key={key: resolved_entity},
            embeddable_text_by_key={key: node_text},
        )
        # The task-④ vector is keyed by the NODE-TEXT, not the name.
        embeddings = EmbeddingMap(vectors={node_text: [0.42] * 8})

        # A real model that would explode if actually called for a fresh embed.
        real_model = MagicMock()
        real_model.embed = AsyncMock(
            side_effect=AssertionError("apply-writes must not re-embed")
        )

        # Act
        await _dispatch_entity_write(
            database=MagicMock(),
            embedding_model=real_model,
            resolver=MagicMock(spec=CompositeResolver),
            user_id=_USER_ID,
            node=node,
            source_document_id="d1",
            resolved_entity=resolved_entity,
            decision=DedupDecision(action="none"),
            embeddings=embeddings,
            resolved=resolved,
            dedup_config=DeduplicationConfig(),
            summary=WriteSummary(),
        )

        # Assert — the vector handed to add_entity is the cached node-text
        # vector, and the real model's embed was never invoked.
        assert captured["vector"] == [0.42] * 8
        assert isinstance(captured["model"], _CachedSingleEmbedding)
        real_model.embed.assert_not_called()


# ---------------------------------------------------------------------------
# #043 — resolution name-embedding uses the RESOLUTION model (transient),
# dedup / writes / supersession use the SEARCH model (persisted).
# ---------------------------------------------------------------------------


class TestResolverUsesResolutionModel:
    """``_build_resolver`` wires its model into the semantic stage.

    The model passed to ``_build_resolver`` is the one the resolver's
    transient name-embedding semantic stage uses — proven by reaching into
    the constructed ``CompositeResolver``'s ``SemanticMatchResolver``.
    """

    def test_build_resolver_threads_model_into_semantic_stage(self) -> None:
        from tree.memory.extraction.pipeline import _build_resolver

        sentinel_model = MagicMock(spec=BaseEmbeddingModel)

        resolver = _build_resolver(sentinel_model)

        assert isinstance(resolver, CompositeResolver)
        # The semantic stage is the only resolver leg that holds an
        # embedding model; it must be the model we passed in.
        assert resolver._semantic is not None
        assert resolver._semantic._embedding_model is sentinel_model


class TestFlowEmbeddingModelSplit:
    """Both flow entry points hold two distinct embedding handles.

    Resolver ← ``get_resolution_embedding_model()`` (transient name vector).
    Dedup / writes / supersession ← ``get_search_embedding_model()``
    (persisted node vector). The two MUST be distinct objects so the
    operator can later swap a lighter resolution model without touching the
    persisted-vector space.
    """

    async def test_memory_extraction_builds_resolver_from_resolution_model(
        self, mocker
    ) -> None:
        # Distinct sentinels so we can prove which handle reached the resolver.
        resolution_model = MagicMock(spec=BaseEmbeddingModel, name="resolution_model")
        search_model = MagicMock(spec=BaseEmbeddingModel, name="search_model")
        res_factory = mocker.patch(
            "tree.memory.extraction.pipeline.get_resolution_embedding_model",
            return_value=resolution_model,
        )
        search_factory = mocker.patch(
            "tree.memory.extraction.pipeline.get_search_embedding_model",
            return_value=search_model,
        )
        build_resolver = mocker.patch(
            "tree.memory.extraction.pipeline._build_resolver",
            return_value=MagicMock(spec=CompositeResolver),
        )

        # No-docs early-exit — the resolver/model handles are constructed
        # BEFORE the document fetch, so we never need to mock the LLM/embed
        # stages here.
        mock_client = MagicMock()
        mock_client.__getitem__.return_value = MagicMock()
        mocker.patch(
            "tree.memory.extraction.pipeline.init_mongodb",
            new=AsyncMock(return_value=mock_client),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.Document.find",
            return_value=MagicMock(to_list=AsyncMock(return_value=[])),
        )

        await memory_extraction.fn(user_id=_USER_ID)

        # The resolver is built from the RESOLUTION model, never the search model.
        res_factory.assert_called_once()
        build_resolver.assert_called_once_with(resolution_model)
        # The search model factory is still wired up for dedup/writes/supersession.
        assert search_factory.call_count >= 1
        assert resolution_model is not search_model

    async def test_memory_extraction_threads_search_model_into_supersession_and_writes(
        self, mocker
    ) -> None:
        resolution_model = MagicMock(spec=BaseEmbeddingModel, name="resolution_model")
        search_model = MagicMock(spec=BaseEmbeddingModel, name="search_model")
        mocker.patch(
            "tree.memory.extraction.pipeline.get_resolution_embedding_model",
            return_value=resolution_model,
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.get_search_embedding_model",
            return_value=search_model,
        )
        resolver = MagicMock(spec=CompositeResolver)
        mocker.patch(
            "tree.memory.extraction.pipeline._build_resolver",
            return_value=resolver,
        )

        # DB plumbing — one document so the flow runs past the early-exit and
        # reaches supersession + apply-writes.
        mock_client = MagicMock()
        mock_client.__getitem__.return_value = MagicMock()
        mocker.patch(
            "tree.memory.extraction.pipeline.init_mongodb",
            new=AsyncMock(return_value=mock_client),
        )
        doc = _make_document()
        mocker.patch(
            "tree.memory.extraction.pipeline.Document.find",
            return_value=MagicMock(to_list=AsyncMock(return_value=[doc])),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.User.get",
            new=AsyncMock(return_value=MagicMock()),
        )

        # No-op the heavy stages; we only care about the model handle wiring.
        chunked = ChunkedDocument(
            document_id="507f1f77bcf86cd799439011",
            source_uri="https://example.com/a",
            source_type="huggingface",
            date=None,
            reference_uris=[],
            chunk_texts=[],
            chunk_ids=[],
            structural=ExtractionResult(),
        )
        raw = RawExtraction(
            document_id="507f1f77bcf86cd799439011",
            source_uri="https://example.com/a",
            chunked=chunked,
            extracted=ExtractionResult(),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.extract_chunks_and_structural_task",
            new=AsyncMock(return_value=chunked),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.llm_extract_entities_task",
            new=AsyncMock(return_value=raw),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.validate_raws_task",
            new=AsyncMock(return_value=[raw]),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.redirect_first_person",
            return_value=[],
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.canonicalize_preference_names",
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.get_llm",
            return_value=MagicMock(),
        )
        supersession = mocker.patch(
            "tree.memory.extraction.pipeline.resolve_supersessions",
            new=AsyncMock(),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.write_self_has_preference_edges",
            new=AsyncMock(),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.resolve_entities_task",
            new=AsyncMock(return_value=ResolutionOutput()),
        )
        apply_writes = mocker.patch(
            "tree.memory.extraction.pipeline.apply_writes_task",
            new=AsyncMock(return_value=WriteSummary()),
        )

        await memory_extraction.fn(
            user_id=_USER_ID, document_ids=["507f1f77bcf86cd799439011"]
        )

        # Supersession compares against PERSISTED preference vectors → SEARCH model.
        assert supersession.await_args.kwargs["embedding_model"] is search_model
        # apply-writes persists node vectors → SEARCH model (positional arg #8).
        assert search_model in apply_writes.await_args.args
        assert resolution_model not in apply_writes.await_args.args

    async def test_run_extraction_helper_builds_resolver_from_resolution_model(
        self, mocker
    ) -> None:
        resolution_model = MagicMock(spec=BaseEmbeddingModel, name="resolution_model")
        search_model = MagicMock(spec=BaseEmbeddingModel, name="search_model")
        res_factory = mocker.patch(
            "tree.memory.extraction.pipeline.get_resolution_embedding_model",
            return_value=resolution_model,
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.get_search_embedding_model",
            return_value=search_model,
        )
        build_resolver = mocker.patch(
            "tree.memory.extraction.pipeline._build_resolver",
            return_value=MagicMock(spec=CompositeResolver),
        )
        # No-docs early-exit.
        mocker.patch(
            "tree.memory.extraction.pipeline.Document.find",
            return_value=MagicMock(to_list=AsyncMock(return_value=[])),
        )
        client = MagicMock()
        client.__getitem__.return_value = MagicMock()

        await run_extraction_for_documents(
            ["507f1f77bcf86cd799439011"],
            user_id=_USER_ID,
            client=client,
            database_name="test",
        )

        res_factory.assert_called_once()
        build_resolver.assert_called_once_with(resolution_model)

    async def test_run_extraction_helper_uses_injected_search_model_for_writes(
        self, mocker
    ) -> None:
        # When the MCP path injects an ``embedding_model`` it must drive the
        # SEARCH/persisted path (supersession + writes), NOT the resolver.
        injected_search_model = MagicMock(
            spec=BaseEmbeddingModel, name="injected_search"
        )
        injected_search_model.embed = AsyncMock(return_value=[])
        resolution_model = MagicMock(spec=BaseEmbeddingModel, name="resolution_model")
        mocker.patch(
            "tree.memory.extraction.pipeline.get_resolution_embedding_model",
            return_value=resolution_model,
        )
        resolver = MagicMock(spec=CompositeResolver)
        build_resolver = mocker.patch(
            "tree.memory.extraction.pipeline._build_resolver",
            return_value=resolver,
        )

        doc = _make_document()
        mocker.patch(
            "tree.memory.extraction.pipeline.Document.find",
            return_value=MagicMock(to_list=AsyncMock(return_value=[doc])),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.User.get",
            new=AsyncMock(return_value=MagicMock()),
        )
        chunked = ChunkedDocument(
            document_id="507f1f77bcf86cd799439011",
            source_uri="https://example.com/a",
            source_type="huggingface",
            date=None,
            reference_uris=[],
            chunk_texts=[],
            chunk_ids=[],
            structural=ExtractionResult(),
        )
        raw = RawExtraction(
            document_id="507f1f77bcf86cd799439011",
            source_uri="https://example.com/a",
            chunked=chunked,
            extracted=ExtractionResult(),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline._extract_chunks_and_structural",
            new=AsyncMock(return_value=chunked),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline._llm_extract_entities",
            new=AsyncMock(return_value=raw),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline._validate_raws",
            new=AsyncMock(return_value=[raw]),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.redirect_first_person",
            return_value=[],
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.canonicalize_preference_names",
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.get_llm",
            return_value=MagicMock(),
        )
        supersession = mocker.patch(
            "tree.memory.extraction.pipeline.resolve_supersessions",
            new=AsyncMock(),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.write_self_has_preference_edges",
            new=AsyncMock(),
        )
        mocker.patch(
            "tree.memory.extraction.pipeline._resolve_entities",
            new=AsyncMock(return_value=ResolutionOutput()),
        )
        apply_writes = mocker.patch(
            "tree.memory.extraction.pipeline._apply_writes",
            new=AsyncMock(return_value=WriteSummary()),
        )
        client = MagicMock()
        client.__getitem__.return_value = MagicMock()

        await run_extraction_for_documents(
            ["507f1f77bcf86cd799439011"],
            user_id=_USER_ID,
            client=client,
            database_name="test",
            embedding_model=injected_search_model,
        )

        # Resolver built from the resolution model, NOT the injected search model.
        build_resolver.assert_called_once_with(resolution_model)
        # Supersession + writes use the injected search/persisted model.
        assert (
            supersession.await_args.kwargs["embedding_model"] is injected_search_model
        )
        assert injected_search_model in apply_writes.await_args.args
        assert resolution_model not in apply_writes.await_args.args
