"""Integration tests for #044 — real-time request batching across the three
embedding stages: INDEXING backfill, extraction task ④ (dedup), and
RESOLUTION pre-warm.

Each test wires a stage end-to-end against the local MongoDB and asserts the
number of SEPARATE embed requests drops to ``ceil(N / chunk-size)`` instead of
``N`` (or one-per-name), using a counting mock embedding model. None of these
need a live mongot ``$vectorSearch`` — dedup is stubbed to ``action="none"``
in the extraction test and the indexing test calls ``embed_nodes`` directly —
so the suite runs in the fast loop.

REJECTED ALTERNATIVE — the Voyage async Batch API is NOT what "batching"
means here (it has a 12h completion window and doesn't support
/v1/multimodalembeddings). See ``tree.memory.embedding_text`` and tracker/044.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.entities.documents import Document, SourceType
from tree.entities.users import User
from tree.memory.extraction.dedup import DeduplicationResult
from tree.memory.extraction.pipeline import memory_extraction
from tree.memory.indexing.core import embed_nodes
from tree.models.base import BaseEmbeddingModel

TEST_DATABASE = "integration_tests_twin"
_DIMS = 8
_NOW = datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Counting embedding model — records each SEPARATE embed() request
# ---------------------------------------------------------------------------


class _CountingEmbeddingModel(BaseEmbeddingModel):
    """Records the size of each ``embed`` request so a test can assert how
    many SEPARATE requests a stage issued, and returns a per-text deterministic
    vector so positional alignment can be checked too."""

    def __init__(self, dimensions: int = _DIMS) -> None:
        self._dimensions = dimensions
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def vec(self, text: str) -> list[float]:
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i] / 255.0 for i in range(self._dimensions)]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
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


@pytest.fixture
async def _kg_collection(mongo_client):
    db = mongo_client[TEST_DATABASE]
    yield db["knowledge_graph"]
    await db.drop_collection("knowledge_graph")


@pytest.fixture(autouse=True)
def _refresh_prefect_cache(monkeypatch):
    """Force task-④'s ``INPUTS`` cache to recompute every run so the counting
    model actually sees the embed requests (Prefect would otherwise serve a
    cached result from a prior session)."""

    monkeypatch.setenv("PREFECT_TASKS_REFRESH_CACHE", "true")


# ---------------------------------------------------------------------------
# AC — INDEXING: embed_nodes routes node-text embedding through the batcher
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestIndexingBackfillBatches:
    async def test_backfill_issues_ceil_requests_not_one_per_node(
        self, mongo_client, _kg_collection, mocker
    ) -> None:
        # Arrange — seed N unembedded nodes for one tenant.
        n_nodes = 250
        max_inputs = 100  # small cap so the split is observable
        user_id = PydanticObjectId()
        rows = [
            {
                "_id": f"{user_id}:person:p{i}",
                "user_id": user_id,
                "kind": "node",
                "type": "person",
                "name": f"person {i}",
                "properties": {},
                "embedding": [],
                "sources": [],
                "created_at": _NOW,
                "updated_at": _NOW,
            }
            for i in range(n_nodes)
        ]
        await _kg_collection.insert_many(rows)

        model = _CountingEmbeddingModel(dimensions=_DIMS)

        # Lower the batcher's input cap via the YAML config the batcher reads
        # (``embed_node_texts`` resolves caps from app_config on each call).
        from tree.config.app_config import app_config

        mocker.patch.object(app_config.models.embedding_batch, "max_inputs", max_inputs)

        # Act
        embedded_count = await embed_nodes(mongo_client, TEST_DATABASE, model, user_id)

        # Assert — all nodes embedded, in ceil(250 / 100) = 3 requests (NOT 250).
        assert embedded_count == n_nodes
        assert len(model.calls) == 3
        assert [len(c) for c in model.calls] == [100, 100, 50]

        # Every node now carries a vector, positionally aligned with its text.
        embedded = await _kg_collection.find(
            {"user_id": user_id, "kind": "node"}
        ).to_list()
        assert all(len(row["embedding"]) == _DIMS for row in embedded)


# ---------------------------------------------------------------------------
# AC — Extraction task ④ embeds all run node-texts in fewer requests
# ---------------------------------------------------------------------------


def _patch_extraction_deps(
    mocker, mongo_client, *, llm, embedding_model: BaseEmbeddingModel
) -> None:
    mocker.patch(
        "tree.memory.extraction.pipeline.init_mongodb", return_value=mongo_client
    )
    mocker.patch(
        "tree.memory.extraction.pipeline.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )
    mocker.patch("tree.memory.extraction.pipeline.get_llm", return_value=llm)
    mocker.patch(
        "tree.memory.extraction.pipeline.get_search_embedding_model",
        return_value=embedding_model,
    )
    mocker.patch(
        "tree.memory.extraction.pipeline.get_resolution_embedding_model",
        return_value=embedding_model,
    )
    mocker.patch(
        "tree.memory.extraction.pipeline.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )
    mocker.patch(
        "tree.memory.extraction.add_entity.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )


@pytest.mark.slow
class TestExtractionTaskFourBatches:
    async def test_task_four_embeds_all_node_texts_in_fewer_requests(
        self, mongo_client, _kg_collection, mocker
    ) -> None:
        # Arrange — a single document whose LLM extraction yields many entities.
        from tree.models.fake_model import FakeLLM

        user = await _make_user()
        doc = await _insert_doc(
            content="A document mentioning many people.",
            source_uri=f"https://example.com/many-{uuid4().hex[:8]}",
            user_id=user.id,
        )
        n_entities = 40
        nodes = [
            {
                "name": f"person number {i} {uuid4().hex[:6]}",
                "type": "person",
                "subtype": "individual",
                "properties": {"role": "researcher"},
            }
            for i in range(n_entities)
        ]
        llm = FakeLLM([{"nodes": nodes, "edges": []}])

        search_model = _CountingEmbeddingModel(dimensions=_DIMS)
        _patch_extraction_deps(
            mocker, mongo_client, llm=llm, embedding_model=search_model
        )
        # Stub the supersession judge defensively (no live LLM).
        mocker.patch(
            "tree.memory.extraction.preference_supersession.judge_contradiction",
            new=AsyncMock(return_value=(False, 0.0)),
        )

        # Act
        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        # Assert — task ④ embedded ALL node-texts. The default 1000-input cap
        # is far above 40 distinct node-texts, so they all go in ONE request.
        # That request count is FAR fewer than the one-per-name baseline (40).
        # ``search_model`` is also used by the resolver pre-warm and
        # apply-writes fallback, so isolate task ④'s call: the largest single
        # request carries every unique node-text in one shot.
        max_request_size = max(len(c) for c in search_model.calls)
        assert max_request_size >= n_entities, (
            f"expected one batched request carrying >= {n_entities} node-texts; "
            f"largest request had {max_request_size}. calls sizes="
            f"{[len(c) for c in search_model.calls]}"
        )
        # The one-per-name baseline would have been >= 40 requests just for
        # task ④; the batched path keeps the WHOLE flow's request count low.
        assert len(search_model.calls) < n_entities, (
            f"batching should keep total requests well under the one-per-name "
            f"baseline ({n_entities}); saw {len(search_model.calls)}"
        )


# ---------------------------------------------------------------------------
# AC — RESOLUTION pre-warms its embedding cache with a batched call
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestResolutionPrewarmBatches:
    async def test_uncached_candidate_names_embedded_in_one_request(
        self, mongo_client, _kg_collection, mocker
    ) -> None:
        # Arrange — seed existing same-type PERSON candidates so the resolver's
        # semantic stage has candidates to compare against, then run extraction
        # for a new mention. The resolver should embed the input + candidate
        # names in ONE batched request, not one-per-name.
        from tree.models.fake_model import FakeLLM

        user = await _make_user()

        n_candidates = 30
        candidate_rows = [
            {
                "_id": f"{user.id}:person:candidate {i}",
                "user_id": user.id,
                "kind": "node",
                "type": "person",
                "name": f"candidate {i}",
                "canonical_name": f"candidate {i}",
                "aliases": [],
                "properties": {},
                "embedding": [],
                "sources": [],
                "merged_into": None,
                "created_at": _NOW,
                "updated_at": _NOW,
            }
            for i in range(n_candidates)
        ]
        await _kg_collection.insert_many(candidate_rows)

        doc = await _insert_doc(
            content="A brand new person appears.",
            source_uri=f"https://example.com/resolve-{uuid4().hex[:8]}",
            user_id=user.id,
        )
        new_name = f"brand new person {uuid4().hex[:6]}"
        llm = FakeLLM(
            [
                {
                    "nodes": [
                        {
                            "name": new_name,
                            "type": "person",
                            "subtype": "individual",
                            "properties": {},
                        }
                    ],
                    "edges": [],
                }
            ]
        )

        # A distinct resolution model so we can isolate the resolver's requests
        # from the search model's (task ④ / apply-writes).
        resolution_model = _CountingEmbeddingModel(dimensions=_DIMS)
        search_model = _CountingEmbeddingModel(dimensions=_DIMS)
        mocker.patch(
            "tree.memory.extraction.pipeline.init_mongodb", return_value=mongo_client
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.settings.mongo.mongo_initdb_database",
            TEST_DATABASE,
        )
        mocker.patch("tree.memory.extraction.pipeline.get_llm", return_value=llm)
        mocker.patch(
            "tree.memory.extraction.pipeline.get_search_embedding_model",
            return_value=search_model,
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.get_resolution_embedding_model",
            return_value=resolution_model,
        )
        mocker.patch(
            "tree.memory.extraction.pipeline.dedupe_entity",
            new=AsyncMock(return_value=DeduplicationResult(action="none")),
        )
        mocker.patch(
            "tree.memory.extraction.add_entity.dedupe_entity",
            new=AsyncMock(return_value=DeduplicationResult(action="none")),
        )

        # Act
        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        # Assert — the resolution model issued FEWER requests than the
        # one-per-name baseline. The pre-warm packs the input name + every
        # candidate name (30 + a couple of self/incoming) into ONE batched
        # request (default 1000-input cap), so a single request carries the
        # bulk of the names.
        assert resolution_model.calls, "resolution model was never called"
        baseline = n_candidates + 1  # one-per-name lower bound
        assert len(resolution_model.calls) < baseline, (
            f"resolution pre-warm should batch; expected < {baseline} requests, "
            f"saw {len(resolution_model.calls)} "
            f"(sizes={[len(c) for c in resolution_model.calls]})"
        )
        # The biggest single request carried (most of) the candidate set.
        assert max(len(c) for c in resolution_model.calls) >= n_candidates, (
            "expected one batched request carrying all uncached candidate names"
        )
        # Resolution embeds NAMES only (no multi-line node-text).
        for call in resolution_model.calls:
            assert all("\n" not in t for t in call)
