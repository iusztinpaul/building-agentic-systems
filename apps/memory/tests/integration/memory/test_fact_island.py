"""End-to-end integration test for the #031 ``fact`` escape-hatch node.

Two complementary tests:

1. ``test_fact_node_lands_with_edge_to_fact_rejected`` — the LLM emits
   one valid ``fact`` row + one malformed ``mentions chunk -> fact``
   edge. After the extraction pipeline:
   * The fact lands as a ``KnowledgeGraphEntry(kind="node", type="fact")``.
   * The edge is dropped to ``extraction_rejections`` with
     ``rejection_reason="fact_endpoint_disallowed"``.
   * No edge with a ``fact`` endpoint exists in ``knowledge_graph``.
2. ``test_kgquery_find_facts_round_trip`` — after the pipeline run,
   :meth:`KGQuery.find_facts` returns the fact by every filter
   combination (subject / predicate / object).

Marked ``@pytest.mark.slow`` because it spins up the full Prefect
flow.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.entities.documents import Document, SourceType
from tree.entities.users import User
from tree.memory.extraction.dedup import DeduplicationResult
from tree.memory.extraction.pipeline import memory_extraction
from tree.memory.query.kgquery import KGQuery
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM


TEST_DATABASE = "integration_tests_twin"


async def _make_user() -> User:
    user = User(identifier=f"test-fact-island-user-{PydanticObjectId()}")
    await user.insert()
    return user


async def _insert_doc(
    *, content: str, user_id: PydanticObjectId, source_uri: str
) -> Document:
    doc = Document(
        title="Fact Island E2E",
        content=content,
        source_type=SourceType.HUGGINGFACE,
        source_uri=source_uri,
        user_id=user_id,
        authors=["Test"],
    )
    await doc.insert()
    return doc


def _patch_pipeline_deps(
    mocker,
    mongo_client,
    *,
    llm: FakeLLM,
    embedding_model: FakeEmbeddingModel,
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
        "tree.memory.extraction.pipeline.get_embedding_model",
        return_value=embedding_model,
    )
    # #042: task ④ embeds node-text via the SEARCH model factory.
    mocker.patch(
        "tree.memory.extraction.pipeline.get_search_embedding_model",
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


async def _rejection_rows(mongo_client) -> list[dict[str, Any]]:
    return await mongo_client[TEST_DATABASE]["extraction_rejections"].find().to_list()


async def _kg_rows(mongo_client) -> list[dict[str, Any]]:
    return await mongo_client[TEST_DATABASE]["knowledge_graph"].find().to_list()


@pytest.mark.slow
class TestFactIslandEnd2End:
    """A fact lands; every edge attempt with a fact endpoint is dropped
    to ``extraction_rejections``."""

    async def test_fact_node_lands_with_edge_to_fact_rejected(
        self, mongo_client, mocker
    ) -> None:
        user = await _make_user()
        doc = await _insert_doc(
            content="Earth orbits the Sun once every 365.25 days.",
            user_id=user.id,
            source_uri="https://example.com/fact-island",
        )
        # The LLM emits one valid fact + one malformed ``mentions``
        # edge whose target is the fact. The envelope validator must
        # let the fact through and reject the edge.
        # Three attempted bad edges + one valid fact node. Each edge
        # exercises a different layer of the island rule:
        #   * ``mentions chunk -> fact`` — structural edge that the LLM
        #     shouldn't be emitting at all (rejected by the parser as
        #     ``non_extractable_type``).
        #   * ``related_to person -> fact`` and ``related_to fact ->
        #     person`` — LLM-extractable edges; pair isn't in any
        #     semantic's ``allowed_pairs`` so the parser rejects as
        #     ``disallowed_pair`` (the structural carve-out below the
        #     parser is unreachable because the parser intercepts first;
        #     either reason indicates the island rule fired).
        response = {
            "nodes": [
                {
                    "name": "earth-orbits-sun",
                    "type": "fact",
                    "properties": {
                        "subject": "earth",
                        "predicate": "orbits",
                        "object": "sun",
                    },
                },
                # Also emit a person so the related_to edge has a valid
                # second endpoint (otherwise the parser would reject on
                # the missing person, not on the fact endpoint).
                {
                    "name": "alice",
                    "type": "person",
                    "subtype": "individual",
                    "properties": {},
                },
            ],
            "edges": [
                {
                    "source_node_id": "https://example.com/fact-island#chunk-0",
                    "source_type": "chunk",
                    "target_node_id": "earth-orbits-sun",
                    "target_type": "fact",
                    "type": "mentions",
                    "properties": {},
                },
                {
                    "source_node_id": "alice",
                    "source_type": "person",
                    "target_node_id": "earth-orbits-sun",
                    "target_type": "fact",
                    "type": "related_to",
                    "semantic_type": "knows",
                    "properties": {},
                },
                {
                    "source_node_id": "earth-orbits-sun",
                    "source_type": "fact",
                    "target_node_id": "alice",
                    "target_type": "person",
                    "type": "related_to",
                    "semantic_type": "knows",
                    "properties": {},
                },
            ],
        }
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([response]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        # The fact landed as a node row with its properties intact.
        kg = await _kg_rows(mongo_client)
        facts = [r for r in kg if r["kind"] == "node" and r["type"] == "fact"]
        assert len(facts) == 1, f"expected one fact node, got {len(facts)}"
        fact_row = facts[0]
        assert fact_row["name"] == "earth-orbits-sun"
        assert fact_row["subtype"] is None
        # Wire-form ``"object"`` key (alias of ``object_``) survives the
        # alias-aware validator and lands on the stored doc.
        assert fact_row["properties"] == {
            "subject": "earth",
            "predicate": "orbits",
            "object": "sun",
        }
        # Extractor provenance is stamped on LLM-extracted rows.
        assert fact_row.get("extractor") is not None

        # NO edge in knowledge_graph touches the fact node.
        fact_id = fact_row["_id"]
        edges_to_fact = [
            r
            for r in kg
            if r["kind"] == "edge"
            and (
                r.get("source_node_id") == fact_id or r.get("target_node_id") == fact_id
            )
        ]
        assert edges_to_fact == [], f"expected no edges to fact, got {edges_to_fact!r}"

        # Every bad edge attempt landed as a row in
        # ``extraction_rejections``. We don't care which exact reason
        # token fires (parser vs envelope, ``non_extractable_type`` vs
        # ``disallowed_pair`` vs ``fact_endpoint_disallowed``) — the
        # island rule is satisfied as long as none of the bad edges
        # reach ``knowledge_graph``. Pin the count + the
        # rejected-shape so a regression that lets one through fails
        # loudly.
        rejections = await _rejection_rows(mongo_client)
        # 3 bad edges + however many other rows the LLM also tried to
        # emit. Pin only that we have at least three fact-endpoint
        # rejections.
        fact_endpoint_rejections = [
            r
            for r in rejections
            if (
                r.get("raw_row", {}).get("target_type") == "fact"
                or r.get("raw_row", {}).get("source_type") == "fact"
            )
        ]
        assert len(fact_endpoint_rejections) >= 3, (
            f"expected at least 3 fact-endpoint rejections, got "
            f"{len(fact_endpoint_rejections)}: {rejections!r}"
        )
        # Every rejection reason should be one of the documented
        # island-rule reasons.
        allowed_reasons = {
            "fact_endpoint_disallowed",
            "disallowed_pair",
            "non_extractable_type",
        }
        for r in fact_endpoint_rejections:
            assert r["rejection_reason"] in allowed_reasons, (
                f"unexpected rejection_reason {r['rejection_reason']!r}; "
                f"expected one of {allowed_reasons}"
            )

    async def test_kgquery_find_facts_round_trip(self, mongo_client, mocker) -> None:
        user = await _make_user()
        doc = await _insert_doc(
            content="Earth orbits the Sun.",
            user_id=user.id,
            source_uri="https://example.com/find-facts",
        )
        response = {
            "nodes": [
                {
                    "name": "earth-orbits-sun",
                    "type": "fact",
                    "properties": {
                        "subject": "earth",
                        "predicate": "orbits",
                        "object": "sun",
                    },
                },
            ],
            "edges": [],
        }
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([response]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        kg = KGQuery(user.id)

        # Every single-filter combination returns the row.
        by_subject = await kg.find_facts(subject="earth")
        assert len(by_subject) == 1
        assert by_subject[0].name == "earth-orbits-sun"

        by_predicate = await kg.find_facts(predicate="orbits")
        assert len(by_predicate) == 1

        by_object = await kg.find_facts(object="sun")
        assert len(by_object) == 1

        # Three-filter combo also finds it.
        by_all = await kg.find_facts(subject="earth", predicate="orbits", object="sun")
        assert len(by_all) == 1

        # No filter returns every fact for this user.
        all_facts = await kg.find_facts()
        assert len(all_facts) == 1

        # A filter that doesn't match returns empty.
        none_match = await kg.find_facts(subject="mars")
        assert none_match == []

    async def test_two_users_facts_are_isolated(self, mongo_client, mocker) -> None:
        """Tenant isolation: user A's facts MUST NOT surface in user
        B's :meth:`KGQuery.find_facts`."""

        user_a = await _make_user()
        user_b = await _make_user()
        doc_a = await _insert_doc(
            content="A's fact.",
            user_id=user_a.id,
            source_uri="https://example.com/iso-a",
        )
        doc_b = await _insert_doc(
            content="B's fact.",
            user_id=user_b.id,
            source_uri="https://example.com/iso-b",
        )
        response = {
            "nodes": [
                {
                    "name": "shared-fact-name",
                    "type": "fact",
                    "properties": {
                        "subject": "x",
                        "predicate": "is",
                        "object": "y",
                    },
                },
            ],
            "edges": [],
        }
        # Run extraction for user A.
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([response]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )
        with prefect_tags("tests"):
            await memory_extraction(user_id=user_a.id, document_ids=[str(doc_a.id)])

        # And again for user B.
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([response]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )
        with prefect_tags("tests"):
            await memory_extraction(user_id=user_b.id, document_ids=[str(doc_b.id)])

        # Each user sees exactly their own fact.
        a_facts = await KGQuery(user_a.id).find_facts(object="y")
        b_facts = await KGQuery(user_b.id).find_facts(object="y")
        assert len(a_facts) == 1
        assert len(b_facts) == 1
        # The _id carries the user prefix — pin the disjoint shape.
        assert a_facts[0].id != b_facts[0].id
        assert str(user_a.id) in a_facts[0].id
        assert str(user_b.id) in b_facts[0].id
