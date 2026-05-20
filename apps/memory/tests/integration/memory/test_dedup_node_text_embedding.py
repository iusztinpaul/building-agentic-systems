"""Integration tests for #042 — dedup on node-text embedding + vector reuse.

These tests exercise the full extraction flow (``memory_extraction.fn``)
against the local MongoDB and assert the persisted node ``embedding``:

* AC #5 — a newly-created PERSON node's persisted vector equals the
  embedding of its NODE-TEXT (search model), NOT its bare name.
* AC #6 — the indexing backfill (``embed_nodes``) is a no-op for that node
  because the extraction pipeline already wrote its vector.
* AC #7 — a PREFERENCE node still stores the ``properties.statement``
  embedding (#032 regression — supersession stays statement-vs-statement).

The ``dedupe_entity`` call is patched to return ``action="none"`` so the
flow always creates a new node and we observe the persisted vector
directly — these tests do not need a live mongot ``$vectorSearch``. The
auto-merge-in-the-same-space story (which DOES need mongot) lives in
``TestNearDuplicateAutoMergesInSameSpace`` and is marked accordingly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.entities.documents import Document, SourceType
from tree.entities.knowledge_graph import NodeType, build_node_id
from tree.entities.users import User
from tree.memory.embedding_text import node_to_embedding_text
from tree.memory.extraction.dedup import (
    DeduplicationConfig,
    DeduplicationResult,
    dedupe_entity,
)
from tree.memory.extraction.pipeline import memory_extraction
from tree.memory.indexing.core import embed_nodes, ensure_indexes
from tree.models.base import BaseEmbeddingModel
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM

TEST_DATABASE = "integration_tests_twin"
_DIMS = 8


# ---------------------------------------------------------------------------
# Deterministic per-text embedding model
# ---------------------------------------------------------------------------


class _PerTextEmbeddingModel(BaseEmbeddingModel):
    """Deterministic embedding: distinct text → distinct (stable) vector.

    Unlike ``FakeEmbeddingModel`` (which returns zero vectors for every
    input), this lets a test distinguish "embedded the node-text" from
    "embedded the bare name". It also records every text it was asked to
    embed.
    """

    def __init__(self, dimensions: int = _DIMS) -> None:
        self._dimensions = dimensions
        self.embedded_texts: list[str] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def vec(self, text: str) -> list[float]:
        # Stable across processes (unlike builtin ``hash``) so a Prefect
        # ``INPUTS``-cached task ④ result from a previous session still
        # equals a freshly-computed vector for the same text.
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i] / 255.0 for i in range(self._dimensions)]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded_texts.extend(texts)
        return [self.vec(t) for t in texts]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user() -> User:
    user = User(identifier=f"test-user-{PydanticObjectId()}")
    await user.insert()
    return user


async def _insert_doc(
    *, content: str, source_uri: str, user_id: PydanticObjectId
) -> Document:
    doc = Document(
        title="Test Document",
        content=content,
        source_type=SourceType.HUGGINGFACE,
        source_uri=source_uri,
        user_id=user_id,
        authors=["Test Author"],
    )
    await doc.insert()
    return doc


def _patch_pipeline_deps(
    mocker,
    mongo_client,
    *,
    llm: Any,
    embedding_model: BaseEmbeddingModel,
    resolution_embedding_model: BaseEmbeddingModel | None = None,
) -> None:
    mocker.patch(
        "tree.memory.extraction.pipeline.init_mongodb", return_value=mongo_client
    )
    mocker.patch(
        "tree.memory.extraction.pipeline.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )
    mocker.patch("tree.memory.extraction.pipeline.get_llm", return_value=llm)
    # #043: the SEARCH model is the persisted / dedup / supersession handle.
    mocker.patch(
        "tree.memory.extraction.pipeline.get_search_embedding_model",
        return_value=embedding_model,
    )
    # #043: the resolution model feeds the resolver's transient name vector.
    # Default to the same handle as search (the YAML default points both at
    # voyage-multimodal-3) unless a test wants to distinguish the two.
    mocker.patch(
        "tree.memory.extraction.pipeline.get_resolution_embedding_model",
        return_value=resolution_embedding_model or embedding_model,
    )
    # Force a new-node decision so we observe the persisted vector directly
    # (no live $vectorSearch needed).
    mocker.patch(
        "tree.memory.extraction.pipeline.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )
    mocker.patch(
        "tree.memory.extraction.add_entity.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )


@pytest.fixture(autouse=True)
def _refresh_prefect_cache(monkeypatch):
    """Force task-④'s ``INPUTS`` cache to recompute every run.

    These tests use a deterministic per-text embedding model and assert on
    exact vector equality. Prefect's on-disk ``INPUTS`` result cache would
    otherwise return a vector computed by a *previous* session's model
    instance for the same node-text, masking the behavior under test.
    Refreshing the cache makes ``embed_entities_task`` actually call the
    patched model on every run.
    """

    monkeypatch.setenv("PREFECT_TASKS_REFRESH_CACHE", "true")


@pytest.fixture
async def _kg_collection(mongo_client):
    db = mongo_client[TEST_DATABASE]
    yield db["knowledge_graph"]
    await db.drop_collection("knowledge_graph")


# ---------------------------------------------------------------------------
# AC #5 + #6 — persisted vector is node-text, backfill is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestNewNodePersistsNodeTextVector:
    async def test_person_node_persists_node_text_vector_not_name(
        self, mongo_client, _kg_collection, mocker
    ) -> None:
        # Arrange — a unique person name per run so task-④'s on-disk
        # ``INPUTS`` cache never returns a stale vector for this node-text.
        name = f"andrej karpathy {uuid4().hex[:8]}"
        user = await _make_user()
        doc = await _insert_doc(
            content="Andrej Karpathy is a researcher.",
            source_uri="https://example.com/ak",
            user_id=user.id,
        )
        llm = FakeLLM(
            [
                {
                    "nodes": [
                        {
                            "name": name,
                            "type": "person",
                            "subtype": "individual",
                            "properties": {"role": "researcher"},
                        }
                    ],
                    "edges": [],
                }
            ]
        )
        model = _PerTextEmbeddingModel(dimensions=_DIMS)
        _patch_pipeline_deps(mocker, mongo_client, llm=llm, embedding_model=model)

        # Act
        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        # Assert — the PERSON node carries the node-text vector.
        person_id = build_node_id(user.id, NodeType.PERSON, name)
        row = await _kg_collection.find_one({"_id": person_id})
        assert row is not None, "expected the person node to be created"

        # The persisted vector is the embedding of the node's NODE-TEXT
        # (deterministic per text), NOT the embedding of its bare name.
        # ``vec`` is text-deterministic, so vector equality proves WHICH
        # text was embedded.
        node_text = node_to_embedding_text(row)
        expected_vec = model.vec(node_text)
        name_vec = model.vec(name)

        assert node_text == f"person: {name}"
        assert row["embedding"] == expected_vec
        assert row["embedding"] != name_vec

    @pytest.mark.requires_mongot
    @pytest.mark.usefixtures("_skip_without_mongot")
    async def test_indexing_backfill_is_noop_for_dedup_created_node(
        self, mongo_client, _kg_collection, mocker
    ) -> None:
        # Arrange — run extraction so the person node carries its vector.
        name = f"andrej karpathy {uuid4().hex[:8]}"
        user = await _make_user()
        doc = await _insert_doc(
            content="Andrej Karpathy is a researcher.",
            source_uri="https://example.com/ak2",
            user_id=user.id,
        )
        llm = FakeLLM(
            [
                {
                    "nodes": [
                        {
                            "name": name,
                            "type": "person",
                            "subtype": "individual",
                            "properties": {"role": "researcher"},
                        }
                    ],
                    "edges": [],
                }
            ]
        )
        model = _PerTextEmbeddingModel(dimensions=_DIMS)
        _patch_pipeline_deps(mocker, mongo_client, llm=llm, embedding_model=model)
        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        person_id = build_node_id(user.id, NodeType.PERSON, name)
        before = await _kg_collection.find_one({"_id": person_id})
        assert before is not None
        before_vec = before["embedding"]
        assert before_vec, "person node must have a vector after extraction"

        # Ensure indexes exist so embed_nodes can run cleanly.
        await ensure_indexes(
            mongo_client,
            TEST_DATABASE,
            embedding_model=FakeEmbeddingModel(dimensions=_DIMS),
            user_id=user.id,
        )

        # Act — backfill. Count how many nodes it (re-)embeds.
        embedded_count = await embed_nodes(
            mongo_client,
            TEST_DATABASE,
            FakeEmbeddingModel(dimensions=_DIMS),
            user.id,
        )

        # Assert — the dedup-created person node was NOT re-embedded.
        after = await _kg_collection.find_one({"_id": person_id})
        assert after is not None
        assert after["embedding"] == before_vec
        # A FakeEmbeddingModel would zero-out the vector if it ran on this
        # node — it did not, so the vector is unchanged regardless of count.
        # (Structural/empty-vector rows may still be backfilled; the person
        # node specifically must be skipped.)
        person_was_reembedded = after["embedding"] != before_vec
        assert not person_was_reembedded, (
            f"backfill re-embedded the dedup-created node "
            f"(embedded_count={embedded_count})"
        )


# ---------------------------------------------------------------------------
# AC #7 — PREFERENCE still stores the statement embedding (#032 regression)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestPreferenceStillStoresStatementEmbedding:
    async def test_preference_persists_statement_vector(
        self, mongo_client, _kg_collection, mocker
    ) -> None:
        # Arrange
        user = await _make_user()
        doc = await _insert_doc(
            content="The user prefers dark mode.",
            source_uri="https://example.com/pref",
            user_id=user.id,
        )
        # Unique statement per run so task-④'s cache key is fresh.
        statement = f"prefers dark mode in the editor {uuid4().hex[:8]}"
        llm = FakeLLM(
            [
                {
                    "nodes": [
                        {
                            "name": "prefers-dark-mode",
                            "type": "preference",
                            "properties": {
                                "statement": statement,
                                "category": "ui",
                                "strength": "strong",
                            },
                        }
                    ],
                    "edges": [],
                }
            ]
        )
        model = _PerTextEmbeddingModel(dimensions=_DIMS)
        _patch_pipeline_deps(mocker, mongo_client, llm=llm, embedding_model=model)
        # The supersession judge is never asked on a first write (no prior),
        # but stub it defensively so a live LLM is never reached.
        mocker.patch(
            "tree.memory.extraction.preference_supersession.judge_contradiction",
            new=AsyncMock(return_value=(False, 0.0)),
        )

        # Act
        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        # Assert — persisted vector is the STATEMENT embedding, not node-text.
        # The preference ``_id`` is a slug of the (now unique) statement, so
        # look the node up by type rather than reconstructing the slug.
        pref_rows = await _kg_collection.find(
            {
                "user_id": user.id,
                "kind": "node",
                "type": NodeType.PREFERENCE.value,
            }
        ).to_list()
        assert len(pref_rows) == 1, "expected exactly one preference node"
        row = pref_rows[0]

        statement_vec = model.vec(statement)
        node_text_vec = model.vec(node_to_embedding_text(row))
        assert row["embedding"] == statement_vec
        # The slug/node-text vector must NOT have won.
        assert row["embedding"] != node_text_vec


# ---------------------------------------------------------------------------
# #043 — resolution name vector (transient) vs search node-text vector
# (persisted). The two embedding handles are distinct: the resolver embeds
# the NAME with the resolution model and that vector is NEVER persisted, while
# the persisted node ``embedding`` comes from the SEARCH model's node-text.
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestResolutionModelIsNameOnlyAndTransient:
    async def test_persisted_vector_is_search_node_text_not_resolution_name(
        self, mongo_client, _kg_collection, mocker
    ) -> None:
        # Arrange — two DISTINGUISHABLE models. The resolution model records
        # every text it embeds (so we can prove it only ever sees the NAME)
        # and returns a different vector family than the search model.
        name = f"andrej karpathy {uuid4().hex[:8]}"
        user = await _make_user()
        doc = await _insert_doc(
            content="Andrej Karpathy is a researcher.",
            source_uri="https://example.com/ak-043",
            user_id=user.id,
        )
        llm = FakeLLM(
            [
                {
                    "nodes": [
                        {
                            "name": name,
                            "type": "person",
                            "subtype": "individual",
                            "properties": {"role": "researcher"},
                        }
                    ],
                    "edges": [],
                }
            ]
        )

        # The search model: persisted vectors come from here (node-text).
        search_model = _PerTextEmbeddingModel(dimensions=_DIMS)

        # The resolution model: a SEPARATE handle whose vectors are a constant
        # family that the search model never produces, AND which records every
        # text it embeds. If the resolution vector were ever persisted, the
        # assertion below would catch it.
        class _RecordingResolutionModel(BaseEmbeddingModel):
            def __init__(self) -> None:
                self.embedded_texts: list[str] = []

            @property
            def dimensions(self) -> int:
                return _DIMS

            async def embed(self, texts: list[str]) -> list[list[float]]:
                self.embedded_texts.extend(texts)
                # A sentinel vector the search model never returns.
                return [[9.0] * _DIMS for _ in texts]

        resolution_model = _RecordingResolutionModel()
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=llm,
            embedding_model=search_model,
            resolution_embedding_model=resolution_model,
        )

        # Act
        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        # Assert — the persisted vector is the SEARCH model's node-text vector.
        person_id = build_node_id(user.id, NodeType.PERSON, name)
        row = await _kg_collection.find_one({"_id": person_id})
        assert row is not None, "expected the person node to be created"

        node_text = node_to_embedding_text(row)
        assert row["embedding"] == search_model.vec(node_text)
        # The resolution model's sentinel vector was NEVER persisted.
        assert row["embedding"] != [9.0] * _DIMS

        # And the resolution model embedded the NAME (the light op), not the
        # node-text. (There are existing same-type candidates only if the
        # graph is non-empty; ``person:self`` shares the type, so the resolver
        # runs and embeds the incoming name + any candidate names — never the
        # multi-line node-text the search model builds.)
        assert any(name in t for t in resolution_model.embedded_texts), (
            f"resolution model never embedded the entity name; "
            f"saw {resolution_model.embedded_texts!r}"
        )
        assert all("\n" not in t for t in resolution_model.embedded_texts), (
            "resolution model embedded multi-line node-text, expected NAME only"
        )


# ---------------------------------------------------------------------------
# Auto-merge story — dedup decision in the SAME space as persisted vectors
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.requires_mongot
@pytest.mark.usefixtures("_skip_without_mongot")
class TestNearDuplicateAutoMergesInSameSpace:
    async def test_two_runs_merge_into_one_node(
        self, mongo_client, _kg_collection, mocker
    ) -> None:
        """Seed a person via extraction (its node-text vector is persisted),
        then a second near-identical mention runs the REAL dedup against the
        live ``vector_index`` — same space → auto-merge into the existing
        node instead of a duplicate.
        """

        user = await _make_user()

        # A constant-vector model: every text maps to the same unit vector,
        # so the second mention's query vector clears auto_merge_threshold
        # against the first node's persisted vector (cos == 1.0).
        class _ConstantModel(BaseEmbeddingModel):
            @property
            def dimensions(self) -> int:
                return _DIMS

            async def embed(self, texts: list[str]) -> list[list[float]]:
                vec = [1.0] + [0.0] * (_DIMS - 1)
                return [list(vec) for _ in texts]

        model = _ConstantModel()

        # First run — real dedup, empty graph → new node, vector persisted.
        mocker.patch(
            "tree.memory.extraction.pipeline.init_mongodb", return_value=mongo_client
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.settings.mongo.mongo_initdb_database",
            TEST_DATABASE,
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.get_search_embedding_model",
            return_value=model,
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.get_resolution_embedding_model",
            return_value=model,
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.get_llm",
            return_value=FakeLLM(
                [
                    {
                        "nodes": [
                            {
                                "name": "andrej karpathy",
                                "type": "person",
                                "subtype": "individual",
                                "properties": {"role": "researcher"},
                            }
                        ],
                        "edges": [],
                    }
                ]
            ),
        )

        # Ensure the vector index exists at the right dim before any run.
        await ensure_indexes(
            mongo_client,
            TEST_DATABASE,
            embedding_model=model,
            user_id=user.id,
        )

        async def _non_self_persons() -> list[dict[str, Any]]:
            rows = await _kg_collection.find(
                {"user_id": user.id, "kind": "node", "type": NodeType.PERSON.value}
            ).to_list()
            # ``person:self`` is created by the User hook with an empty
            # embedding (it is never a $vectorSearch candidate); exclude it.
            return [r for r in rows if not str(r["_id"]).endswith(":person:self")]

        doc1 = await _insert_doc(
            content="Andrej Karpathy is a researcher.",
            source_uri="https://example.com/run1",
            user_id=user.id,
        )
        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc1.id)])

        assert len(await _non_self_persons()) == 1

        # Wait for the index to pick up the seeded vector (self has no
        # embedding, so we expect exactly 1 indexed node).
        await _wait_for_indexed_count(_kg_collection, user.id, 1)

        # Second run — a near-duplicate mention. REAL dedup runs against the
        # live index in the SAME space and auto-merges.
        mocker.patch(
            "tree.memory.extraction.pipeline.get_llm",
            return_value=FakeLLM(
                [
                    {
                        "nodes": [
                            {
                                "name": "andrej karpathy",
                                "type": "person",
                                "subtype": "individual",
                                "properties": {"role": "ai researcher"},
                            }
                        ],
                        "edges": [],
                    }
                ]
            ),
        )
        doc2 = await _insert_doc(
            content="Andrej Karpathy works on AI.",
            source_uri="https://example.com/run2",
            user_id=user.id,
        )
        with prefect_tags("tests"):
            summary = await memory_extraction(
                user_id=user.id, document_ids=[str(doc2.id)]
            )

        # Assert — no duplicate person node was created; the mention merged.
        person_rows_after = await _non_self_persons()
        assert len(person_rows_after) == 1, (
            "near-duplicate should auto-merge in the shared node-text space, "
            f"got {len(person_rows_after)} non-self person rows; summary={summary}"
        )


# ---------------------------------------------------------------------------
# Disambiguation — same surface name, different node-text content → NO merge
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.requires_mongot
@pytest.mark.usefixtures("_skip_without_mongot")
class TestSameNameDifferentContentDoesNotMerge:
    async def test_same_name_different_content_does_not_auto_merge(
        self, mongo_client, _kg_collection, mocker
    ) -> None:
        """The headline #042 guarantee: node-text embeddings DISAMBIGUATE.

        ``TestNearDuplicateAutoMergesInSameSpace`` (constant-vector model)
        only proves a merge *fires*. This is the negative companion: two
        PERSON entities share an IDENTICAL surface name but carry materially
        different content (``role``/``bio`` properties), so their NODE-TEXTs —
        and thus their search-model vectors — are far apart.

        * **Pre-#042 (bare-NAME embedding):** the query vector would be the
          embedding of ``"john smith"`` against a stored ``"john smith"``
          vector → raw cosine 1.0 → ``action="merged"``. The two distinct
          people would have been spuriously collapsed.
        * **Post-#042 (NODE-TEXT embedding):** the engineer's stored vector
          and the saxophonist's query vector are the embeddings of their
          (distinct) node-texts → raw cosine ~0.66. Even with the production
          fuzzy boost firing on the identical names (fuzzy = 1.0, combined =
          (0.66 + 1.0)/2 ≈ 0.83), the score stays below the 0.95
          ``auto_merge_threshold`` → ``action != "merged"``.

        Real ``$vectorSearch`` runs against the live ``vector_index`` with the
        deterministic sha256-seeded embedder, so this is ``requires_mongot``.
        """

        user = await _make_user()
        model = _PerTextEmbeddingModel(dimensions=_DIMS)

        # The two same-name PERSONs differ only in their content. Their
        # node-texts mirror what ``node_to_embedding_text`` would emit for the
        # persisted node shape so the seeded vector matches the real corpus.
        name = "john smith"
        engineer_node_text = (
            "person: john smith\n"
            "role: software engineer at acme\n"
            "bio: builds distributed databases in rust"
        )
        saxophonist_node_text = (
            "person: john smith\n"
            "role: jazz saxophonist\n"
            "bio: toured europe performing bebop standards"
        )

        # Sanity: the two node-texts genuinely land far apart, while a
        # bare-name embedding would have been identical (cosine 1.0).
        assert model.vec(engineer_node_text) != model.vec(saxophonist_node_text)
        assert model.vec(name) == model.vec(name)

        # Build the vector index at the right dim, then seed the existing
        # engineer node carrying its NODE-TEXT vector (the post-#042 corpus
        # shape — what extraction would have persisted).
        await ensure_indexes(
            mongo_client,
            TEST_DATABASE,
            embedding_model=model,
            user_id=user.id,
        )
        engineer_id = build_node_id(user.id, NodeType.PERSON, name)
        await _kg_collection.insert_one(
            {
                "_id": engineer_id,
                "user_id": user.id,
                "kind": "node",
                "type": NodeType.PERSON.value,
                "name": name,
                "canonical_name": name,
                "properties": {"aliases": []},
                "embedding": model.vec(engineer_node_text),
                "sources": [],
                "merged_into": None,
            }
        )
        await _wait_for_indexed_count(_kg_collection, user.id, 1)

        # Act — the saxophonist mention arrives. Dedup embeds its NODE-TEXT
        # (the production grain) and runs the REAL $vectorSearch. Production
        # config (fuzzy ON) is used so the identical-name boost is exercised.
        config = DeduplicationConfig()
        result = await dedupe_entity(
            database=mongo_client[TEST_DATABASE],
            user_id=user.id,
            name=name,
            entity_type=NodeType.PERSON,
            embedding=model.vec(saxophonist_node_text),
            config=config,
        )

        # Assert — node-text content kept the two same-name people apart:
        # the decision is NOT an auto-merge (a bare-name embedding would have
        # merged at cosine 1.0).
        assert result.action != "merged", (
            "same-name/different-content PERSONs must NOT auto-merge in "
            "node-text space (a bare-name embedding would have); "
            f"got action={result.action!r} score={result.similarity_score!r}"
        )
        assert result.similarity_score < config.auto_merge_threshold


async def _wait_for_indexed_count(
    collection, user_id: PydanticObjectId, expected: int, timeout: float = 30.0
) -> None:
    """Poll ``$vectorSearch`` until at least ``expected`` nodes are returned."""

    import asyncio

    probe = [1.0] + [0.0] * (_DIMS - 1)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        cursor = await collection.aggregate(
            [
                {
                    "$vectorSearch": {
                        "index": "vector_index",
                        "path": "embedding",
                        "queryVector": probe,
                        "numCandidates": 100,
                        "limit": 50,
                        "filter": {"user_id": user_id, "kind": "node"},
                    }
                },
                {"$count": "n"},
            ]
        )
        rows = [r async for r in cursor]
        if rows and rows[0].get("n", 0) >= expected:
            return
        await asyncio.sleep(1.0)
    raise RuntimeError(
        f"vector_index did not return {expected} nodes within {timeout}s"
    )
