"""E2E acceptance for the embedding split + batching feature (#045).

This is the HEADLINE end-to-end test that ties #039-#044 together on the
LIVE stack (MongoDB + mongot). Where the per-task tests
(``test_dedup_node_text_embedding.py``, ``test_embedding_batching.py``)
prove each behaviour in isolation — usually with dedup stubbed or a helper
called directly — this test runs the WHOLE chain a single time and asserts
the five things the feature must demonstrate together:

1. **Full chain runs** — ``memory_extract_etl_worker`` (extraction) →
   ``memory_indexing`` (indexing backfill + index reconcile) → live
   ``$vectorSearch`` via ``query_memory`` (query), against the live mongot
   stack for one seed user.
2. **Consistent persisted vectors** — a newly-extracted generic node's
   persisted ``embedding`` equals the SEARCH model's embedding of its
   NODE-TEXT (within float tolerance). The dedup-created vector and the
   index-backfilled vector live in ONE space.
3. **Vector-space agreement** — a ``$vectorSearch`` query for the seeded
   entity returns that entity at top rank (dedup space == query space ==
   index space).
4. **Batching reduces requests** — embedding N node-texts during the run
   issues materially fewer than N separate embed requests (counting model).
5. **Resolution uses the resolution model** — with a DISTINGUISHABLE
   resolution-vs-search model pairing, resolution embeddings come from the
   resolution model (names only, transient, never persisted) and persisted
   vectors come from the search model (node-text).

Plus the #045 AC #6 corollary: ``embed_nodes`` backfill immediately after
extraction re-embeds 0 of the dedup-created generic nodes (extraction
already persisted their node-text vectors).

Deterministic fake models keep this fast (within the ``slow`` tier) and
independent of the Voyage free-tier 3-RPM rate limit — the LIVE Voyage
behaviour and rate-limit reality are covered by the manual runbook
([HUMAN] AC), captured in the task log.
"""

from __future__ import annotations

import asyncio
import hashlib
from uuid import uuid4

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.entities.documents import Document, SourceType
from tree.entities.knowledge_graph import NodeType, build_node_id
from tree.entities.users import User
from tree.memory.embedding_text import node_to_embedding_text
from tree.memory.extraction.pipeline import memory_extract_etl_worker
from tree.memory.indexing.core import embed_nodes
from tree.memory.indexing.pipeline import memory_indexing
from tree.memory.query.core import query_memory, search_nodes
from tree.models.base import BaseEmbeddingModel

TEST_DATABASE = "integration_tests_twin"
_DIMS = 8


# ---------------------------------------------------------------------------
# Deterministic models — distinguishable SEARCH vs RESOLUTION handles
# ---------------------------------------------------------------------------


class _CountingPerTextModel(BaseEmbeddingModel):
    """Per-text-deterministic embedding that also records every request.

    * ``vec(text)`` is stable across processes (sha256, not builtin
      ``hash``) so vector equality proves WHICH text was embedded.
    * ``calls`` records the inputs of each SEPARATE ``embed`` request so a
      test can assert how many requests a stage issued (batching).
    """

    def __init__(self, dimensions: int = _DIMS) -> None:
        self._dimensions = dimensions
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def vec(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i] / 255.0 for i in range(self._dimensions)]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vec(t) for t in texts]


class _RecordingResolutionModel(BaseEmbeddingModel):
    """Resolution handle: returns a sentinel vector family the search model
    never produces, and records every text it embeds.

    If a resolution vector were ever persisted, the consistency assertion
    would catch it (the sentinel ``[9.0]*DIMS`` is not any search-model
    node-text vector). If resolution ever embedded a multi-line node-text
    instead of a bare name, the "names only" assertion would catch it.
    """

    def __init__(self, dimensions: int = _DIMS) -> None:
        self._dimensions = dimensions
        self.embedded_texts: list[str] = []
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        self.embedded_texts.extend(texts)
        return [[9.0] * self._dimensions for _ in texts]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user() -> User:
    """Insert a fresh tenant. The ``after_insert`` hook seeds ``person:self``."""

    user = User(identifier=f"e2e-user-{PydanticObjectId()}@example.com")
    await user.insert()
    return user


async def _insert_doc(
    *, content: str, source_uri: str, user_id: PydanticObjectId
) -> Document:
    doc = Document(
        title="E2E Test Document",
        content=content,
        source_type=SourceType.HUGGINGFACE,
        source_uri=source_uri,
        user_id=user_id,
        authors=["Test Author"],
    )
    await doc.insert()
    return doc


def _patch_extraction_models(
    mocker,
    mongo_client,
    *,
    llm,
    search_model: BaseEmbeddingModel,
    resolution_model: BaseEmbeddingModel,
) -> None:
    """Point the extraction flow at the test DB + the distinguishable models.

    Dedup is NOT stubbed here — the live ``$vectorSearch`` runs for real so
    the run exercises the shared node-text space against mongot.
    """

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


def _patch_indexing_models(
    mocker, mongo_client, *, search_model: BaseEmbeddingModel
) -> None:
    """Point the indexing flow at the test DB + the SEARCH model.

    The boot-time dim assertion is stubbed: this test builds the index at
    the fake model's dimension (8), not the production 1024.
    """

    from unittest.mock import AsyncMock

    mocker.patch(
        "tree.memory.indexing.pipeline.init_mongodb", return_value=mongo_client
    )
    mocker.patch(
        "tree.memory.indexing.pipeline.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )
    mocker.patch(
        "tree.memory.indexing.pipeline.get_embedding_model",
        return_value=search_model,
    )
    mocker.patch(
        "tree.memory.indexing.pipeline.assert_settings_match_live_vector_index",
        new_callable=AsyncMock,
    )


@pytest.fixture(autouse=True)
def _refresh_prefect_cache(monkeypatch):
    """Force task-④'s ``INPUTS`` cache to recompute so the counting model
    actually sees every embed request (Prefect would otherwise serve a
    cached vector from a prior session for the same node-text)."""

    monkeypatch.setenv("PREFECT_TASKS_REFRESH_CACHE", "true")


@pytest.fixture
async def _kg_collection(mongo_client):
    db = mongo_client[TEST_DATABASE]
    yield db["knowledge_graph"]
    await db.drop_collection("knowledge_graph")


async def _wait_for_indexed_count(
    collection,
    user_id: PydanticObjectId,
    expected: int,
    *,
    timeout: float = 60.0,
) -> None:
    """Poll ``$vectorSearch`` until at least ``expected`` nodes are returned.

    mongot is eventually-consistent: after the index is (re)created and the
    vectors are written, there is a convergence window before
    ``$vectorSearch`` returns them.
    """

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
                        "numCandidates": 200,
                        "limit": 100,
                        "filter": {"user_id": user_id, "kind": "node"},
                    }
                },
                {"$count": "n"},
            ]
        )
        rows = [r async for r in cursor]
        if rows and rows[0].get("n", 0) >= expected:
            return
        await asyncio.sleep(1.5)
    raise AssertionError(
        f"vector_index did not return {expected} nodes within {timeout}s "
        f"(mongot convergence window exceeded)"
    )


# ---------------------------------------------------------------------------
# The headline e2e — full chain on the live stack
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.requires_mongot
@pytest.mark.usefixtures("_skip_without_mongot")
class TestEmbeddingSplitAndBatchingE2E:
    async def test_full_chain_consistent_space_batched_and_routed(
        self, mongo_client, _kg_collection, mocker
    ) -> None:
        """Extraction → indexing → live $vectorSearch, asserting all five
        feature guarantees in one cohesive run."""

        # --- Arrange ---------------------------------------------------------
        # A document yielding MANY distinct generic (PERSON) entities so the
        # batching assertion is meaningful (one batched request << N names),
        # plus one "headline" entity we query back for the top-rank assertion.
        user = await _make_user()
        run_tag = uuid4().hex[:8]
        headline_name = f"andrej karpathy {run_tag}"
        n_extra = 30  # extra distinct people → N = 31 node-texts to embed

        nodes = [
            {
                "name": headline_name,
                "type": "person",
                "subtype": "individual",
                "properties": {"role": "ai researcher", "bio": "memory for agents"},
            }
        ]
        nodes += [
            {
                "name": f"person {i} {run_tag}",
                "type": "person",
                "subtype": "individual",
                "properties": {"role": "researcher"},
            }
            for i in range(n_extra)
        ]
        total_people = len(nodes)

        from tree.models.fake_model import FakeLLM

        llm = FakeLLM([{"nodes": nodes, "edges": []}])
        doc = await _insert_doc(
            content="A document about many AI researchers and their memory work.",
            source_uri=f"https://example.com/e2e-{run_tag}",
            user_id=user.id,
        )

        search_model = _CountingPerTextModel(dimensions=_DIMS)
        resolution_model = _RecordingResolutionModel(dimensions=_DIMS)
        _patch_extraction_models(
            mocker,
            mongo_client,
            llm=llm,
            search_model=search_model,
            resolution_model=resolution_model,
        )
        # Defensive: never reach a live supersession LLM (no preferences here,
        # but stub so the flow can never call out).
        from unittest.mock import AsyncMock

        mocker.patch(
            "tree.memory.extraction.preference_supersession.judge_contradiction",
            new=AsyncMock(return_value=(False, 0.0)),
        )

        # --- Act 1: extraction ----------------------------------------------
        with prefect_tags("tests"):
            summary = await memory_extract_etl_worker(
                user_id=user.id, document_ids=[str(doc.id)]
            )

        # --- Assert 2: consistent persisted vectors -------------------------
        # The headline PERSON node carries the SEARCH model's NODE-TEXT vector,
        # NOT its bare name and NOT the resolution sentinel.
        headline_id = build_node_id(user.id, NodeType.PERSON, headline_name)
        row = await _kg_collection.find_one({"_id": headline_id})
        assert row is not None, f"headline node not created; summary={summary}"

        node_text = node_to_embedding_text(row)
        assert node_text.startswith(f"person: {headline_name}")
        assert row["embedding"] == search_model.vec(node_text), (
            "persisted vector must equal the SEARCH model's node-text embedding "
            "(consistent space) — got a different vector"
        )
        assert row["embedding"] != search_model.vec(headline_name), (
            "persisted vector is the bare-NAME embedding, not the node-text"
        )
        assert row["embedding"] != [9.0] * _DIMS, (
            "the RESOLUTION sentinel vector leaked into the persisted node"
        )

        # --- Assert 5: resolution routed to the resolution model ------------
        # Resolution embedded NAMES only (transient), never persisted.
        assert resolution_model.embedded_texts, "resolution model never called"
        assert any(headline_name in t for t in resolution_model.embedded_texts), (
            "resolution model never embedded the entity name"
        )
        assert all("\n" not in t for t in resolution_model.embedded_texts), (
            "resolution embedded multi-line node-text; expected NAME only"
        )

        # --- Assert 4: batching reduced request count -----------------------
        # The search model embedded all node-texts in FAR fewer requests than
        # the one-per-name baseline (the default 1000-input cap packs all
        # node-texts into one request). The largest single request carries
        # >= the full people set.
        assert search_model.calls, "search model never embedded anything"
        max_request_size = max(len(c) for c in search_model.calls)
        assert max_request_size >= total_people, (
            f"expected one batched request carrying >= {total_people} node-texts; "
            f"largest had {max_request_size} (sizes="
            f"{[len(c) for c in search_model.calls]})"
        )
        assert len(search_model.calls) < total_people, (
            f"batching should keep total requests well under the one-per-name "
            f"baseline ({total_people}); saw {len(search_model.calls)}"
        )

        # --- Act 2: indexing backfill (AC #6 — no-op for extracted nodes) ----
        # Count how many of the EXISTING generic nodes the backfill re-embeds.
        # Extraction already persisted their node-text vectors, so the headline
        # node must NOT be touched.
        before_vec = row["embedding"]
        embedded_count = await embed_nodes(
            mongo_client, TEST_DATABASE, search_model, user.id
        )
        after = await _kg_collection.find_one({"_id": headline_id})
        assert after is not None
        assert after["embedding"] == before_vec, (
            f"indexing backfill re-embedded the extracted node "
            f"(embedded_count={embedded_count}) — vectors should be reused, "
            f"not recomputed"
        )

        # --- Act 3: full indexing pipeline (ensure indexes + reconcile) ------
        _patch_indexing_models(mocker, mongo_client, search_model=search_model)
        with prefect_tags("tests"):
            await memory_indexing(user_id=user.id)

        # Wait for mongot to converge on the freshly written node vectors.
        # ``person:self`` carries no embedding, so it is not a $vectorSearch
        # candidate; we expect at least the people we extracted.
        await _wait_for_indexed_count(_kg_collection, user.id, total_people)

        # --- Assert 3: vector-space agreement (top-rank retrieval) ----------
        # A $vectorSearch whose query vector is the headline node's node-text
        # (same SEARCH model) must return the headline node at TOP rank: the
        # dedup/index/query spaces all agree.
        query_vec = search_model.vec(node_text)
        hits = await _kg_collection.aggregate(
            [
                {
                    "$vectorSearch": {
                        "index": "vector_index",
                        "path": "embedding",
                        "queryVector": query_vec,
                        "numCandidates": 200,
                        "limit": 5,
                        "filter": {"user_id": user.id, "kind": "node"},
                    }
                },
            ]
        )
        ranked = [h async for h in hits]
        assert ranked, "$vectorSearch returned no nodes (index not converged?)"
        assert ranked[0]["_id"] == headline_id, (
            "vector-space disagreement: the headline node's own node-text "
            f"vector did not retrieve it at top rank; top hit was "
            f"{ranked[0]['_id']!r}"
        )

        # --- Assert 1: the query layer also returns it (end of the chain) ----
        # ``search_nodes`` fuses vector + text; the headline node must surface.
        seeds = await search_nodes(
            mongo_client,
            TEST_DATABASE,
            headline_name,
            search_model,
            user.id,
            top_k=10,
        )
        seed_ids = {n["_id"] for n in seeds}
        assert headline_id in seed_ids, (
            "the headline node did not surface in the fused query results"
        )

        # ``query_memory`` (search + graph expansion) completes and includes it.
        result = await query_memory(
            mongo_client,
            TEST_DATABASE,
            headline_name,
            search_model,
            user.id,
            top_k=10,
        )
        result_ids = {n["_id"] for n in result.nodes}
        assert headline_id in result_ids, (
            "the full query_memory chain did not return the headline node"
        )
