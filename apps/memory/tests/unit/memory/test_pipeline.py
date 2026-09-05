"""Unit tests for the memory ingestion pipeline (``tree.memory.pipeline``).

The rag stages (clean+chunk -> embed children -> load rows) run in BOTH modes;
the graph stages run only in ``graphrag``. Most tests exercise a single task
body (``task.fn``) with mocked Mongo / embedding / LLM dependencies; the
mode-branching tests drive the whole worker body against the real test database
(``unit_tests_twin``), because "1 document + 2 parent + 6 child rows and ZERO
edges" is a claim about what landed in Mongo, not about which mock was called.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId
from prefect.cache_policies import NO_CACHE

from tests.unit.conftest import TEST_DATABASE
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.documents import Document, SourceType
from tree.entities.memory import (
    MEMORY_COLLECTION,
    RAG_NODE_TYPES,
    EdgeType,
    NodeType,
    build_edge_id,
    build_node_id,
)
from tree.entities.users import User
from tree.memory.graph.dedup import DeduplicationConfig, DeduplicationResult
from tree import offline, online
from tree.memory import pipeline
from tree.memory.embedding_text import node_to_embedding_text
from tree.memory.pipeline import (
    _CachedSingleEmbedding,
    _split_documents,
    _clean_and_chunk,
    _dedupe_entities,
    _dispatch_entity_write,
    _embed_children,
    _embed_entities,
    _entity_embeddable_text,
    _llm_extract_entities,
    _load_rag_rows,
    _rag_row_id_map,
    _resolve_entities,
    _run_extraction_worker_body,
    _validate_raws,
    child_embedding_texts,
    clean_and_chunk_task,
    embed_children_task,
    embed_entities_task,
    llm_extract_entities_task,
    load_rag_rows_task,
    memory_extract_etl_worker,
)
from tree.memory.rag.embedding import child_embedding_text
from tree.memory.rag.load import (
    child_chunk_name,
    child_row_id,
    document_row_id,
    parent_chunk_name,
    parent_row_id,
)
from tree.memory.rag.types import ChildChunk, ParentChunk
from tree.memory.graph.resolution.composite import CompositeResolver
from tree.memory.graph.resolution.types import ResolvedEntity
from tree.models.base import BaseEmbeddingModel, BaseLLM
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM, MockEmbeddingModel
from tree.memory.types import (
    ChunkedDocument,
    DedupDecision,
    DedupMap,
    EmbeddingMap,
    ExtractedEdge,
    ExtractedNode,
    ExtractionResult,
    RawExtraction,
    ResolutionOutput,
    WriteSummary,
    make_entity_key,
)


# The canned LLM emission the extraction tests drive through the pipeline.
_PERSON_RESPONSE = {
    "nodes": [{"name": "alice", "type": "person", "properties": {}}],
    "edges": [],
}

# A stable user_id used across the unit suite.
_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_PH = str(_USER_ID)
# The memory-collection key; the bulk-write DB mock returns the same collection
# for any subscript, so the exact string is irrelevant to the assertions.
_MEMORY_COLLECTION_SENTINEL = MEMORY_COLLECTION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_document(
    *,
    doc_id: str = "507f1f77bcf86cd799439011",
    content: str = "Alice ships ML pipelines.",
    source_uri: str = "https://example.com/a",
    title: str | None = "A Title",
) -> Any:
    """Build a minimal Document-like object that ``_clean_and_chunk`` accepts."""

    doc = MagicMock(name="Document")
    doc.id = doc_id
    doc.source_uri = source_uri
    doc.source_type = MagicMock(value="huggingface")
    doc.title = title
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
# Task ① — clean_and_chunk
# ---------------------------------------------------------------------------


class TestCleanAndChunkTask:
    async def test_empty_content_yields_no_parents(self) -> None:
        doc = _make_document(content="")

        chunked = await _clean_and_chunk(doc)

        assert chunked.parents == []

    async def test_carries_document_metadata_for_the_downstream_stages(self) -> None:
        doc = _make_document(content="some content " * 50, title="Memory for Agents")

        chunked = await _clean_and_chunk(doc)

        assert chunked.document_id == str(doc.id)
        assert chunked.source_uri == doc.source_uri
        assert chunked.source_type == "huggingface"
        # ``title`` is one of the two Contextual-header inputs — it has to
        # survive task ① or every child embeds a headerless text.
        assert chunked.title == "Memory for Agents"

    async def test_task_decorator_is_registered(self) -> None:
        assert clean_and_chunk_task.name == "clean-and-chunk"

    async def test_content_is_cleaned_before_chunking(self) -> None:
        # The Clean step is the FIRST memory stage: the cookie-banner line must
        # never reach the chunker (ADR-006 Decision 7).
        doc = _make_document(content="Intro\nAccept all cookies\nBody")

        chunked = await _clean_and_chunk(doc)

        joined = "".join(parent.content for parent in chunked.parents)
        assert "Accept all cookies" not in joined
        assert "Intro" in joined
        assert "Body" in joined

    async def test_substack_footer_boilerplate_never_reaches_chunks(self) -> None:
        # User story: an ingested Substack article loses its footer boilerplate
        # while the body paragraph survives.
        doc = _make_document(
            content="The body paragraph stays.\n\nThanks for reading!\nShare this post\n"
        )

        chunked = await _clean_and_chunk(doc)

        joined = "".join(parent.content for parent in chunked.parents)
        assert "Thanks for reading" not in joined
        assert "Share this post" not in joined
        assert "The body paragraph stays." in joined

    async def test_output_is_identical_across_two_calls(self) -> None:
        # User story: re-running task ① on the same Document yields an identical
        # payload, so the downstream INPUTS cache is a hit.
        content = "Intro\nAccept all cookies\nBody   text"

        first = await _clean_and_chunk(_make_document(content=content))
        second = await _clean_and_chunk(_make_document(content=content))

        assert first == second

    async def test_three_parent_document_yields_deterministic_row_names(
        self, monkeypatch
    ) -> None:
        # Squeeze the parent budget so a short document really splits into three
        # parents, then check the names the loader derives from the result.
        monkeypatch.setenv("TREE_MEMORY__CHUNKING__PARENT__SIZE", "20")
        monkeypatch.setenv("TREE_MEMORY__CHUNKING__CHILD__SIZE", "8")
        monkeypatch.setenv("TREE_MEMORY__CHUNKING__CHILD__OVERLAP", "0")
        doc = _make_document(
            content="\n\n".join(
                f"Paragraph {i} " + "filler words here " * 4 for i in range(3)
            )
        )

        chunked = await _clean_and_chunk(doc)

        assert len(chunked.parents) == 3
        assert [
            parent_chunk_name(chunked.source_uri, parent.index)
            for parent in chunked.parents
        ] == [f"{doc.source_uri}#parent-{i}" for i in range(3)]
        first_children = chunked.parents[0].children
        assert [
            child_chunk_name(chunked.source_uri, 0, child.index)
            for child in first_children
        ] == [
            f"{doc.source_uri}#parent-0#child-{i}" for i in range(len(first_children))
        ]


# ---------------------------------------------------------------------------
# Task ② — embed_children
# ---------------------------------------------------------------------------


class _SpyEmbeddingModel(BaseEmbeddingModel):
    """Records every text it is asked to embed."""

    def __init__(self, dimensions: int = 4) -> None:
        self._dimensions = dimensions
        self.texts: list[str] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[0.5] * self._dimensions for _ in texts]


def _chunked_with_children(
    *,
    document_id: str = "507f1f77bcf86cd799439011",
    source_uri: str = "https://example.com/a",
    title: str | None = "A Title",
    shape: tuple[int, ...] = (2,),
) -> ChunkedDocument:
    """A ChunkedDocument with ``shape[i]`` children under parent ``i``."""

    return ChunkedDocument(
        document_id=document_id,
        source_uri=source_uri,
        source_type="huggingface",
        title=title,
        parents=[
            ParentChunk(
                index=parent_index,
                content=f"parent {parent_index} body",
                heading_path=["Memory", f"Section {parent_index}"],
                children=[
                    ChildChunk(
                        index=child_index,
                        content=f"child {parent_index}.{child_index} body",
                    )
                    for child_index in range(n_children)
                ],
            )
            for parent_index, n_children in enumerate(shape)
        ],
    )


class TestChildEmbeddingTexts:
    def test_returns_contextual_header_text_not_raw_content(self) -> None:
        chunked = _chunked_with_children(shape=(1,))

        texts = child_embedding_texts([chunked])

        assert texts == [
            child_embedding_text(
                title="A Title",
                heading_path=["Memory", "Section 0"],
                content="child 0.0 body",
            )
        ]
        assert texts[0] != "child 0.0 body"

    def test_is_sorted_and_deduplicated(self) -> None:
        # Two documents whose (title, heading path, content) collide embed once.
        chunked = _chunked_with_children(shape=(2, 1))
        twin = _chunked_with_children(shape=(2, 1))

        texts = child_embedding_texts([chunked, twin])

        assert texts == sorted(set(texts))
        assert len(texts) == 3


class TestEmbedChildrenTask:
    async def test_embeds_every_text_and_returns_a_text_to_vector_map(
        self, mocker
    ) -> None:
        model = _SpyEmbeddingModel(dimensions=4)
        mocker.patch(
            "tree.memory.pipeline.get_search_embedding_model", return_value=model
        )

        vectors = await _embed_children(["a", "b"])

        assert model.texts == ["a", "b"]
        assert vectors == {"a": [0.5] * 4, "b": [0.5] * 4}

    async def test_empty_input_returns_empty_without_calling_model(
        self, mocker
    ) -> None:
        factory = mocker.patch("tree.memory.pipeline.get_search_embedding_model")

        assert await _embed_children([]) == {}

        factory.assert_not_called()

    async def test_task_decorator_is_registered(self) -> None:
        assert embed_children_task.name == "embed-children"


# ---------------------------------------------------------------------------
# Task ③ — load_rag_rows
# ---------------------------------------------------------------------------


class TestLoadRagRowsTask:
    async def test_writes_every_row_in_one_unordered_bulk_write(self) -> None:
        chunked = _chunked_with_children(shape=(2, 1))
        collection = MagicMock(name="memory_collection")
        collection.bulk_write = AsyncMock(return_value=MagicMock())
        collection.update_one = AsyncMock(return_value=MagicMock())
        database = MagicMock(name="database")
        database.__getitem__.return_value = collection

        rows = await _load_rag_rows([chunked], {}, database, _USER_ID)

        # 1 document + 2 parents + 3 children.
        assert rows == 6
        collection.update_one.assert_not_awaited()
        assert collection.bulk_write.await_count == 1
        assert collection.bulk_write.await_args.kwargs.get("ordered") is False
        assert len(collection.bulk_write.await_args.args[0]) == 6

    async def test_no_documents_issues_no_write(self) -> None:
        collection = MagicMock(name="memory_collection")
        collection.bulk_write = AsyncMock(return_value=MagicMock())
        database = MagicMock(name="database")
        database.__getitem__.return_value = collection

        rows = await _load_rag_rows([], {}, database, _USER_ID)

        assert rows == 0
        collection.bulk_write.assert_not_awaited()

    async def test_task_is_uncached_and_retried(self) -> None:
        assert load_rag_rows_task.name == "load-rag-rows"
        assert load_rag_rows_task.cache_policy is NO_CACHE
        assert load_rag_rows_task.retries == 3


# ---------------------------------------------------------------------------
# Task ④ — llm_extract_entities (graphrag only)
# ---------------------------------------------------------------------------


class TestLlmExtractEntitiesTask:
    async def test_no_parents_returns_empty(self) -> None:
        chunked = ChunkedDocument(
            document_id="d1", source_uri="u1", source_type="huggingface"
        )

        raw = await _llm_extract_entities(chunked, _USER_ID)

        assert raw.extracted.nodes == []
        assert raw.extracted.edges == []

    async def test_calls_llm_once_per_parent(self, mocker) -> None:
        fake = FakeLLM(responses=[_PERSON_RESPONSE] * 3)
        mocker.patch("tree.memory.pipeline.get_llm", return_value=fake)
        chunked = _chunked_with_children(shape=(2, 2, 2))

        raw = await _llm_extract_entities(chunked, _USER_ID)

        # 3 parents -> 3 calls (NOT 6, one per child).
        assert fake.call_count == 3
        assert len(raw.extracted.nodes) == 3

    async def test_stamps_the_parent_row_id_as_chunk_id_provenance(
        self, mocker
    ) -> None:
        mocker.patch(
            "tree.memory.pipeline.get_llm",
            return_value=FakeLLM(responses=[_PERSON_RESPONSE] * 2),
        )
        chunked = _chunked_with_children(shape=(1, 1))

        raw = await _llm_extract_entities(chunked, _USER_ID)

        assert {node.chunk_id for node in raw.extracted.nodes} == {
            parent_row_id(_USER_ID, chunked.source_uri, 0),
            parent_row_id(_USER_ID, chunked.source_uri, 1),
        }

    async def test_task_decorator_is_registered(self) -> None:
        assert llm_extract_entities_task.name == "llm-extract-entities"


# ---------------------------------------------------------------------------
# Task ⑤ — resolve_entities
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
# Task ⑥ — embed_entities (batched, #044)
# ---------------------------------------------------------------------------


class TestEmbedEntitiesTask:
    async def test_embeds_all_texts_in_one_batched_call(self, mocker) -> None:
        # #044: task ④ embeds EVERY run node-text in a single batched call
        # (via embed_in_batches over the SEARCH model) and returns a
        # ``text -> vector`` map.
        model = MagicMock()
        model.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
        search_factory = mocker.patch(
            "tree.memory.pipeline.get_search_embedding_model",
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
            "tree.memory.pipeline.get_search_embedding_model",
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
# Task ⑦ — dedupe_entities
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
            "tree.memory.pipeline.dedupe_entity",
            new=AsyncMock(),
        )

        out = await _dedupe_entities(resolved, embeddings, database, cfg, _USER_ID)
        assert out.decisions["d1|person|alice"].action == "none"
        dedupe_spy.assert_not_called()

    async def test_self_match_is_dropped(self, mocker) -> None:
        cfg = DeduplicationConfig()
        # ``dedupe_entity`` returns a "merged" with the same id as prospective.
        mocker.patch(
            "tree.memory.pipeline.dedupe_entity",
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
# Task ⑦ — dedupe_entities parallelization (#058)
# ---------------------------------------------------------------------------


def _multi_entity_resolved(n: int) -> Any:
    """Build a ``ResolutionOutput`` with ``n`` distinct PERSON entities.

    Each entity gets a unique key (``d1|person|name<i>``), canonical name,
    and a distinct embeddable text so the embedding map can return a
    per-entity vector.
    """

    resolved_by_key: dict[str, Any] = {}
    embeddable_text_by_key: dict[str, str] = {}
    entities: list[tuple[str, NodeType]] = []
    for i in range(n):
        name = f"name{i}"
        key = f"d1|person|{name}"
        text = f"text-{name}"
        resolved_by_key[key] = ResolvedEntity(
            original_name=name,
            canonical_name=name,
            entity_type=NodeType.PERSON,
            confidence=1.0,
            match_type="exact",
        )
        embeddable_text_by_key[key] = text
        entities.append((name, NodeType.PERSON))
    return ResolutionOutput(
        entities=entities,
        resolved_by_key=resolved_by_key,
        embeddable_text_by_key=embeddable_text_by_key,
    )


def _embeddings_for(resolved: Any, *, dim: int = 8) -> Any:
    """Seed a vector for every entity's embeddable text."""

    return EmbeddingMap(
        vectors={text: [0.1] * dim for text in resolved.embeddable_text_by_key.values()}
    )


async def _sequential_reference(
    resolved: Any,
    embeddings: Any,
    database: Any,
    dedup_config: Any,
    user_id: Any,
    dedupe_fn: Any,
) -> tuple[dict[str, Any], int, int, int]:
    """A faithful sequential re-implementation of the dedupe task body.

    Used as the golden oracle: the parallel implementation must produce a
    byte-identical ``decisions`` mapping and identical tallies for the same
    input. ``dedupe_fn`` is the (mocked) ``dedupe_entity``.
    """

    from tree.entities.memory import build_node_id as _build_node_id
    from tree.memory.pipeline import _normalize, _to_decision

    decisions: dict[str, Any] = {}
    n_merged = n_flagged = n_none = 0
    for key, resolved_entity in resolved.resolved_by_key.items():
        doc_id, type_value, name = key.split("|", maxsplit=2)
        entity_type = NodeType(type_value)
        embeddable_text = resolved.embeddable_text_by_key.get(
            key, resolved_entity.canonical_name
        )
        embedding = embeddings.vectors.get(embeddable_text) or []
        if not embedding or not dedup_config.enabled:
            decisions[key] = DedupDecision(action="none")
            n_none += 1
            continue
        prospective_id = _build_node_id(user_id, entity_type, _normalize(name))
        raw = await dedupe_fn(
            database=database,
            user_id=user_id,
            name=name,
            entity_type=entity_type,
            embedding=embedding,
            config=dedup_config,
            incoming_node_id=prospective_id,
        )
        decision = _to_decision(raw, prospective_id)
        decisions[key] = decision
        if decision.action == "merged":
            n_merged += 1
        elif decision.action == "flagged":
            n_flagged += 1
        else:
            n_none += 1
    return decisions, n_merged, n_flagged, n_none


def _result_for_name(name: str) -> DeduplicationResult:
    """A deterministic per-name dedupe result: merged / flagged / none by index.

    Cycles through the three actions so a fixed fixture exercises every tally
    bucket. ``matched_node_id`` is intentionally distinct from any prospective
    id so ``_to_decision`` never collapses it to a self-match.
    """

    idx = int(name.removeprefix("name"))
    bucket = idx % 3
    if bucket == 0:
        return DeduplicationResult(
            action="merged",
            matched_node_id=f"other:person:match-{name}",
            matched_node_name=f"match-{name}",
            similarity_score=0.99,
            match_type="embedding",
        )
    if bucket == 1:
        return DeduplicationResult(
            action="flagged",
            matched_node_id=f"other:person:match-{name}",
            matched_node_name=f"match-{name}",
            similarity_score=0.85,
            match_type="embedding",
        )
    return DeduplicationResult(action="none")


class TestDedupeEntitiesParallelization:
    """#058 — the dedupe task runs decisions concurrently under a semaphore
    sized by ``dedup_concurrency`` while producing byte-identical output to the
    sequential reference."""

    async def test_decisions_run_concurrently_under_semaphore(self, mocker) -> None:
        # Arrange: bound concurrency to 4; track how many dedupe calls run at once.
        mocker.patch.dict(
            "os.environ", {"TREE_EXTRACTION__DEDUP_CONCURRENCY": "4"}, clear=False
        )
        cfg = DeduplicationConfig()
        resolved = _multi_entity_resolved(12)
        embeddings = _embeddings_for(resolved)

        in_flight = 0
        max_in_flight = 0
        gate = asyncio.Event()
        started = 0

        async def _slow_dedupe(**kwargs: Any) -> DeduplicationResult:
            nonlocal in_flight, max_in_flight, started
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            started += 1
            # Once the first batch (== concurrency) is in flight, release.
            if started >= 4:
                gate.set()
            await gate.wait()
            await asyncio.sleep(0)
            in_flight -= 1
            return _result_for_name(kwargs["name"])

        mocker.patch(
            "tree.memory.pipeline.dedupe_entity",
            new=AsyncMock(side_effect=_slow_dedupe),
        )

        out = await _dedupe_entities(resolved, embeddings, MagicMock(), cfg, _USER_ID)

        # Assert: never more than the semaphore bound ran simultaneously, and
        # we actually reached the bound (i.e. it ran in parallel, not serially).
        assert max_in_flight == 4
        assert len(out.decisions) == 12

    async def test_no_sequential_await_loop_remains(self) -> None:
        # Diff-guard: the task body must use asyncio.gather + a Semaphore and
        # must NOT iterate with a bare ``for ... await dedupe_entity`` loop.
        import inspect

        from tree.memory.pipeline import _dedupe_entities as fn

        src = inspect.getsource(fn)
        assert "asyncio.gather" in src
        assert "Semaphore" in src
        assert "dedup_concurrency" in src

    async def test_no_embed_call_in_dedupe_task(self) -> None:
        # The dedupe task must read precomputed vectors only — no Voyage embed.
        import inspect

        from tree.memory.pipeline import _dedupe_entities as fn

        src = inspect.getsource(fn)
        assert ".embed(" not in src

    async def test_identical_output_to_sequential_reference(self, mocker) -> None:
        cfg = DeduplicationConfig()
        resolved = _multi_entity_resolved(15)
        embeddings = _embeddings_for(resolved)
        database = MagicMock()

        async def _dedupe(**kwargs: Any) -> DeduplicationResult:
            return _result_for_name(kwargs["name"])

        mocker.patch(
            "tree.memory.pipeline.dedupe_entity",
            new=AsyncMock(side_effect=_dedupe),
        )

        # Act — parallel implementation
        out = await _dedupe_entities(resolved, embeddings, database, cfg, _USER_ID)

        # Oracle — sequential reference over the same fixture
        ref_decisions, n_merged, n_flagged, n_none = await _sequential_reference(
            resolved, embeddings, database, cfg, _USER_ID, _dedupe
        )

        # Assert: identical mapping (keys + per-key decision) and tallies.
        assert set(out.decisions.keys()) == set(ref_decisions.keys())
        for key, decision in ref_decisions.items():
            got = out.decisions[key]
            assert got.action == decision.action
            assert got.matched_node_id == decision.matched_node_id
            assert got.matched_node_name == decision.matched_node_name
            assert got.similarity_score == decision.similarity_score
            assert got.match_type == decision.match_type
        # Sanity: every tally bucket was exercised by the fixture.
        assert n_merged > 0 and n_flagged > 0 and n_none > 0

    async def test_concurrency_one_reproduces_sequential(
        self, mocker, monkeypatch
    ) -> None:
        # Degenerate concurrency=1 must produce identical output to the
        # sequential reference (concurrency knob honored, behavior preserved).
        monkeypatch.setenv("TREE_EXTRACTION__DEDUP_CONCURRENCY", "1")
        cfg = DeduplicationConfig()
        resolved = _multi_entity_resolved(9)
        embeddings = _embeddings_for(resolved)
        database = MagicMock()

        async def _dedupe(**kwargs: Any) -> DeduplicationResult:
            return _result_for_name(kwargs["name"])

        mocker.patch(
            "tree.memory.pipeline.dedupe_entity",
            new=AsyncMock(side_effect=_dedupe),
        )

        out = await _dedupe_entities(resolved, embeddings, database, cfg, _USER_ID)
        ref_decisions, _, _, _ = await _sequential_reference(
            resolved, embeddings, database, cfg, _USER_ID, _dedupe
        )

        assert {k: v.action for k, v in out.decisions.items()} == {
            k: v.action for k, v in ref_decisions.items()
        }

    async def test_disabled_and_missing_embedding_short_circuit_per_key(
        self, mocker
    ) -> None:
        # Per-key early-continue branches must be preserved under parallelism:
        # a key with no embedding -> action="none" without calling dedupe_entity.
        cfg = DeduplicationConfig()
        resolved = _multi_entity_resolved(3)
        # Drop the vector for the middle entity so its embedding resolves empty.
        embeddings = _embeddings_for(resolved)
        embeddings.vectors.pop("text-name1")

        dedupe_spy = mocker.patch(
            "tree.memory.pipeline.dedupe_entity",
            new=AsyncMock(side_effect=lambda **kw: _result_for_name(kw["name"])),
        )

        out = await _dedupe_entities(resolved, embeddings, MagicMock(), cfg, _USER_ID)

        # The embedding-less key short-circuits to "none".
        assert out.decisions["d1|person|name1"].action == "none"
        # ``dedupe_entity`` was called only for the two keys that had vectors.
        called_names = {call.kwargs["name"] for call in dedupe_spy.call_args_list}
        assert called_names == {"name0", "name2"}


# ---------------------------------------------------------------------------
# R7 (#059) — doc-level chunking fan-out
# ---------------------------------------------------------------------------


def _chunked_for(doc_id: str) -> ChunkedDocument:
    """A trivial, identity-tagged ChunkedDocument for the chunking fan-out tests."""

    return ChunkedDocument(
        document_id=doc_id,
        source_uri=f"u-{doc_id}",
        source_type="huggingface",
        parents=[ParentChunk(index=0, content=f"parent-{doc_id}")],
    )


class TestChunkDocumentsFanout:
    """#059 R7 — the per-doc chunking task ① is fanned out under a bounded
    semaphore sized by ``doc_concurrency``. Output ORDER + contents must stay
    byte-identical to the sequential path; the LLM task ② loop is untouched."""

    async def test_default_concurrency_one_preserves_order_and_contents(
        self, mocker, monkeypatch
    ) -> None:
        # Arrange: default doc_concurrency=1 (serial-equivalent).
        monkeypatch.delenv("TREE_EXTRACTION__DOC_CONCURRENCY", raising=False)
        docs = [_make_document(doc_id=f"d{i}") for i in range(5)]

        async def _fake_task(doc: Any, **_kwargs: Any) -> ChunkedDocument:
            return _chunked_for(str(doc.id))

        mocker.patch(
            "tree.memory.pipeline.clean_and_chunk_task",
            new=AsyncMock(side_effect=_fake_task),
        )

        chunked_docs = await _split_documents(docs)

        # Assert: same order + contents as a straight sequential map.
        expected = [_chunked_for(str(d.id)) for d in docs]
        assert chunked_docs == expected
        assert [c.document_id for c in chunked_docs] == [str(d.id) for d in docs]

    async def test_concurrency_above_one_runs_concurrently_bounded(
        self, mocker, monkeypatch
    ) -> None:
        # Arrange: bound concurrency to 4; track max simultaneous in-flight.
        monkeypatch.setenv("TREE_EXTRACTION__DOC_CONCURRENCY", "4")
        docs = [_make_document(doc_id=f"d{i}") for i in range(12)]

        in_flight = 0
        max_in_flight = 0
        gate = asyncio.Event()
        started = 0

        async def _slow_task(doc: Any, **_kwargs: Any) -> ChunkedDocument:
            nonlocal in_flight, max_in_flight, started
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            started += 1
            # Once the first wave (== concurrency) is in flight, release.
            if started >= 4:
                gate.set()
            await gate.wait()
            await asyncio.sleep(0)
            in_flight -= 1
            return _chunked_for(str(doc.id))

        mocker.patch(
            "tree.memory.pipeline.clean_and_chunk_task",
            new=AsyncMock(side_effect=_slow_task),
        )

        chunked_docs = await _split_documents(docs)

        # Assert: ran in parallel up to — but never beyond — the bound.
        assert max_in_flight == 4
        # Determinism: order + contents identical to the sequential result.
        assert chunked_docs == [_chunked_for(str(d.id)) for d in docs]

    async def test_concurrency_above_one_identical_to_sequential(
        self, mocker, monkeypatch
    ) -> None:
        # Even when tasks finish OUT of dispatch order, gather preserves input
        # order, so the result is byte-identical to a sequential map.
        monkeypatch.setenv("TREE_EXTRACTION__DOC_CONCURRENCY", "4")
        docs = [_make_document(doc_id=f"d{i}") for i in range(8)]

        async def _jittered_task(doc: Any, **_kwargs: Any) -> ChunkedDocument:
            # Later docs finish first (reverse-skewed delay) to expose any
            # ordering bug in result collection.
            idx = int(str(doc.id)[1:])
            await asyncio.sleep((len(docs) - idx) * 0.001)
            return _chunked_for(str(doc.id))

        mocker.patch(
            "tree.memory.pipeline.clean_and_chunk_task",
            new=AsyncMock(side_effect=_jittered_task),
        )

        chunked_docs = await _split_documents(docs)

        assert chunked_docs == [_chunked_for(str(d.id)) for d in docs]

    async def test_empty_docs_returns_empty_list(self, mocker) -> None:
        task_spy = mocker.patch(
            "tree.memory.pipeline.clean_and_chunk_task",
            new=AsyncMock(),
        )

        out = await _split_documents([])

        assert out == []
        task_spy.assert_not_called()

    async def test_uses_gather_and_semaphore_gated_by_doc_concurrency(self) -> None:
        # Diff-guard: the chunking fan-out must use asyncio.gather + a Semaphore
        # sized by ``doc_concurrency`` — not a bare sequential await loop.
        import inspect

        fn = _split_documents

        src = inspect.getsource(fn)
        assert "asyncio.gather" in src
        assert "Semaphore" in src
        assert "doc_concurrency" in src

    async def test_llm_task_loop_left_sequential(self) -> None:
        # R6 is OUT OF SCOPE: the LLM task loop in the flow body must remain a
        # plain sequential ``for chunked in chunked_docs`` await loop with NO
        # added fan-out around ``llm_extract_entities_task``.
        import inspect

        src = inspect.getsource(_run_extraction_worker_body)
        assert "for chunked in chunked_docs:" in src
        assert "llm_extract_entities_task(" in src
        # The chunking task ① loop is the bounded gather helper.
        assert "_split_documents(" in src


# ---------------------------------------------------------------------------
# R4 (#059) — _validate_raws audit writes via a single insert_many
# ---------------------------------------------------------------------------


class TestValidateRawsInsertMany:
    """#059 R4 — audit/rejection rows are accumulated and written with a single
    ``insert_many`` per collection. The row CONTENTS and the SET of rows written
    must match the prior per-row ``insert_one`` behavior."""

    @staticmethod
    def _make_database() -> Any:
        """A database mock whose collections record insert_one/insert_many."""

        def _collection(_name: str) -> Any:
            col = MagicMock(name=f"col-{_name}")
            col.insert_one = AsyncMock(return_value=MagicMock())
            col.insert_many = AsyncMock(return_value=MagicMock())
            return col

        cols: dict[str, Any] = {}

        def _getitem(name: str) -> Any:
            if name not in cols:
                cols[name] = _collection(name)
            return cols[name]

        database = MagicMock(name="database")
        database.__getitem__.side_effect = _getitem
        database._cols = cols  # expose for assertions
        return database

    @staticmethod
    def _extractor() -> Any:
        from tree.entities.memory import ExtractorInfo

        return ExtractorInfo(name="fake-llm", version="tree-memory-0.0.0+test")

    def _raw_with_rejections_and_drops(self) -> RawExtraction:
        from tree.memory.types import RawRejection

        # One parser-level raw_rejection, one envelope-invalid node, one valid
        # node with a dropped field, one envelope-invalid edge, one valid edge
        # with a dropped field — exercises every audit-write branch.
        bad_node = ExtractedNode(
            name="",  # empty name -> envelope invalid
            type=NodeType.PERSON,
            subtype=None,
            properties={},
            chunk_id="c1",
        )
        good_node = ExtractedNode(
            name="Alice",
            type=NodeType.PERSON,
            subtype="individual",  # passes the envelope (subtype required)
            properties={"email": "a@x.com", "bogus_field": 5},
            chunk_id="c1",
        )
        result = ExtractionResult(
            nodes=[bad_node, good_node],
            edges=[],
            raw_rejections=[
                RawRejection(
                    kind="node",
                    reason="unknown_type",
                    raw={"type": "weird", "name": "x"},
                    chunk_id="c1",
                )
            ],
        )
        return RawExtraction(
            document_id="507f1f77bcf86cd799439011",
            source_uri="u1",
            chunked=ChunkedDocument(
                document_id="507f1f77bcf86cd799439011",
                source_uri="u1",
                source_type="huggingface",
            ),
            extracted=result,
        )

    async def test_writes_one_insert_many_per_collection_no_insert_one(self) -> None:
        database = self._make_database()
        raw = self._raw_with_rejections_and_drops()

        await _validate_raws(
            raws=[raw],
            database=database,
            user_id=_USER_ID,
            extractor=self._extractor(),
        )

        rej = database["extraction_rejections"]
        dropped = database["extraction_dropped_fields"]
        # Per-row insert_one is gone; a single insert_many per collection.
        rej.insert_one.assert_not_called()
        dropped.insert_one.assert_not_called()
        rej.insert_many.assert_called_once()
        dropped.insert_many.assert_called_once()

    async def test_rejection_rows_match_prior_per_row_contents(self) -> None:
        database = self._make_database()
        raw = self._raw_with_rejections_and_drops()

        await _validate_raws(
            raws=[raw],
            database=database,
            user_id=_USER_ID,
            extractor=self._extractor(),
        )

        rows = database["extraction_rejections"].insert_many.call_args.args[0]
        # Two rejections: the parser raw_rejection + the empty-name envelope drop.
        reasons = [r["rejection_reason"] for r in rows]
        assert "unknown_type" in reasons
        assert len(rows) == 2
        # Every row carries the expected scalar fields (contents preserved).
        for r in rows:
            assert r["user_id"] == _USER_ID
            assert r["rejected_at_stage"] == "envelope"
            assert "raw_row" in r and "extractor" in r and "timestamp" in r
            assert "_id" not in r

    async def test_dropped_field_rows_match_prior_per_row_contents(self) -> None:
        database = self._make_database()
        raw = self._raw_with_rejections_and_drops()

        await _validate_raws(
            raws=[raw],
            database=database,
            user_id=_USER_ID,
            extractor=self._extractor(),
        )

        rows = database["extraction_dropped_fields"].insert_many.call_args.args[0]
        # The good node's "bogus_field" is dropped -> exactly one dropped row.
        assert len(rows) == 1
        assert rows[0]["dropped_field"] == "bogus_field"
        assert rows[0]["user_id"] == _USER_ID
        assert "_id" not in rows[0]

    async def test_empty_input_writes_nothing(self) -> None:
        database = self._make_database()

        out = await _validate_raws(
            raws=[],
            database=database,
            user_id=_USER_ID,
            extractor=self._extractor(),
        )

        assert out == []
        # No collections touched at all — no insert_one, no insert_many.
        assert database._cols == {}

    async def test_clean_raws_write_no_audit_rows(self) -> None:
        # A raw with only valid nodes/edges must produce NO insert_many calls
        # (empty accumulator -> no-op, no crash).
        database = self._make_database()
        clean = RawExtraction(
            document_id="507f1f77bcf86cd799439011",
            source_uri="u1",
            chunked=ChunkedDocument(
                document_id="507f1f77bcf86cd799439011",
                source_uri="u1",
                source_type="huggingface",
            ),
            extracted=ExtractionResult(
                nodes=[
                    ExtractedNode(
                        name="Alice",
                        type=NodeType.PERSON,
                        subtype="individual",  # passes envelope; no drops
                        properties={"email": "a@x.com"},
                        chunk_id="c1",
                    )
                ],
                edges=[],
            ),
        )

        await _validate_raws(
            raws=[clean],
            database=database,
            user_id=_USER_ID,
            extractor=self._extractor(),
        )

        for col in database._cols.values():
            col.insert_many.assert_not_called()
            col.insert_one.assert_not_called()


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
            # The flow re-loads the config at entry, before any work runs.
            await memory_extract_etl_worker.fn(user_id=_USER_ID)

    async def test_dedup_threshold_inversion_raises_value_error(
        self, monkeypatch
    ) -> None:
        # auto_merge_threshold MUST be strictly greater than flag_threshold.
        monkeypatch.setenv("TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD", "0.5")
        monkeypatch.setenv("TREE_EXTRACTION__DEDUP__FLAG_THRESHOLD", "0.8")

        with pytest.raises(ValueError, match="auto_merge_threshold.*flag_threshold"):
            await memory_extract_etl_worker.fn(user_id=_USER_ID)


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
    """ADR-006 decision 8: ONE module holding the rag tasks, the graph tasks and
    all three memory flows — under their UNCHANGED names."""

    @pytest.mark.parametrize(
        "task_name",
        [
            "clean_and_chunk_task",
            "embed_children_task",
            "load_rag_rows_task",
            "llm_extract_entities_task",
            "resolve_entities_task",
            "embed_entities_task",
            "dedupe_entities_task",
            "apply_writes_task",
            "embed_nodes_task",
            "ensure_indexes_task",
        ],
    )
    def test_task_exported(self, task_name: str) -> None:
        assert hasattr(pipeline, task_name), f"Missing task export: {task_name}"

    def test_flow_names_are_unchanged(self) -> None:
        # Renaming a deployment would orphan its Cloud definition, so the three
        # flow names are part of the contract this move must NOT break.
        assert memory_extract_etl_worker.name == "memory-extract-etl-worker"
        assert (
            pipeline.memory_extract_etl_coordinator.name
            == "memory-extract-etl-coordinator"
        )
        assert pipeline.memory_indexing.name == "memory-indexing-etl"

    def test_the_pipeline_entrypoints_import_the_flows_from_here(self) -> None:
        # ``tree.offline`` / ``tree.online`` must bind the SAME flow objects the
        # deployments serve — a second copy would run un-deployed code.
        assert (
            offline.memory_extract_etl_coordinator
            is pipeline.memory_extract_etl_coordinator
        )
        assert online.memory_extract_etl_worker is pipeline.memory_extract_etl_worker
        assert online.memory_indexing is pipeline.memory_indexing

    def test_legacy_run_extraction_helper_is_gone(self) -> None:
        # #108 deleted the no-production-caller helper; its coverage lives in
        # the worker-body tests.
        assert not hasattr(pipeline, "run_extraction_for_documents")


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
            await memory_extract_etl_worker.fn(document_ids=["x"])  # type: ignore[call-arg]

    async def test_indexing_flow_missing_user_id_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="user_id"):
            await pipeline.memory_indexing.fn()  # type: ignore[call-arg]


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
            "tree.memory.pipeline.add_entity",
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
        from tree.memory.pipeline import _build_resolver

        sentinel_model = MagicMock(spec=BaseEmbeddingModel)

        resolver = _build_resolver(sentinel_model)

        assert isinstance(resolver, CompositeResolver)
        # The semantic stage is the only resolver leg that holds an
        # embedding model; it must be the model we passed in.
        assert resolver._semantic is not None
        assert resolver._semantic._embedding_model is sentinel_model


class TestFlowEmbeddingModelSplit:
    """The graphrag worker holds two distinct embedding handles.

    Resolver ← ``get_resolution_embedding_model()`` (transient name vector).
    Dedup / writes / supersession ← ``get_search_embedding_model()``
    (persisted node vector). The two MUST be distinct objects so the
    operator can later swap a lighter resolution model without touching the
    persisted-vector space.
    """

    @pytest.fixture
    def stubbed_graph_stages(self, mocker) -> dict[str, Any]:
        """Mock every stage of the worker body except the model wiring."""

        resolution_model = MagicMock(spec=BaseEmbeddingModel, name="resolution_model")
        search_model = MagicMock(spec=BaseEmbeddingModel, name="search_model")
        mocker.patch(
            "tree.memory.pipeline.get_resolution_embedding_model",
            return_value=resolution_model,
        )
        mocker.patch(
            "tree.memory.pipeline.get_search_embedding_model",
            return_value=search_model,
        )
        build_resolver = mocker.patch(
            "tree.memory.pipeline._build_resolver",
            return_value=MagicMock(spec=CompositeResolver),
        )

        mock_client = MagicMock()
        mock_client.__getitem__.return_value = MagicMock()
        mocker.patch(
            "tree.memory.pipeline.init_mongodb",
            new=AsyncMock(return_value=mock_client),
        )
        doc = _make_document()
        mocker.patch(
            "tree.memory.pipeline.Document.find",
            return_value=MagicMock(to_list=AsyncMock(return_value=[doc])),
        )
        mocker.patch(
            "tree.memory.pipeline.User.get", new=AsyncMock(return_value=MagicMock())
        )

        chunked = ChunkedDocument(
            document_id="507f1f77bcf86cd799439011",
            source_uri="https://example.com/a",
            source_type="huggingface",
            parents=[],
        )
        raw = RawExtraction(
            document_id="507f1f77bcf86cd799439011",
            source_uri="https://example.com/a",
            chunked=chunked,
            extracted=ExtractionResult(),
        )
        mocker.patch(
            "tree.memory.pipeline.clean_and_chunk_task",
            new=AsyncMock(return_value=chunked),
        )
        mocker.patch(
            "tree.memory.pipeline.embed_children_task", new=AsyncMock(return_value={})
        )
        mocker.patch(
            "tree.memory.pipeline.load_rag_rows_task", new=AsyncMock(return_value=0)
        )
        mocker.patch(
            "tree.memory.pipeline.llm_extract_entities_task",
            new=AsyncMock(return_value=raw),
        )
        mocker.patch(
            "tree.memory.pipeline.validate_raws_task", new=AsyncMock(return_value=[raw])
        )
        mocker.patch("tree.memory.pipeline.redirect_first_person", return_value=[])
        mocker.patch("tree.memory.pipeline.canonicalize_preference_names")
        mocker.patch("tree.memory.pipeline.get_llm", return_value=MagicMock())
        supersession = mocker.patch(
            "tree.memory.pipeline.resolve_supersessions", new=AsyncMock()
        )
        mocker.patch(
            "tree.memory.pipeline.write_self_has_preference_edges", new=AsyncMock()
        )
        mocker.patch(
            "tree.memory.pipeline.resolve_entities_task",
            new=AsyncMock(return_value=ResolutionOutput()),
        )
        mocker.patch(
            "tree.memory.pipeline.embed_entities_task", new=AsyncMock(return_value={})
        )
        mocker.patch(
            "tree.memory.pipeline.dedupe_entities_task",
            new=AsyncMock(return_value=DedupMap()),
        )
        apply_writes = mocker.patch(
            "tree.memory.pipeline.apply_writes_task",
            new=AsyncMock(return_value=WriteSummary()),
        )
        return {
            "resolution_model": resolution_model,
            "search_model": search_model,
            "build_resolver": build_resolver,
            "supersession": supersession,
            "apply_writes": apply_writes,
        }

    async def test_builds_the_resolver_from_the_resolution_model(
        self, stubbed_graph_stages
    ) -> None:
        await memory_extract_etl_worker.fn(
            user_id=_USER_ID, document_ids=["507f1f77bcf86cd799439011"]
        )

        # The resolver is built from the RESOLUTION model, never the search model.
        stubbed_graph_stages["build_resolver"].assert_called_once_with(
            stubbed_graph_stages["resolution_model"]
        )
        assert (
            stubbed_graph_stages["resolution_model"]
            is not stubbed_graph_stages["search_model"]
        )

    async def test_threads_the_search_model_into_supersession_and_writes(
        self, stubbed_graph_stages
    ) -> None:
        await memory_extract_etl_worker.fn(
            user_id=_USER_ID, document_ids=["507f1f77bcf86cd799439011"]
        )

        search_model = stubbed_graph_stages["search_model"]
        supersession = stubbed_graph_stages["supersession"]
        apply_writes = stubbed_graph_stages["apply_writes"]
        # Supersession compares against PERSISTED preference vectors → SEARCH model.
        assert supersession.await_args.kwargs["embedding_model"] is search_model
        # apply-writes persists node vectors → SEARCH model.
        assert search_model in apply_writes.await_args.args
        assert (
            stubbed_graph_stages["resolution_model"] not in apply_writes.await_args.args
        )

    async def test_rag_mode_never_builds_the_graph_model_handles(
        self, mocker, monkeypatch, stubbed_graph_stages
    ) -> None:
        monkeypatch.setenv("TREE_MEMORY__MODE", "rag")

        summary = await memory_extract_etl_worker.fn(
            user_id=_USER_ID, document_ids=["507f1f77bcf86cd799439011"]
        )

        # The whole graph half is skipped: no resolver, no LLM writes, no edges.
        stubbed_graph_stages["build_resolver"].assert_not_called()
        stubbed_graph_stages["apply_writes"].assert_not_awaited()
        assert summary.edges_written == 0


# ---------------------------------------------------------------------------
# Task ⑧ — _apply_writes bulk_write batching (#057)
# ---------------------------------------------------------------------------


def _make_bulk_write_database() -> Any:
    """Build a database mock whose KG collection records bulk_write/update_one.

    ``bulk_write`` returns a benign result; ``update_one`` is present so a
    regression that re-introduces per-item round-trips is caught (the test
    asserts it is never awaited).
    """

    collection = MagicMock(name="kg_collection")
    collection.bulk_write = AsyncMock(return_value=MagicMock())
    collection.update_one = AsyncMock(return_value=MagicMock())

    database = MagicMock(name="database")
    database.__getitem__.return_value = collection
    return database


def _structural_only_raw(
    *,
    doc_id: str = "507f1f77bcf86cd799439011",
    source_uri: str = "https://example.com/a",
) -> RawExtraction:
    """A RawExtraction whose ONLY output is the structural hierarchy.

    One parent with one child, no LLM-extracted nodes — keeps ``_apply_writes``
    on the structural path (2 ``part_of`` edges) without exercising
    ``add_entity``. Post-ADR-006 the document / chunk ROWS are the rag loader's
    job, so this fixture deliberately carries no structural nodes.
    """

    chunked = ChunkedDocument(
        document_id=doc_id,
        source_uri=source_uri,
        source_type="huggingface",
        parents=[
            ParentChunk(
                index=0,
                content="parent text",
                children=[ChildChunk(index=0, content="child text")],
            )
        ],
    )
    return RawExtraction(
        document_id=doc_id,
        source_uri=source_uri,
        chunked=chunked,
        extracted=ExtractionResult(),
    )


async def _run_apply_writes(
    database: Any,
    raws: list[RawExtraction],
    *,
    extractor: Any = None,
) -> WriteSummary:
    """Invoke the bare ``_apply_writes`` body with stubbed collaborators."""

    from tree.memory.pipeline import _apply_writes

    return await _apply_writes(
        raws=raws,
        resolved=ResolutionOutput(),
        embeddings=EmbeddingMap(),
        dedup_results=DedupMap(),
        database=database,
        resolver=MagicMock(spec=CompositeResolver),
        dedup_config=DeduplicationConfig(),
        embedding_model=MagicMock(spec=BaseEmbeddingModel),
        user_id=_USER_ID,
        extractor=extractor,
    )


class TestApplyWritesBulkBatching:
    """#057 — the structural-node and edge loops each issue ONE bulk_write."""

    async def test_edges_use_a_single_unordered_bulk_write_no_update_one(self) -> None:
        # Arrange — two docs, each contributing 2 part_of edges.
        database = _make_bulk_write_database()
        collection = database[_MEMORY_COLLECTION_SENTINEL]
        raws = [
            _structural_only_raw(
                doc_id="507f1f77bcf86cd799439011",
                source_uri="https://example.com/a",
            ),
            _structural_only_raw(
                doc_id="507f1f77bcf86cd799439012",
                source_uri="https://example.com/b",
            ),
        ]

        summary = await _run_apply_writes(database, raws)

        # Assert — no per-item update_one anywhere in the loop.
        collection.update_one.assert_not_awaited()
        # ONE bulk_write: the structural node rows are the rag loader's, so the
        # only write left in apply-writes is the edge flush.
        assert collection.bulk_write.await_count == 1
        for call in collection.bulk_write.await_args_list:
            assert call.kwargs.get("ordered") is False
        # 2 docs x (child->parent + parent->document).
        assert summary.edges_written == 4
        # apply-writes no longer counts (or writes) the rag rows.
        assert summary.nodes_written == 0

    async def test_node_and_edge_counts_match_golden(self) -> None:
        # Arrange — single doc: 1 parent + 1 child, so 2 PART_OF edges.
        database = _make_bulk_write_database()
        raws = [_structural_only_raw()]

        summary = await _run_apply_writes(database, raws)

        # Assert — golden counts for this fixed input.
        assert summary.nodes_written == 0
        assert summary.edges_written == 2

    async def test_structural_edge_endpoints_are_the_loader_row_ids(self) -> None:
        # The endpoints must be the ids the rag loader actually wrote — NOT the
        # ``_normalize``d fallback, which lowercases and would orphan the edge.
        database = _make_bulk_write_database()
        collection = database[_MEMORY_COLLECTION_SENTINEL]
        raw = _structural_only_raw(source_uri="https://example.com/Mixed-Case")

        await _run_apply_writes(database, [raw])

        ops = collection.bulk_write.await_args.args[0]
        edge_ids = {op._filter["_id"] for op in ops}
        uri = "https://example.com/Mixed-Case"
        assert edge_ids == {
            build_edge_id(
                child_row_id(_USER_ID, uri, 0, 0),
                EdgeType.PART_OF,
                parent_row_id(_USER_ID, uri, 0),
            ),
            build_edge_id(
                parent_row_id(_USER_ID, uri, 0),
                EdgeType.PART_OF,
                document_row_id(_USER_ID, uri),
            ),
        }

    async def test_empty_input_issues_no_bulk_write(self) -> None:
        # Arrange — no documents → no nodes, no edges.
        database = _make_bulk_write_database()
        collection = database[_MEMORY_COLLECTION_SENTINEL]

        summary = await _run_apply_writes(database, [])

        # Assert — pymongo errors on an empty bulk_write, so we must skip it.
        collection.bulk_write.assert_not_awaited()
        collection.update_one.assert_not_awaited()
        assert summary.nodes_written == 0
        assert summary.edges_written == 0

    async def test_extractor_stamped_only_on_related_to_edges(self, mocker) -> None:
        # Arrange — capture the ops the edge bulk_write receives.
        from tree.entities.extraction_audit import ExtractorInfo

        extractor = ExtractorInfo(name="gemini", version="2.5")

        # A doc whose hierarchy contributes a structural PART_OF edge, plus an
        # LLM related_to edge between two PERSON nodes that resolve via
        # name_to_target_id.
        alice = ExtractedNode(name="Alice", type=NodeType.PERSON)
        bob = ExtractedNode(name="Bob", type=NodeType.PERSON)
        related = ExtractedEdge(
            source_node_id="Alice",
            source_type=NodeType.PERSON,
            target_node_id="Bob",
            target_type=NodeType.PERSON,
            type=EdgeType.RELATED_TO,
            semantic_type="knows",
        )
        chunked = ChunkedDocument(
            document_id="507f1f77bcf86cd799439011",
            source_uri="https://example.com/a",
            source_type="huggingface",
            parents=[ParentChunk(index=0, content="parent text")],
        )
        raw = RawExtraction(
            document_id="507f1f77bcf86cd799439011",
            source_uri="https://example.com/a",
            chunked=chunked,
            extracted=ExtractionResult(nodes=[alice, bob], edges=[related]),
        )

        database = _make_bulk_write_database()
        collection = database[_MEMORY_COLLECTION_SENTINEL]

        # Stub the LLM-entity write so Alice/Bob get registered in
        # name_to_target_id (the remap source for the related_to edge).
        async def _dispatch(**kwargs: Any) -> str:
            node = kwargs["node"]
            return build_node_id(_USER_ID, node.type, node.name)

        mocker.patch(
            "tree.memory.pipeline._dispatch_entity_write",
            side_effect=_dispatch,
        )

        await _run_apply_writes(database, [raw], extractor=extractor)

        # Assert — locate the edge bulk_write (the second call) and inspect ops.
        edge_call = collection.bulk_write.await_args_list[-1]
        ops = edge_call.args[0]
        stamped = {}
        for op in ops:
            # pymongo UpdateOne exposes the match under ._filter and the
            # aggregation-pipeline update under ._doc.
            filter_id = op._filter["_id"]
            set_stage = op._doc[0]["$set"]
            stamped[filter_id] = "extractor" in set_stage

        related_id = build_edge_id(
            build_node_id(_USER_ID, NodeType.PERSON, "Alice"),
            EdgeType.RELATED_TO,
            build_node_id(_USER_ID, NodeType.PERSON, "Bob"),
        )
        # related_to carries extractor; every structural edge does not.
        assert stamped[related_id] is True
        assert any(v is False for k, v in stamped.items() if k != related_id)

    async def test_name_to_target_id_resolves_mentions_edges(self, mocker) -> None:
        # Arrange — a doc with a PERSON node produces a MENTIONS edge whose
        # document endpoint must resolve through the loader's row-id map.
        person = ExtractedNode(name="Alice", type=NodeType.PERSON)
        chunked = ChunkedDocument(
            document_id="507f1f77bcf86cd799439011",
            source_uri="https://example.com/a",
            source_type="huggingface",
            parents=[],
        )
        raw = RawExtraction(
            document_id="507f1f77bcf86cd799439011",
            source_uri="https://example.com/a",
            chunked=chunked,
            extracted=ExtractionResult(nodes=[person], edges=[]),
        )

        database = _make_bulk_write_database()
        collection = database[_MEMORY_COLLECTION_SENTINEL]

        # Stub the LLM-entity write so the PERSON registers a target id.
        async def _dispatch(**kwargs: Any) -> str:
            node = kwargs["node"]
            return build_node_id(_USER_ID, node.type, node.name)

        mocker.patch(
            "tree.memory.pipeline._dispatch_entity_write",
            side_effect=_dispatch,
        )

        summary = await _run_apply_writes(database, [raw])

        # Assert — the MENTIONS edge (document -> person) was emitted, which
        # only happens when name_to_target_id resolved both endpoints.
        edge_call = collection.bulk_write.await_args_list[-1]
        ops = edge_call.args[0]
        edge_ids = [op._filter["_id"] for op in ops]
        mentions_id = build_edge_id(
            build_node_id(_USER_ID, NodeType.DOCUMENT, "https://example.com/a"),
            "mentions",
            build_node_id(_USER_ID, NodeType.PERSON, "Alice"),
        )
        assert mentions_id in edge_ids
        assert summary.edges_written == len(edge_ids)


# ---------------------------------------------------------------------------
# Observability — cache-policy exclusion of opik_trace_headers
# ---------------------------------------------------------------------------


class TestTraceHeadersCacheExclusion:
    """The ``opik_trace_headers`` parameter MUST be excluded from the cache key
    of every cached task — otherwise a new run's headers bust the cache every
    time, defeating the INPUTS cache on the expensive chunk / embed / LLM tasks
    (the CRITICAL Prefect-caching concern in the monitoring fix)."""

    @pytest.mark.parametrize(
        "cached_task",
        [
            clean_and_chunk_task,
            embed_children_task,
            llm_extract_entities_task,
            embed_entities_task,
        ],
    )
    def test_cache_policy_excludes_trace_headers(self, cached_task) -> None:
        # The task's cache policy is ``INPUTS - "opik_trace_headers"`` — i.e. an
        # ``Inputs`` policy whose exclude list contains the headers param.
        policy = cached_task.cache_policy
        assert "opik_trace_headers" in (policy.exclude or [])

    def test_task_fn_accepts_trace_headers_kwarg(self) -> None:
        # Every instrumented task body accepts the optional headers kwarg so the
        # flow can thread it through without a Prefect parameter error.
        import inspect

        for fn in (
            _clean_and_chunk,
            _embed_children,
            _load_rag_rows,
            _llm_extract_entities,
            _embed_entities,
        ):
            params = inspect.signature(fn).parameters
            assert "opik_trace_headers" in params
            assert params["opik_trace_headers"].default is None


# ---------------------------------------------------------------------------
# Worker body — the ONE mode switch (ADR-006 decisions 3, 5 and 8)
# ---------------------------------------------------------------------------
#
# These drive the whole worker body against the real ``unit_tests_twin``
# database: the claims under test ("1 document + 2 parent + 6 child rows, ZERO
# edges", "a re-run does not grow the collection") are about what LANDED in
# Mongo, and a bulk_write mock cannot answer them.

# Exactly 48 cl100k_base tokens: with the fixed-token strategy below it splits
# into 2 parents of 24 tokens, each into 3 children of 8 — the shape the
# acceptance criteria count rows against.
_ARTICLE = " ".join(f"word{i}" for i in range(24))
_EXPECTED_PARENTS = 2
_EXPECTED_CHILDREN_PER_PARENT = 3
_EMBEDDING_DIMENSIONS = 4


class _RecordingLLM(BaseLLM):
    """Records every prompt and returns one canned ``person`` emission."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate_json(
        self, prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        return {
            "nodes": [{"name": "alice", "type": "person", "properties": {}}],
            "edges": [],
        }


class _ClientToTestDatabase:
    """Stand-in for the Mongo client: every database name maps to the test DB.

    The worker asks for ``settings.mongo.mongo_initdb_database``; unit tests must
    write to ``unit_tests_twin`` instead of the operator's live database.
    """

    def __init__(self, database: Any) -> None:
        self._database = database

    def __getitem__(self, _name: str) -> Any:
        return self._database


@pytest.fixture
async def test_database() -> Any:
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(), TEST_DATABASE
    )
    database = client[TEST_DATABASE]
    yield database
    await database[MEMORY_COLLECTION].delete_many({})


@pytest.fixture
async def ingested_document(test_database) -> Any:
    """A real user + a real 48-token document, on a clean ``memory`` collection.

    Inserting a ``User`` writes its ``person:self`` node, so the collection is
    cleared AFTER the user exists — otherwise every row count below would be
    off by one.
    """

    user = User(identifier=f"pipeline-test-{PydanticObjectId()}")
    await user.insert()
    document = Document(
        source_type=SourceType.WEB,
        source_uri="https://example.com/rag-vs-graphrag",
        user_id=user.id,
        # Unique per test: the title is part of every child's Contextual header,
        # so it is also part of task ②'s INPUTS cache key. A shared title would
        # let one test's embed result be served from Prefect's cache to the
        # next, and the spy model would record nothing.
        title=f"Memory for AI Agents {PydanticObjectId()}",
        content=_ARTICLE,
    )
    await document.insert()
    await test_database[MEMORY_COLLECTION].delete_many({})

    yield user, document

    await document.delete()
    await user.delete()


@pytest.fixture
def fixed_two_by_three_chunking(monkeypatch) -> None:
    """Pin the splitter so ``_ARTICLE`` yields exactly 2 parents x 3 children."""

    monkeypatch.setenv("TREE_MEMORY__CHUNKING__STRATEGY", "fixed_tokens")
    monkeypatch.setenv("TREE_MEMORY__CHUNKING__PARENT__SIZE", "24")
    monkeypatch.setenv("TREE_MEMORY__CHUNKING__PARENT__OVERLAP", "0")
    monkeypatch.setenv("TREE_MEMORY__CHUNKING__CHILD__SIZE", "8")
    monkeypatch.setenv("TREE_MEMORY__CHUNKING__CHILD__OVERLAP", "0")


async def _run_worker(
    mocker,
    monkeypatch,
    *,
    mode: str,
    test_database: Any,
    user: User,
    document: Document,
    llm: BaseLLM | None = None,
    embedding_model: BaseEmbeddingModel | None = None,
) -> WriteSummary:
    """Run the worker body in ``mode`` against the real test database."""

    monkeypatch.setenv("TREE_MEMORY__MODE", mode)
    # Dedup would issue a ``$vectorSearch`` against an index the test database
    # does not carry; it has its own test module.
    monkeypatch.setenv("TREE_EXTRACTION__DEDUP__ENABLED", "false")
    mocker.patch(
        "tree.memory.pipeline.init_mongodb",
        new=AsyncMock(return_value=_ClientToTestDatabase(test_database)),
    )
    mocker.patch(
        "tree.memory.pipeline.get_search_embedding_model",
        return_value=embedding_model or FakeEmbeddingModel(_EMBEDDING_DIMENSIONS),
    )
    mocker.patch(
        "tree.memory.pipeline.get_resolution_embedding_model",
        return_value=MockEmbeddingModel(_EMBEDDING_DIMENSIONS),
    )
    mocker.patch("tree.memory.pipeline.get_llm", return_value=llm or _RecordingLLM())
    # Supersession + the deterministic ``has`` edge have their own test modules;
    # stubbing them keeps the LLM call count equal to the extraction calls.
    mocker.patch("tree.memory.pipeline.resolve_supersessions", new=AsyncMock())
    mocker.patch(
        "tree.memory.pipeline.write_self_has_preference_edges", new=AsyncMock()
    )

    return await _run_extraction_worker_body(
        user_id=user.id,
        document_ids=[str(document.id)],
        log=logging.getLogger("test.worker"),
    )


@pytest.mark.usefixtures("fixed_two_by_three_chunking")
class TestWorkerRagMode:
    """``rag``: node rows only, no edges, no LLM (the Chapter-4 system)."""

    async def test_writes_one_document_two_parent_and_six_child_rows(
        self, mocker, monkeypatch, test_database, ingested_document
    ) -> None:
        user, document = ingested_document
        collection = test_database[MEMORY_COLLECTION]

        summary = await _run_worker(
            mocker,
            monkeypatch,
            mode="rag",
            test_database=test_database,
            user=user,
            document=document,
        )

        assert await collection.count_documents({"type": "document"}) == 1
        assert await collection.count_documents({"subtype": "parent"}) == 2
        assert await collection.count_documents({"subtype": "child"}) == 6
        assert await collection.count_documents({}) == 9
        assert summary.nodes_written == 9
        assert summary.documents_processed == 1

    async def test_writes_no_edges_and_no_non_rag_node_types(
        self, mocker, monkeypatch, test_database, ingested_document
    ) -> None:
        user, document = ingested_document
        collection = test_database[MEMORY_COLLECTION]

        summary = await _run_worker(
            mocker,
            monkeypatch,
            mode="rag",
            test_database=test_database,
            user=user,
            document=document,
        )

        assert await collection.count_documents({"kind": "edge"}) == 0
        assert summary.edges_written == 0
        written_types = await collection.distinct("type")
        assert set(written_types) <= RAG_NODE_TYPES

    async def test_never_calls_the_llm(
        self, mocker, monkeypatch, test_database, ingested_document
    ) -> None:
        user, document = ingested_document
        llm = _RecordingLLM()

        await _run_worker(
            mocker,
            monkeypatch,
            mode="rag",
            test_database=test_database,
            user=user,
            document=document,
            llm=llm,
        )

        assert llm.prompts == []

    async def test_rerunning_the_same_document_is_idempotent(
        self, mocker, monkeypatch, test_database, ingested_document
    ) -> None:
        user, document = ingested_document
        collection = test_database[MEMORY_COLLECTION]

        await _run_worker(
            mocker,
            monkeypatch,
            mode="rag",
            test_database=test_database,
            user=user,
            document=document,
        )
        first_count = await collection.count_documents({})
        first_row = await collection.find_one(
            {"_id": document_row_id(user.id, document.source_uri)}
        )

        await _run_worker(
            mocker,
            monkeypatch,
            mode="rag",
            test_database=test_database,
            user=user,
            document=document,
        )

        assert await collection.count_documents({}) == first_count
        second_row = await collection.find_one(
            {"_id": document_row_id(user.id, document.source_uri)}
        )
        # Same row, refreshed: created_at is preserved, updated_at moves.
        assert second_row["created_at"] == first_row["created_at"]
        assert second_row["updated_at"] >= first_row["updated_at"]


@pytest.mark.usefixtures("fixed_two_by_three_chunking")
class TestWorkerRowShape:
    """The rows themselves — identical in BOTH modes (ADR-006 decision 3)."""

    @pytest.mark.parametrize("mode", ["rag", "graphrag"])
    async def test_only_children_carry_a_vector(
        self, mocker, monkeypatch, test_database, ingested_document, mode: str
    ) -> None:
        user, document = ingested_document
        collection = test_database[MEMORY_COLLECTION]

        await _run_worker(
            mocker,
            monkeypatch,
            mode=mode,
            test_database=test_database,
            user=user,
            document=document,
        )

        async for row in collection.find({"subtype": "child"}):
            assert len(row["embedding"]) == _EMBEDDING_DIMENSIONS
        async for row in collection.find(
            {"$or": [{"subtype": "parent"}, {"type": "document"}]}
        ):
            assert row["embedding"] == []

    @pytest.mark.parametrize("mode", ["rag", "graphrag"])
    async def test_hierarchy_columns_and_denormalised_properties(
        self, mocker, monkeypatch, test_database, ingested_document, mode: str
    ) -> None:
        user, document = ingested_document
        collection = test_database[MEMORY_COLLECTION]
        uri = document.source_uri

        await _run_worker(
            mocker,
            monkeypatch,
            mode=mode,
            test_database=test_database,
            user=user,
            document=document,
        )

        parent = await collection.find_one({"_id": parent_row_id(user.id, uri, 1)})
        assert parent["subtype"] == "parent"
        assert parent["parent_id"] == document_row_id(user.id, uri)
        assert parent["chunk_index"] == 1
        assert parent["name"] == parent_chunk_name(uri, 1)

        child = await collection.find_one({"_id": child_row_id(user.id, uri, 1, 2)})
        assert child["subtype"] == "child"
        assert child["parent_id"] == parent_row_id(user.id, uri, 1)
        assert child["chunk_index"] == 2
        assert child["name"] == child_chunk_name(uri, 1, 2)
        # Denormalised so the indexing backfill needs no join.
        assert child["properties"]["title"] == document.title
        assert (
            child["properties"]["heading_path"] == parent["properties"]["heading_path"]
        )
        assert child["properties"]["content"] in parent["properties"]["content"]

    async def test_children_are_embedded_on_their_contextual_header_text(
        self, mocker, monkeypatch, test_database, ingested_document
    ) -> None:
        user, document = ingested_document
        collection = test_database[MEMORY_COLLECTION]
        spy = _SpyEmbeddingModel(dimensions=_EMBEDDING_DIMENSIONS)

        await _run_worker(
            mocker,
            monkeypatch,
            mode="rag",
            test_database=test_database,
            user=user,
            document=document,
            embedding_model=spy,
        )

        expected = []
        async for row in collection.find({"subtype": "child"}):
            expected.append(
                child_embedding_text(
                    title=row["properties"]["title"],
                    heading_path=row["properties"]["heading_path"],
                    content=row["properties"]["content"],
                )
            )
        # The embedded text is the contextual header, NEVER the raw content.
        assert sorted(spy.texts) == sorted(expected)
        raw_contents = [
            row["properties"]["content"]
            async for row in collection.find({"subtype": "child"})
        ]
        assert set(spy.texts).isdisjoint(raw_contents)


@pytest.mark.usefixtures("fixed_two_by_three_chunking")
class TestWorkerGraphragMode:
    """``graphrag``: the same rows PLUS structural edges and LLM extraction."""

    async def test_writes_part_of_and_next_edges_at_both_levels(
        self, mocker, monkeypatch, test_database, ingested_document
    ) -> None:
        user, document = ingested_document
        collection = test_database[MEMORY_COLLECTION]

        summary = await _run_worker(
            mocker,
            monkeypatch,
            mode="graphrag",
            test_database=test_database,
            user=user,
            document=document,
        )

        # 6 child->parent + 2 parent->document.
        assert await collection.count_documents({"type": "part_of"}) == 8
        # 2 x (3 children - 1) sibling hops + 1 parent-level hop.
        assert await collection.count_documents({"type": "next"}) == 5
        assert summary.edges_written >= 13

    async def test_calls_the_llm_once_per_parent_with_the_parent_content(
        self, mocker, monkeypatch, test_database, ingested_document
    ) -> None:
        user, document = ingested_document
        llm = _RecordingLLM()

        await _run_worker(
            mocker,
            monkeypatch,
            mode="graphrag",
            test_database=test_database,
            user=user,
            document=document,
            llm=llm,
        )

        assert len(llm.prompts) == _EXPECTED_PARENTS
        collection = test_database[MEMORY_COLLECTION]
        async for row in collection.find({"subtype": "parent"}):
            assert any(row["properties"]["content"] in p for p in llm.prompts)

    async def test_writes_the_same_rag_rows_as_rag_mode(
        self, mocker, monkeypatch, test_database, ingested_document
    ) -> None:
        user, document = ingested_document
        collection = test_database[MEMORY_COLLECTION]

        await _run_worker(
            mocker,
            monkeypatch,
            mode="graphrag",
            test_database=test_database,
            user=user,
            document=document,
        )

        assert await collection.count_documents({"type": "document"}) == 1
        assert await collection.count_documents({"subtype": "parent"}) == 2
        assert await collection.count_documents({"subtype": "child"}) == 6

    async def test_rerunning_the_same_document_is_idempotent(
        self, mocker, monkeypatch, test_database, ingested_document
    ) -> None:
        user, document = ingested_document
        collection = test_database[MEMORY_COLLECTION]

        await _run_worker(
            mocker,
            monkeypatch,
            mode="graphrag",
            test_database=test_database,
            user=user,
            document=document,
        )
        first_count = await collection.count_documents({})

        await _run_worker(
            mocker,
            monkeypatch,
            mode="graphrag",
            test_database=test_database,
            user=user,
            document=document,
        )

        assert await collection.count_documents({}) == first_count


class TestRagRowIdMap:
    """``_apply_writes`` remaps structural endpoints through this map."""

    def test_maps_every_row_name_to_the_loader_id(self) -> None:
        chunked = _chunked_with_children(shape=(2,))
        uri = chunked.source_uri

        ids = _rag_row_id_map(_USER_ID, chunked)

        assert ids[f"{NodeType.DOCUMENT.value}|{uri}"] == document_row_id(_USER_ID, uri)
        assert ids[
            f"{NodeType.CHUNK.value}|{parent_chunk_name(uri, 0)}"
        ] == parent_row_id(_USER_ID, uri, 0)
        assert ids[
            f"{NodeType.CHUNK.value}|{child_chunk_name(uri, 0, 1)}"
        ] == child_row_id(_USER_ID, uri, 0, 1)
