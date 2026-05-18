"""End-to-end validator integration tests (#030).

Mocks Gemini with synthetic emissions covering each branch of the
two-tier validator and asserts the on-disk shape of three collections:

* ``knowledge_graph`` — only rows that passed the envelope.
* ``extraction_rejections`` — one row per envelope drop.
* ``extraction_dropped_fields`` — one row per per-field drop.

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
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM


TEST_DATABASE = "integration_tests_twin"


async def _make_user() -> User:
    user = User(identifier=f"test-validator-user-{PydanticObjectId()}")
    await user.insert()
    return user


async def _insert_doc(
    *, content: str, user_id: PydanticObjectId, source_uri: str
) -> Document:
    doc = Document(
        title="Validator E2E",
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


async def _dropped_field_rows(mongo_client) -> list[dict[str, Any]]:
    return (
        await mongo_client[TEST_DATABASE]["extraction_dropped_fields"].find().to_list()
    )


async def _kg_rows(mongo_client) -> list[dict[str, Any]]:
    return await mongo_client[TEST_DATABASE]["knowledge_graph"].find().to_list()


# ---------------------------------------------------------------------------
# Happy path — every envelope passes, no field drops
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestValidatorEnd2End:
    """One test class per validator branch the AC enumerates.

    Each test asserts the on-disk shape of the three collections that
    matter — the validator is the single seam where the prompt's
    output becomes the graph's reality.
    """

    async def test_happy_path_writes_kg_no_audit(self, mongo_client, mocker) -> None:
        user = await _make_user()
        doc = await _insert_doc(
            content="Alice is at Anthropic.",
            user_id=user.id,
            source_uri="https://example.com/happy",
        )
        response = {
            "nodes": [
                {
                    "name": "alice",
                    "type": "person",
                    "subtype": "individual",
                    "properties": {
                        "email": "alice@example.com",
                        "occupation": "engineer",
                    },
                },
                {
                    "name": "anthropic",
                    "type": "organization",
                    "subtype": "company",
                    "properties": {},
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

        # No audit rows.
        assert await _rejection_rows(mongo_client) == []
        assert await _dropped_field_rows(mongo_client) == []

        # KG carries the two LLM-extracted rows with their properties intact.
        kg = await _kg_rows(mongo_client)
        alice_rows = [r for r in kg if r["type"] == "person" and r["name"] == "alice"]
        assert len(alice_rows) == 1
        assert alice_rows[0]["properties"]["email"] == "alice@example.com"
        assert alice_rows[0]["properties"]["occupation"] == "engineer"
        assert alice_rows[0]["subtype"] == "individual"
        # ExtractorInfo is stamped on every LLM-extracted row.
        assert alice_rows[0]["extractor"] is not None
        assert alice_rows[0]["extractor"]["name"]  # non-empty model name

    async def test_unknown_type_drops_row_and_writes_rejection(
        self, mongo_client, mocker
    ) -> None:
        user = await _make_user()
        doc = await _insert_doc(
            content="dragons.",
            user_id=user.id,
            source_uri="https://example.com/dragon",
        )
        response = {
            "nodes": [
                {
                    "name": "smaug",
                    "type": "dragon",
                    "properties": {"breath": "fire"},
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

        rejections = await _rejection_rows(mongo_client)
        assert len(rejections) == 1
        row = rejections[0]
        assert row["rejection_reason"] == "unknown_type"
        assert row["user_id"] == user.id
        assert row["raw_row"]["type"] == "dragon"

        # The unknown row never reached the graph.
        kg = await _kg_rows(mongo_client)
        assert not [r for r in kg if r.get("name") == "smaug"]

    async def test_disallowed_pair_drops_edge_and_writes_rejection(
        self, mongo_client, mocker
    ) -> None:
        user = await _make_user()
        doc = await _insert_doc(
            content="anthropic employs alice.",
            user_id=user.id,
            source_uri="https://example.com/pair",
        )
        # employed_by is (person, organization); reverse direction is
        # rejected at the envelope.
        response = {
            "nodes": [
                {
                    "name": "alice",
                    "type": "person",
                    "subtype": "individual",
                    "properties": {},
                },
                {
                    "name": "anthropic",
                    "type": "organization",
                    "subtype": "company",
                    "properties": {},
                },
            ],
            "edges": [
                {
                    "source_node_id": "anthropic",
                    "source_type": "organization",
                    "target_node_id": "alice",
                    "target_type": "person",
                    "type": "related_to",
                    "semantic_type": "employed_by",
                    "properties": {},
                }
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

        rejections = await _rejection_rows(mongo_client)
        assert len(rejections) == 1
        assert rejections[0]["rejection_reason"] == "disallowed_pair"

        # Both NODES still landed (they're valid envelopes); only the
        # bad edge was dropped.
        kg = await _kg_rows(mongo_client)
        edges = [r for r in kg if r["kind"] == "edge" and r["type"] == "related_to"]
        # No related_to edges remain (the only one was dropped).
        assert edges == []

    async def test_missing_subtype_drops_row(self, mongo_client, mocker) -> None:
        user = await _make_user()
        doc = await _insert_doc(
            content="anthropic.",
            user_id=user.id,
            source_uri="https://example.com/missing-subtype",
        )
        response = {
            "nodes": [
                {
                    "name": "anthropic",
                    "type": "organization",
                    # NO subtype emitted — envelope must reject.
                    "properties": {"jurisdiction": "delaware"},
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

        rejections = await _rejection_rows(mongo_client)
        assert len(rejections) == 1
        assert rejections[0]["rejection_reason"] == "missing_subtype"

        kg = await _kg_rows(mongo_client)
        assert not [
            r for r in kg if r["kind"] == "node" and r.get("name") == "anthropic"
        ]

    async def test_invalid_field_dropped_row_kept(self, mongo_client, mocker) -> None:
        user = await _make_user()
        doc = await _insert_doc(
            content="alice.",
            user_id=user.id,
            source_uri="https://example.com/bad-field",
        )
        response = {
            "nodes": [
                {
                    "name": "alice",
                    "type": "person",
                    "subtype": "individual",
                    "properties": {
                        "email": 12345,  # int, not str → dropped
                        "garbage_field": 42,  # unknown → dropped
                        "occupation": "engineer",  # valid
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

        # Row landed (lenient policy) with ONLY the valid field.
        kg = await _kg_rows(mongo_client)
        alice = [r for r in kg if r["type"] == "person" and r.get("name") == "alice"]
        assert len(alice) == 1
        assert alice[0]["properties"].get("occupation") == "engineer"
        assert "email" not in alice[0]["properties"]
        assert "garbage_field" not in alice[0]["properties"]

        # Two ExtractionDroppedField rows — one per bad field.
        drops = await _dropped_field_rows(mongo_client)
        dropped_fields = {d["dropped_field"] for d in drops}
        assert dropped_fields == {"email", "garbage_field"}

        # No envelope rejection — the row survived.
        assert await _rejection_rows(mongo_client) == []

    async def test_all_fields_invalid_row_still_lands(
        self, mongo_client, mocker
    ) -> None:
        """Lenient policy: even an emission with every field invalid
        keeps the row (per `plan.md:336-339`)."""

        user = await _make_user()
        doc = await _insert_doc(
            content="alice.",
            user_id=user.id,
            source_uri="https://example.com/all-bad-fields",
        )
        response = {
            "nodes": [
                {
                    "name": "alice",
                    "type": "person",
                    "subtype": "individual",
                    "properties": {
                        # ``email`` / ``occupation`` / ``nationality`` are str | None
                        # — passing a list triggers a ValidationError. ``aliases``
                        # is a list[str], passing an int triggers same.
                        "email": [1, 2, 3],
                        "occupation": {"nested": "no"},
                        "nationality": ["bad"],
                        "aliases": 5,
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

        # Row landed with an empty user-provided properties subset.
        kg = await _kg_rows(mongo_client)
        alice = [r for r in kg if r["type"] == "person" and r.get("name") == "alice"]
        assert len(alice) == 1

        # Every emitted field was recorded as a drop.
        drops = await _dropped_field_rows(mongo_client)
        assert {d["dropped_field"] for d in drops} == {
            "email",
            "occupation",
            "nationality",
            "aliases",
        }

        # No envelope rejection.
        assert await _rejection_rows(mongo_client) == []

    async def test_extractor_stamped_on_llm_rows_absent_on_structural(
        self, mongo_client, mocker
    ) -> None:
        user = await _make_user()
        doc = await _insert_doc(
            content="alice works at anthropic.",
            user_id=user.id,
            source_uri="https://example.com/extractor",
        )
        response = {
            "nodes": [
                {
                    "name": "alice",
                    "type": "person",
                    "subtype": "individual",
                    "properties": {},
                }
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

        kg = await _kg_rows(mongo_client)
        # Structural rows: document + chunk → extractor absent / null.
        structural = [r for r in kg if r["type"] in {"document", "chunk"}]
        assert structural, "expected document + chunk rows from structural emission"
        for row in structural:
            # The pipeline writes the row WITHOUT the ``extractor`` field
            # (column is unset). When the doc is loaded into Python the
            # default is None; on Mongo's side the key may or may not
            # be present — both shapes are acceptable.
            assert row.get("extractor") in (None, {})

        # LLM-extracted rows carry extractor. ``person:self`` is a
        # user-seed row (created by :meth:`User.after_insert`, not by
        # the LLM) so it intentionally carries no extractor — filter it
        # out before the per-row check.
        llm_rows = [
            r
            for r in kg
            if r["kind"] == "node"
            and r["type"] not in {"document", "chunk"}
            and r.get("name") != "self"
        ]
        assert llm_rows, "expected at least one LLM-extracted node (alice)"
        for row in llm_rows:
            assert row.get("extractor") is not None, (
                f"extractor missing on LLM-extracted row {row['_id']}"
            )
            assert row["extractor"].get("name")
            assert row["extractor"].get("version")

    async def test_audit_row_carries_user_id_for_isolation(
        self, mongo_client, mocker
    ) -> None:
        """Audit rows are tenant-scoped — a second user's rejection
        does not surface in the first user's audit query."""

        user_a = await _make_user()
        user_b = await _make_user()
        doc_a = await _insert_doc(
            content="dragon a.",
            user_id=user_a.id,
            source_uri="https://example.com/a",
        )
        doc_b = await _insert_doc(
            content="dragon b.",
            user_id=user_b.id,
            source_uri="https://example.com/b",
        )
        bad_response = {
            "nodes": [{"name": "smaug", "type": "dragon", "properties": {}}],
            "edges": [],
        }

        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([bad_response]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )
        with prefect_tags("tests"):
            await memory_extraction(user_id=user_a.id, document_ids=[str(doc_a.id)])

        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([bad_response]),
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )
        with prefect_tags("tests"):
            await memory_extraction(user_id=user_b.id, document_ids=[str(doc_b.id)])

        rows = await _rejection_rows(mongo_client)
        # Each tenant gets one rejection row pinned to their user_id.
        per_user = {r["user_id"]: r for r in rows}
        assert per_user[user_a.id]["user_id"] == user_a.id
        assert per_user[user_b.id]["user_id"] == user_b.id
        assert per_user[user_a.id]["_id"] != per_user[user_b.id]["_id"]


# ---------------------------------------------------------------------------
# Lightweight non-slow sanity test — Beanie ODMs roundtrip live
# ---------------------------------------------------------------------------


class TestAuditOdmLiveRoundTrip:
    """Insert one row of each audit collection directly through Beanie
    and read it back. Pins (a) the indexes were created and (b) the
    ODM round-trips against live Mongo."""

    async def test_extraction_rejection_round_trip(self, mongo_client) -> None:
        from datetime import UTC, datetime

        from tree.entities.extraction_audit import ExtractionRejection
        from tree.entities.knowledge_graph import ExtractorInfo

        user_id = PydanticObjectId()
        ts = datetime.now(tz=UTC)
        row = ExtractionRejection(
            user_id=user_id,
            timestamp=ts,
            rejection_reason="unknown_type",
            raw_row={"type": "dragon", "name": "smaug"},
            extractor=ExtractorInfo(name="gemini-2.5-pro", version="tree-memory-0.1.0"),
        )
        await row.insert()
        fetched = await ExtractionRejection.find_one(
            ExtractionRejection.user_id == user_id
        )
        assert fetched is not None
        assert fetched.rejection_reason == "unknown_type"
        assert fetched.raw_row == {"type": "dragon", "name": "smaug"}
        assert fetched.extractor is not None
        assert fetched.extractor.name == "gemini-2.5-pro"

    async def test_extraction_dropped_field_round_trip(self, mongo_client) -> None:
        from datetime import UTC, datetime

        from tree.entities.extraction_audit import ExtractionDroppedField

        user_id = PydanticObjectId()
        row = ExtractionDroppedField(
            user_id=user_id,
            timestamp=datetime.now(tz=UTC),
            row_type="person",
            row_subtype="individual",
            dropped_field="email",
            raw_value=12345,
            reason="email: input should be a valid string",
        )
        await row.insert()
        fetched = await ExtractionDroppedField.find_one(
            ExtractionDroppedField.user_id == user_id
        )
        assert fetched is not None
        assert fetched.dropped_field == "email"
        assert fetched.row_type == "person"

    async def test_indexes_present(self, mongo_client) -> None:
        """The two index lists declared in
        :class:`ExtractionRejection.Settings` / etc. land in Mongo at
        ``init_beanie`` time. Verify by reading
        ``listIndexes`` for each collection."""

        db = mongo_client[TEST_DATABASE]

        rej_cursor = await db["extraction_rejections"].list_indexes()
        rej_indexes = await rej_cursor.to_list(length=None)
        rej_names = {idx["name"] for idx in rej_indexes}
        # The two named indexes declared in Settings + the auto _id_.
        assert "user_timestamp_desc" in rej_names
        assert "user_reason" in rej_names

        drop_cursor = await db["extraction_dropped_fields"].list_indexes()
        drop_indexes = await drop_cursor.to_list(length=None)
        drop_names = {idx["name"] for idx in drop_indexes}
        assert "user_type_field" in drop_names
        assert "user_timestamp_desc" in drop_names
