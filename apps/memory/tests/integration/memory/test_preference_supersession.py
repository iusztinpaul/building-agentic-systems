"""End-to-end integration tests for the preference / fact
supersession resolver branch (#032).

Each test runs ``memory_extraction`` against the live test database
with the LLM, embedding model, and the contradiction judge stubbed
out. The supersession-resolver branch reads / writes Mongo directly
so the integration coverage proves the wiring is correct end-to-end:

  * The new preference's ``valid_from`` is stamped to the run's wall
    clock; the old preference's ``valid_until`` is stamped to the same
    value.
  * A ``superseded_by`` edge is upserted between the two rows.
  * The deterministic ``has: person:self -> preference`` edge is
    written by the pipeline (the LLM never emits it).
  * Subsequent extractions on the same incoming preference don't
    duplicate the supersession.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.entities.documents import Document, SourceType
from tree.entities.knowledge_graph import EdgeType, NodeType, build_node_id
from tree.entities.ontology import PreferenceCategory
from tree.entities.users import User
from tree.memory.extraction.dedup import DeduplicationResult
from tree.memory.extraction.pipeline import memory_extraction
from tree.memory.query.kgquery import KGQuery
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM

TEST_DATABASE = "integration_tests_twin"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user() -> User:
    user = User(identifier=f"test-supersession-user-{PydanticObjectId()}")
    await user.insert()
    return user


async def _insert_doc(
    *, content: str, user_id: PydanticObjectId, source_uri: str
) -> Document:
    doc = Document(
        title="Supersession E2E",
        content=content,
        source_type=SourceType.HUGGINGFACE,
        source_uri=source_uri,
        user_id=user_id,
        authors=["Test"],
    )
    await doc.insert()
    return doc


class _CannedEmbeddingModel(FakeEmbeddingModel):
    """Returns pre-canned vectors keyed by an input substring."""

    def __init__(self, dimensions: int, mapping: dict[str, list[float]]) -> None:
        super().__init__(dimensions=dimensions)
        self._mapping = mapping

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec: list[float] | None = None
            for key, value in self._mapping.items():
                if key in text:
                    vec = list(value)
                    break
            if vec is None:
                vec = [0.0] * self._dimensions
            out.append(vec)
        return out


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
    # #043: the resolver builds from the RESOLUTION model factory (transient
    # name vector). Point it at the same canned model.
    mocker.patch(
        "tree.memory.extraction.pipeline.get_resolution_embedding_model",
        return_value=embedding_model,
    )
    # #042/#043: task ④ AND the supersession judge embed via the SEARCH model
    # factory; point it at the same canned model so the preference statement
    # vector still comes from the test mapping.
    mocker.patch(
        "tree.memory.extraction.pipeline.get_search_embedding_model",
        return_value=embedding_model,
    )
    # Standard dedup branch always says "no candidates" so we cleanly
    # observe whether the supersession-resolver branch fires.
    mocker.patch(
        "tree.memory.extraction.pipeline.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )
    mocker.patch(
        "tree.memory.extraction.add_entity.dedupe_entity",
        new=AsyncMock(return_value=DeduplicationResult(action="none")),
    )


async def _kg_rows(mongo_client) -> list[dict[str, Any]]:
    return await mongo_client[TEST_DATABASE]["knowledge_graph"].find().to_list()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestPreferenceSupersessionE2E:
    """Preference contradiction fires; bi-temporal columns + edge land."""

    async def test_preference_contradiction_writes_supersession(
        self, mongo_client, mocker
    ) -> None:
        user = await _make_user()

        # Document 1: emit dark-mode preference.
        doc_a = await _insert_doc(
            content="A document expressing the user's UI preferences.",
            user_id=user.id,
            source_uri="https://example.com/pref-dark",
        )
        dark_response = {
            "nodes": [
                {
                    "name": "prefers-dark-mode",
                    "type": "preference",
                    "properties": {
                        "statement": "prefers dark mode",
                        "category": "ui",
                        "strength": "strong",
                    },
                }
            ],
            "edges": [],
        }
        embedding_model = _CannedEmbeddingModel(
            dimensions=8,
            mapping={
                "dark": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "light": [0.95, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            },
        )
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([dark_response]),
            embedding_model=embedding_model,
        )
        # First extraction: no candidate yet -> supersession branch is
        # a no-op. The contradiction judge would have returned True if
        # asked, but it won't be asked (no candidate above threshold).
        mocker.patch(
            "tree.memory.extraction.preference_supersession.judge_contradiction",
            new=AsyncMock(return_value=(True, 0.91)),
        )

        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc_a.id)])

        # The dark-mode preference landed; has edge from self exists.
        rows = await _kg_rows(mongo_client)
        prefs = [r for r in rows if r["kind"] == "node" and r["type"] == "preference"]
        assert len(prefs) == 1
        dark_row = prefs[0]
        assert dark_row["name"] == "prefers-dark-mode"
        # First write is the SETUP for supersession - no valid_from/until yet.
        # (The supersession resolver only writes those columns when a
        # supersession actually fires.)
        assert dark_row.get("valid_until") is None

        # has edge: person:self -> preference
        self_id = build_node_id(user.id, NodeType.PERSON, "self")
        dark_id = build_node_id(user.id, NodeType.PREFERENCE, "prefers-dark-mode")
        has_edges = [
            r
            for r in rows
            if r["kind"] == "edge"
            and r["type"] == EdgeType.HAS.value
            and r["source_node_id"] == self_id
            and r["target_node_id"] == dark_id
        ]
        assert len(has_edges) == 1

        # Document 2: emit light-mode preference (contradiction).
        doc_b = await _insert_doc(
            content="Actually I switched to light mode.",
            user_id=user.id,
            source_uri="https://example.com/pref-light",
        )
        light_response = {
            "nodes": [
                {
                    "name": "prefers-light-mode",
                    "type": "preference",
                    "properties": {
                        "statement": "prefers light mode",
                        "category": "ui",
                        "strength": "moderate",
                    },
                }
            ],
            "edges": [],
        }
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([light_response]),
            embedding_model=embedding_model,
        )
        mocker.patch(
            "tree.memory.extraction.preference_supersession.judge_contradiction",
            new=AsyncMock(return_value=(True, 0.91)),
        )

        t_before = datetime.now(tz=UTC)
        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc_b.id)])
        t_after = datetime.now(tz=UTC)

        # Both preferences exist; light is current, dark is superseded.
        rows = await _kg_rows(mongo_client)
        prefs = {
            r["name"]: r
            for r in rows
            if r["kind"] == "node" and r["type"] == "preference"
        }
        assert set(prefs) == {"prefers-dark-mode", "prefers-light-mode"}

        dark = prefs["prefers-dark-mode"]
        light = prefs["prefers-light-mode"]

        # Old (dark) row was superseded.
        assert dark.get("valid_until") is not None
        assert t_before <= dark["valid_until"] <= t_after

        # New (light) row carries valid_from at the same instant.
        assert light.get("valid_from") is not None
        assert t_before <= light["valid_from"] <= t_after
        assert light.get("valid_until") is None

        # superseded_by edge: light -> dark with reason="contradiction".
        light_id = build_node_id(user.id, NodeType.PREFERENCE, "prefers-light-mode")
        dark_id = build_node_id(user.id, NodeType.PREFERENCE, "prefers-dark-mode")
        sb_edges = [
            r
            for r in rows
            if r["kind"] == "edge"
            and r["type"] == EdgeType.SUPERSEDED_BY.value
            and r["source_node_id"] == light_id
            and r["target_node_id"] == dark_id
        ]
        assert len(sb_edges) == 1
        sb = sb_edges[0]
        assert sb["properties"]["reason"] == "contradiction"
        assert sb["properties"]["judge_confidence"] == pytest.approx(0.91)

        # KGQuery.find_current_preferences returns only the new one.
        current = await KGQuery(user.id).find_current_preferences(
            category=PreferenceCategory.UI
        )
        assert len(current) == 1
        assert current[0].name == "prefers-light-mode"

        # KGQuery.find_preferences_at(<right before supersession>) returns
        # only the dark-mode pref.
        # We pass a timestamp BEFORE the supersession so the dark row's
        # valid_until > ts is satisfied. The dark row has no valid_from
        # set (it was inserted in the FIRST extraction, before any
        # supersession), so the "valid_from <= ts or null" branch hits.
        ts = dark["valid_until"]
        # ts is the supersession instant - dark.valid_until = ts means
        # dark was valid strictly before ts; query at ts-epsilon.
        import datetime as _dt

        snapshot_ts = ts - _dt.timedelta(seconds=1)
        historical = await KGQuery(user.id).find_preferences_at(
            snapshot_ts, category=PreferenceCategory.UI
        )
        assert len(historical) == 1
        assert historical[0].name == "prefers-dark-mode"


@pytest.mark.slow
class TestFactSupersessionE2E:
    """Fact contradiction fires; bi-temporal columns + edge land
    (same resolver branch as preferences)."""

    async def test_fact_contradiction_writes_supersession(
        self, mongo_client, mocker
    ) -> None:
        user = await _make_user()

        # Document 1: correct fact (paris is_capital_of france).
        doc_a = await _insert_doc(
            content="Paris is the capital of France.",
            user_id=user.id,
            source_uri="https://example.com/fact-france",
        )
        france_response = {
            "nodes": [
                {
                    "name": "paris-france",
                    "type": "fact",
                    "properties": {
                        "subject": "paris",
                        "predicate": "is_capital_of",
                        "object": "france",
                    },
                }
            ],
            "edges": [],
        }
        embedding_model = _CannedEmbeddingModel(
            dimensions=8,
            mapping={
                "france": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "brazil": [0.95, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            },
        )
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([france_response]),
            embedding_model=embedding_model,
        )
        mocker.patch(
            "tree.memory.extraction.preference_supersession.judge_contradiction",
            new=AsyncMock(return_value=(True, 0.96)),
        )

        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc_a.id)])

        rows = await _kg_rows(mongo_client)
        facts = [r for r in rows if r["kind"] == "node" and r["type"] == "fact"]
        assert len(facts) == 1
        assert facts[0]["name"] == "paris-france"

        # Document 2: contradictory fact (paris is_capital_of brazil).
        doc_b = await _insert_doc(
            content="An LLM hallucinates Paris being the capital of Brazil.",
            user_id=user.id,
            source_uri="https://example.com/fact-brazil",
        )
        brazil_response = {
            "nodes": [
                {
                    "name": "paris-brazil",
                    "type": "fact",
                    "properties": {
                        "subject": "paris",
                        "predicate": "is_capital_of",
                        "object": "brazil",
                    },
                }
            ],
            "edges": [],
        }
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([brazil_response]),
            embedding_model=embedding_model,
        )
        mocker.patch(
            "tree.memory.extraction.preference_supersession.judge_contradiction",
            new=AsyncMock(return_value=(True, 0.96)),
        )

        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc_b.id)])

        rows = await _kg_rows(mongo_client)
        facts = {
            r["name"]: r for r in rows if r["kind"] == "node" and r["type"] == "fact"
        }
        assert set(facts) == {"paris-france", "paris-brazil"}

        france = facts["paris-france"]
        brazil = facts["paris-brazil"]
        assert france.get("valid_until") is not None
        assert brazil.get("valid_from") is not None
        assert brazil.get("valid_until") is None

        # superseded_by: brazil -> france
        brazil_id = build_node_id(user.id, NodeType.FACT, "paris-brazil")
        france_id = build_node_id(user.id, NodeType.FACT, "paris-france")
        sb_edges = [
            r
            for r in rows
            if r["kind"] == "edge"
            and r["type"] == EdgeType.SUPERSEDED_BY.value
            and r["source_node_id"] == brazil_id
            and r["target_node_id"] == france_id
        ]
        assert len(sb_edges) == 1
        assert sb_edges[0]["properties"]["reason"] == "contradiction"
        assert sb_edges[0]["properties"]["judge_confidence"] == pytest.approx(0.96)


@pytest.mark.slow
class TestPreferenceSupersessionLiveEmbedderE2E:
    """#032 QA fix-1: the supersession trigger MUST fire under the
    project's actual local-dev embedder
    (``sentence-transformers/all-MiniLM-L6-v2``), not just the canned
    >0.85-by-construction vectors used by
    :class:`TestPreferenceSupersessionE2E`.

    Pre-fix-1 the resolver pre-filtered candidates by cosine; under
    MiniLM-L6-v2 the cosine between ``"prefers dark mode"`` and
    ``"prefers light mode"`` is ~0.64 (well below the 0.85
    ``flag_threshold``), so the judge was never invoked end-to-end -
    this test would have failed silently before #032 fix-1.

    This is the regression-pin for the cosine-gate failure the Tester
    observed in ``/tmp/tester_032_live_e2e.py``.
    """

    async def test_dark_then_light_under_real_minilm_embedder(
        self, mongo_client, mocker
    ) -> None:
        from tree.models.sentence_transformer import SentenceTransformerEmbeddingModel

        # Real all-MiniLM-L6-v2; 384-dim, no API key needed.
        real_embedder = SentenceTransformerEmbeddingModel(
            model="sentence-transformers/all-MiniLM-L6-v2",
            dimensions=384,
            device="cpu",
        )
        user = await _make_user()

        # Document 1: dark mode.
        doc_a = await _insert_doc(
            content="I prefer dark mode for editors.",
            user_id=user.id,
            source_uri="https://example.com/pref-dark-live",
        )
        dark_response = {
            "nodes": [
                {
                    "name": "prefers-dark-mode",
                    "type": "preference",
                    "properties": {
                        "statement": "prefers dark mode",
                        "category": "ui",
                        "strength": "strong",
                    },
                }
            ],
            "edges": [],
        }
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([dark_response]),
            embedding_model=real_embedder,
        )
        # First extraction: only one preference exists at the end of
        # this run; no candidate yet, so the judge would not be
        # called regardless.
        judge_mock = mocker.patch(
            "tree.memory.extraction.preference_supersession.judge_contradiction",
            new=AsyncMock(return_value=(True, 0.93)),
        )
        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc_a.id)])
        assert judge_mock.await_count == 0, (
            "no candidate in the partition - judge MUST NOT be called"
        )

        # Document 2: light mode. This is the QA reproducer.
        doc_b = await _insert_doc(
            content="Actually I changed my mind. I prefer light mode now in editors.",
            user_id=user.id,
            source_uri="https://example.com/pref-light-live",
        )
        light_response = {
            "nodes": [
                {
                    "name": "prefers-light-mode",
                    "type": "preference",
                    "properties": {
                        "statement": "prefers light mode",
                        "category": "ui",
                        "strength": "moderate",
                    },
                }
            ],
            "edges": [],
        }
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([light_response]),
            embedding_model=real_embedder,
        )
        judge_mock2 = mocker.patch(
            "tree.memory.extraction.preference_supersession.judge_contradiction",
            new=AsyncMock(return_value=(True, 0.93)),
        )
        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc_b.id)])

        # The judge MUST have been called even though cosine on the
        # real MiniLM embedder is ~0.64 (below pre-fix 0.85 gate).
        assert judge_mock2.await_count >= 1, (
            "supersession resolver MUST call the judge even under low cosine "
            "(MiniLM-L6-v2). This is the #032 fix-1 regression pin."
        )

        # End-state: exactly two preferences in the (user, ui) slice;
        # light is current, dark is superseded; one ``superseded_by``
        # edge new->old; deterministic ``has`` from person:self->light.
        rows = await _kg_rows(mongo_client)
        prefs = {
            r["name"]: r
            for r in rows
            if r["kind"] == "node" and r["type"] == "preference"
        }
        assert set(prefs) == {"prefers-dark-mode", "prefers-light-mode"}, (
            f"expected two preferences, got {sorted(prefs)!r}"
        )
        dark = prefs["prefers-dark-mode"]
        light = prefs["prefers-light-mode"]
        assert dark.get("valid_until") is not None, (
            f"dark MUST be marked superseded; got valid_until={dark.get('valid_until')!r}"
        )
        assert light.get("valid_from") is not None
        assert light.get("valid_until") is None

        # Single ``superseded_by`` light->dark.
        light_id = build_node_id(user.id, NodeType.PREFERENCE, "prefers-light-mode")
        dark_id = build_node_id(user.id, NodeType.PREFERENCE, "prefers-dark-mode")
        sb_edges = [
            r
            for r in rows
            if r["kind"] == "edge"
            and r["type"] == EdgeType.SUPERSEDED_BY.value
            and r["source_node_id"] == light_id
            and r["target_node_id"] == dark_id
        ]
        assert len(sb_edges) == 1

        # ``find_current_preferences`` returns ONLY the light row.
        current = await KGQuery(user.id).find_current_preferences(
            category=PreferenceCategory.UI
        )
        assert [p.name for p in current] == ["prefers-light-mode"]

        # Two ``has`` edges in total: one per preference (the dark
        # ``has`` was upserted in run 1 and persists; the light ``has``
        # is upserted in run 2).
        self_id = build_node_id(user.id, NodeType.PERSON, "self")
        has_edges = [
            r
            for r in rows
            if r["kind"] == "edge"
            and r["type"] == EdgeType.HAS.value
            and r["source_node_id"] == self_id
        ]
        assert {e["target_node_id"] for e in has_edges} == {dark_id, light_id}


@pytest.mark.slow
class TestStrictPreferencePolicyE2E:
    """The deterministic ``has`` edge is written by the pipeline, not
    by the LLM. Pinned end-to-end so a regression that lets the LLM
    emit ``has`` directly fails loudly."""

    async def test_llm_does_not_emit_has_for_preference(
        self, mongo_client, mocker
    ) -> None:
        user = await _make_user()
        doc = await _insert_doc(
            content="I love sushi.",
            user_id=user.id,
            source_uri="https://example.com/pref-sushi",
        )
        # The LLM emits ONE preference - no has edge attempt because
        # the prompt forbids it.
        response = {
            "nodes": [
                {
                    "name": "prefers-sushi",
                    "type": "preference",
                    "properties": {
                        "statement": "prefers sushi",
                        "category": "food",
                    },
                }
            ],
            "edges": [],
        }
        embedding_model = FakeEmbeddingModel(dimensions=8)
        _patch_pipeline_deps(
            mocker,
            mongo_client,
            llm=FakeLLM([response]),
            embedding_model=embedding_model,
        )
        mocker.patch(
            "tree.memory.extraction.preference_supersession.judge_contradiction",
            new=AsyncMock(return_value=(False, 0.0)),
        )

        with prefect_tags("tests"):
            await memory_extraction(user_id=user.id, document_ids=[str(doc.id)])

        rows = await _kg_rows(mongo_client)
        # has edge: person:self -> preference
        self_id = build_node_id(user.id, NodeType.PERSON, "self")
        pref_id = build_node_id(user.id, NodeType.PREFERENCE, "prefers-sushi")
        has_edges = [
            r
            for r in rows
            if r["kind"] == "edge"
            and r["type"] == EdgeType.HAS.value
            and r["source_node_id"] == self_id
            and r["target_node_id"] == pref_id
        ]
        assert len(has_edges) == 1, (
            f"expected exactly one deterministic has edge, got "
            f"{[r['_id'] for r in has_edges]!r}"
        )
