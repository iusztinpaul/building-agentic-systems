"""Integration tests for the document-shard fan-out parent flow (#056, ADR-002 §3/§4).

These exercise the parts of #056 that DO NOT need ``$vectorSearch`` (so they run
in CI without the mongot Search Index Management service):

* pending-doc resolution against a real Mongo (a ``Document`` is *pending* iff its
  ``_id`` is absent from every ``knowledge_graph.sources`` array);
* the parent flow ``memory_extraction_sharded`` fanning out one extraction run per
  shard and firing exactly ONE indexing run afterwards.

``run_deployment`` is mocked — truly spawning live child deployments needs
serve-workflows + a real Voyage key; that is the [HUMAN] acceptance AC (§3), not
this test. We spy on the faked ``run_deployment`` (mirroring the
``test_dream_supersession_and_fanout.py`` per-user-runner spy pattern) and assert
the parent's fan-out + single-index behavior.

The flow's DB plumbing is redirected to the test database the same way
``test_dream_consolidation.py`` does.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from beanie import PydanticObjectId

from tree.entities.documents import Document, SourceType
from tree.entities.knowledge_graph import NodeType
from tree.memory.extraction.fanout import (
    FanOutStats,
    _resolve_pending_document_ids,
    memory_extraction_sharded,
)

TEST_DATABASE = "integration_tests_twin"

pytestmark = [pytest.mark.slow]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def kg_collection(mongo_client):
    """The ``knowledge_graph`` collection (no vector index — resolution is a
    plain ``find`` over ``sources``, so we skip mongot)."""

    db = mongo_client[TEST_DATABASE]
    col = db["knowledge_graph"]
    yield col
    await db.drop_collection("knowledge_graph")


@pytest.fixture(autouse=True)
def _redirect_flow_db(mocker, mongo_client):
    """Point the flow's ``init_mongodb`` + db name at the test database."""

    mocker.patch(
        "tree.memory.extraction.fanout.init_mongodb",
        return_value=mongo_client,
    )
    mocker.patch(
        "tree.memory.extraction.fanout.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )


def _database(mongo_client):
    return mongo_client[TEST_DATABASE]


async def _insert_doc(user_id: PydanticObjectId, token: str) -> Document:
    doc = Document(
        title=f"doc-{token}",
        content=f"content {token}",
        source_type=SourceType.CONVERSATION,
        source_uri=f"conversation://{token}-{PydanticObjectId()}",
        user_id=user_id,
    )
    await doc.insert()
    return doc


def _kg_node_with_sources(
    *,
    user_id: PydanticObjectId,
    name: str,
    sources: list[PydanticObjectId],
) -> dict:
    """A KG node row whose ``sources`` provenance points at ingested docs."""

    now = datetime.now(UTC)
    return {
        "_id": f"{user_id}:object:{name}",
        "user_id": user_id,
        "kind": "node",
        "type": NodeType.OBJECT.value,
        "name": name,
        "canonical_name": name,
        "properties": {},
        "aliases": [],
        "confidence": 1.0,
        "embedding": [],
        "sources": sources,
        "merged_into": None,
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# Pending-doc resolution
# ---------------------------------------------------------------------------


async def test_resolution_returns_only_not_yet_ingested_docs(
    mongo_client, kg_collection
) -> None:
    """Only docs whose ``_id`` is absent from every KG ``sources`` array."""

    user_id = PydanticObjectId()
    ingested = await _insert_doc(user_id, "ingested")
    pending = await _insert_doc(user_id, "pending")

    # One KG node references the ingested doc in its ``sources``.
    await kg_collection.insert_one(
        _kg_node_with_sources(user_id=user_id, name="alpha", sources=[ingested.id])
    )

    result = await _resolve_pending_document_ids(
        database=_database(mongo_client), user_id=user_id
    )

    assert result == [str(pending.id)]


async def test_resolution_is_tenant_scoped(mongo_client, kg_collection) -> None:
    """Another user's KG provenance never marks this user's doc as ingested."""

    user_id = PydanticObjectId()
    other = PydanticObjectId()
    mine = await _insert_doc(user_id, "mine")

    # The OTHER user has a KG node that (impossibly) references my doc id —
    # tenant scoping must ignore it, so my doc stays pending.
    await kg_collection.insert_one(
        _kg_node_with_sources(user_id=other, name="beta", sources=[mine.id])
    )

    result = await _resolve_pending_document_ids(
        database=_database(mongo_client), user_id=user_id
    )

    assert result == [str(mine.id)]


async def test_resolution_empty_when_all_ingested(mongo_client, kg_collection) -> None:
    """All docs ingested ⇒ empty pending set."""

    user_id = PydanticObjectId()
    d1 = await _insert_doc(user_id, "one")
    d2 = await _insert_doc(user_id, "two")
    await kg_collection.insert_one(
        _kg_node_with_sources(user_id=user_id, name="gamma", sources=[d1.id, d2.id])
    )

    result = await _resolve_pending_document_ids(
        database=_database(mongo_client), user_id=user_id
    )

    assert result == []


# ---------------------------------------------------------------------------
# Parent flow fan-out (run_deployment spied)
# ---------------------------------------------------------------------------


@pytest.fixture
def spy_run_deployment(mocker):
    """Replace ``run_deployment`` in the flow module with a recording spy."""

    calls: list[tuple[str, dict]] = []

    async def _fake(name, parameters=None, **kwargs):
        calls.append((name, parameters or {}))

    mocker.patch(
        "tree.memory.extraction.fanout.run_deployment",
        side_effect=_fake,
    )
    return calls


async def test_flow_fans_out_per_shard_then_indexes_once(
    mongo_client, kg_collection, spy_run_deployment
) -> None:
    """Explicit 6 ids + num_shards=4 ⇒ 4 disjoint extraction runs + 1 index run."""

    user_id = PydanticObjectId()
    ids = [str(PydanticObjectId()) for _ in range(6)]

    stats = await memory_extraction_sharded(
        user_id=user_id, document_ids=ids, num_shards=4
    )

    extraction = [c for c in spy_run_deployment if "extraction" in c[0]]
    indexing = [c for c in spy_run_deployment if "indexing" in c[0]]

    # 4 shards over 6 ids → sizes 2,2,1,1; union == the 6 ids.
    assert len(extraction) == 4
    shard_id_lists = [p["document_ids"] for _, p in extraction]
    assert [len(s) for s in shard_id_lists] == [2, 2, 1, 1]
    assert [doc_id for shard in shard_id_lists for doc_id in shard] == ids
    assert all(p["user_id"] == str(user_id) for _, p in extraction)

    # Exactly ONE indexing run, fired LAST, scoped to the user only.
    assert len(indexing) == 1
    assert "indexing" in spy_run_deployment[-1][0]
    assert indexing[0][1] == {"user_id": str(user_id)}

    assert stats == FanOutStats(shards_total=4, succeeded=4, failed=0)


async def test_flow_resolves_pending_docs_when_ids_omitted(
    mongo_client, kg_collection, spy_run_deployment
) -> None:
    """Omitting document_ids drives the fan-out off the resolved pending set."""

    user_id = PydanticObjectId()
    ingested = await _insert_doc(user_id, "done")
    pending = await _insert_doc(user_id, "todo")
    await kg_collection.insert_one(
        _kg_node_with_sources(user_id=user_id, name="delta", sources=[ingested.id])
    )

    stats = await memory_extraction_sharded(user_id=user_id, num_shards=4)

    extraction = [c for c in spy_run_deployment if "extraction" in c[0]]
    indexing = [c for c in spy_run_deployment if "indexing" in c[0]]

    # Only the single pending doc is sharded → one extraction run.
    assert len(extraction) == 1
    assert extraction[0][1]["document_ids"] == [str(pending.id)]
    assert len(indexing) == 1
    assert stats.shards_total == 1
    assert stats.succeeded == 1


async def test_flow_no_pending_docs_is_noop(
    mongo_client, kg_collection, spy_run_deployment
) -> None:
    """A user whose docs are all ingested ⇒ no child runs, no indexing run."""

    user_id = PydanticObjectId()
    d1 = await _insert_doc(user_id, "a")
    await kg_collection.insert_one(
        _kg_node_with_sources(user_id=user_id, name="eps", sources=[d1.id])
    )

    stats = await memory_extraction_sharded(user_id=user_id, num_shards=4)

    assert spy_run_deployment == []
    assert stats == FanOutStats(shards_total=0)


async def test_flow_isolates_one_shard_failure_and_still_indexes(
    mongo_client, kg_collection, mocker
) -> None:
    """One shard's extraction raises; others complete; the index run still fires."""

    user_id = PydanticObjectId()
    ids = [str(PydanticObjectId()) for _ in range(8)]

    calls: list[tuple[str, dict]] = []

    async def _fake(name, parameters=None, **kwargs):
        params = parameters or {}
        calls.append((name, params))
        # Fail the first extraction shard only.
        if "extraction" in name and params.get("document_ids") == ids[:2]:
            raise RuntimeError("transient shard error")

    mocker.patch("tree.memory.extraction.fanout.run_deployment", side_effect=_fake)

    stats = await memory_extraction_sharded(
        user_id=user_id, document_ids=ids, num_shards=4
    )

    extraction = [c for c in calls if "extraction" in c[0]]
    indexing = [c for c in calls if "indexing" in c[0]]

    assert len(extraction) == 4
    assert stats.shards_total == 4
    assert stats.succeeded == 3
    assert stats.failed == 1
    assert "transient shard error" in next(iter(stats.failures.values()))

    # The single indexing run STILL fires after the gather.
    assert len(indexing) == 1
    assert "indexing" in calls[-1][0]
