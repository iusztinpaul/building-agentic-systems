"""Unit tests for the supersession resolver branch (#032).

Tests use an in-memory fake database + a stubbed contradiction judge
to verify the branch logic without hitting MongoDB or Gemini.

Post-QA (#032 fix-1) the resolver no longer pre-filters candidates on
cosine; it judges the K most-recent active candidates in the same
partition and the first contradiction wins. These tests pin that
behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from beanie import PydanticObjectId

from tree.entities.knowledge_graph import EdgeType, NodeType, build_node_id
from tree.memory.extraction.preference_supersession import (
    canonicalize_preference_names,
    resolve_supersessions,
    slugify,
    write_self_has_preference_edges,
)
from tree.memory.types import (
    ExtractedNode,
    ExtractionResult,
    RawExtraction,
    ChunkedDocument,
)
from tree.models.base import BaseEmbeddingModel


_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


# ---------------------------------------------------------------------------
# In-memory fake collection
# ---------------------------------------------------------------------------


class _FakeCollection:
    """Mongo-collection stand-in supporting just the operations the
    supersession resolver uses: ``find`` (returns a cursor with
    ``async for``) and ``update_one`` (with upsert)."""

    def __init__(self, seed_rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        for row in seed_rows or []:
            self.rows[row["_id"]] = dict(row)
        self.updates: list[tuple[dict[str, Any], dict[str, Any], bool]] = []

    def find(self, query: dict[str, Any]) -> "_FakeCursor":
        matched: list[dict[str, Any]] = []
        for row in self.rows.values():
            if _matches(row, query):
                matched.append(dict(row))
        return _FakeCursor(matched)

    async def update_one(
        self,
        filter_: dict[str, Any],
        update: dict[str, Any] | list[dict[str, Any]],
        *,
        upsert: bool = False,
    ) -> None:
        self.updates.append((filter_, update, upsert))
        if "_id" not in filter_:
            return
        target_id = filter_["_id"]
        existing = self.rows.get(target_id)
        new_row = dict(existing) if existing else {"_id": target_id}
        if isinstance(update, dict):
            if "$setOnInsert" in update and existing is None:
                new_row.update(update["$setOnInsert"])
            if "$set" in update:
                for k, v in update["$set"].items():
                    new_row[k] = v
        if existing is not None or upsert:
            self.rows[target_id] = new_row


def _matches(row: dict[str, Any], query: dict[str, Any]) -> bool:
    """Hand-rolled Mongo-query matcher for the queries this module
    actually issues."""

    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(row, sub) for sub in expected):
                return False
            continue
        if isinstance(expected, dict):
            if "$exists" in expected and expected["$exists"] is False:
                if _has_path(row, key):
                    return False
                continue
            if "$exists" in expected and expected["$exists"] is True:
                if not _has_path(row, key):
                    return False
                continue
        actual = _get_path(row, key)
        if isinstance(expected, dict) and expected:
            # Unsupported operator - skip (no tests need it).
            continue
        if actual != expected:
            return False
    return True


def _has_path(row: dict[str, Any], dotted: str) -> bool:
    cur: Any = row
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _get_path(row: dict[str, Any], dotted: str) -> Any:
    cur: Any = row
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


class _FakeCursor:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items

    def __aiter__(self) -> "_FakeCursor":
        self._i = 0
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._i >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._i]
        self._i += 1
        return item


class _FakeDatabase:
    def __init__(self, collection: _FakeCollection) -> None:
        self._collection = collection

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._collection


class _FakeEmbedding(BaseEmbeddingModel):
    """Embedding model that returns a pre-canned vector per input."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    @property
    def dimensions(self) -> int:
        return len(self._vector)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vector) for _ in texts]


class _StubJudgeLLM:
    """Captures judge calls; returns a deterministic verdict.

    Two modes:
      * ``payload=<dict>`` returns the same payload every call.
      * ``payloads=[d1, d2, ...]`` cycles through the list (raises
        IndexError if exhausted, so tests pin the expected call count).
    """

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        payloads: list[dict[str, Any]] | None = None,
    ) -> None:
        assert (payload is None) != (payloads is None), (
            "pass exactly one of payload= or payloads="
        )
        self.payload = payload
        self.payloads = payloads
        self.calls = 0
        self.prompts: list[str] = []

    async def generate_json(
        self, prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        idx = self.calls
        self.calls += 1
        if self.payloads is not None:
            return self.payloads[idx]
        assert self.payload is not None
        return self.payload


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _make_raw(*nodes: ExtractedNode) -> RawExtraction:
    """Build a minimal :class:`RawExtraction` with the given LLM nodes."""

    return RawExtraction(
        document_id=str(PydanticObjectId()),
        source_uri="https://example.com/doc",
        chunked=ChunkedDocument(
            document_id=str(PydanticObjectId()),
            source_uri="https://example.com/doc",
            source_type="huggingface",
            date=None,
            reference_uris=[],
            chunk_texts=[],
            chunk_ids=[],
            structural=ExtractionResult(),
        ),
        extracted=ExtractionResult(nodes=list(nodes)),
    )


def _seed_preference_row(
    *,
    name: str,
    category: str,
    statement: str,
    embedding: list[float],
    user_id: PydanticObjectId = _USER_ID,
    valid_from: datetime | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    pid = build_node_id(user_id, NodeType.PREFERENCE, name)
    return {
        "_id": pid,
        "user_id": user_id,
        "kind": "node",
        "type": NodeType.PREFERENCE.value,
        "name": name,
        "properties": {"statement": statement, "category": category},
        "embedding": embedding,
        "valid_from": valid_from,
        "valid_until": None,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPreferenceSupersessionFires:
    async def test_contradiction_writes_superseded_by_and_valid_until(
        self,
    ) -> None:
        # Arrange: one prior dark-mode preference in the same category.
        existing = _seed_preference_row(
            name="prefers-dark-mode",
            category="ui",
            statement="prefers dark mode",
            embedding=[1.0, 0.0, 0.0],
        )
        collection = _FakeCollection(seed_rows=[existing])
        database = _FakeDatabase(collection)

        # Incoming light-mode preference, embedded close to dark-mode
        # (cosine well above the 0.85 flag threshold).
        new_node = ExtractedNode(
            name="prefers-light-mode",
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers light mode", "category": "ui"},
        )
        raw = _make_raw(new_node)

        judge = _StubJudgeLLM(
            {"is_contradiction": True, "confidence": 0.91, "reasoning": "x"}
        )
        embedding_model = _FakeEmbedding([0.95, 0.05, 0.0])
        now = datetime.now(tz=UTC)

        # Act
        decisions = await resolve_supersessions(
            database=database,
            user_id=_USER_ID,
            llm=judge,  # type: ignore[arg-type]
            embedding_model=embedding_model,
            raws=[raw],
            now=now,
        )

        # Assert: one supersession decision returned + judge called.
        assert judge.calls == 1
        assert len(decisions) == 1
        decision = decisions[0]
        assert decision.superseded is True
        assert decision.old_node_id == existing["_id"]
        assert decision.judge_confidence == pytest.approx(0.91)

        # Old row's valid_until is set to ``now``.
        old_row = collection.rows[existing["_id"]]
        assert old_row["valid_until"] == now

        # New row created with valid_from=now.
        new_id = build_node_id(_USER_ID, NodeType.PREFERENCE, "prefers-light-mode")
        new_row = collection.rows[new_id]
        assert new_row["valid_from"] == now
        assert new_row["valid_until"] is None

        # superseded_by edge: new -> old with reason="contradiction".
        edge_id = f"{new_id}|{EdgeType.SUPERSEDED_BY.value}|{existing['_id']}"
        edge_row = collection.rows[edge_id]
        assert edge_row["type"] == EdgeType.SUPERSEDED_BY.value
        assert edge_row["source_node_id"] == new_id
        assert edge_row["target_node_id"] == existing["_id"]
        assert edge_row["properties"]["reason"] == "contradiction"
        assert edge_row["properties"]["judge_confidence"] == pytest.approx(0.91)
        assert edge_row["properties"]["superseded_at"] == now


class TestPreferenceSupersessionDoesNotFire:
    async def test_judge_says_no_falls_through_to_dedup(self) -> None:
        existing = _seed_preference_row(
            name="prefers-python",
            category="language",
            statement="prefers python",
            embedding=[1.0, 0.0, 0.0],
        )
        collection = _FakeCollection(seed_rows=[existing])
        database = _FakeDatabase(collection)

        # Embedding-close paraphrase.
        new_node = ExtractedNode(
            name="prefers-python-2",
            type=NodeType.PREFERENCE,
            properties={"statement": "really likes python", "category": "language"},
        )
        raw = _make_raw(new_node)

        judge = _StubJudgeLLM(
            {"is_contradiction": False, "confidence": 0.10, "reasoning": "paraphrase"}
        )
        embedding_model = _FakeEmbedding([0.95, 0.05, 0.0])

        decisions = await resolve_supersessions(
            database=database,
            user_id=_USER_ID,
            llm=judge,  # type: ignore[arg-type]
            embedding_model=embedding_model,
            raws=[raw],
        )

        # Judge fired but said "no" - no decision recorded, no
        # supersession written.
        assert judge.calls == 1
        assert decisions == []
        # Old row's valid_until untouched.
        assert collection.rows[existing["_id"]]["valid_until"] is None
        # No new row was inserted under the prospective id.
        new_id = build_node_id(_USER_ID, NodeType.PREFERENCE, "prefers-python-2")
        assert new_id not in collection.rows

    async def test_no_candidates_skips_judge_entirely(self) -> None:
        collection = _FakeCollection()  # Empty.
        database = _FakeDatabase(collection)

        new_node = ExtractedNode(
            name="prefers-tea",
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers tea", "category": "food"},
        )
        raw = _make_raw(new_node)

        judge = _StubJudgeLLM(
            {"is_contradiction": True, "confidence": 1.0, "reasoning": "x"}
        )
        embedding_model = _FakeEmbedding([1.0, 0.0])

        decisions = await resolve_supersessions(
            database=database,
            user_id=_USER_ID,
            llm=judge,  # type: ignore[arg-type]
            embedding_model=embedding_model,
            raws=[raw],
        )

        # No candidates -> judge not called -> no supersession.
        assert judge.calls == 0
        assert decisions == []

    async def test_low_cosine_still_calls_judge(self) -> None:
        """#032 fix-1: the cosine gate is removed; the judge is asked
        on every same-partition candidate. This test pins that an
        orthogonal embedding does NOT short-circuit the judge."""

        existing = _seed_preference_row(
            name="prefers-dark-mode",
            category="ui",
            statement="prefers dark mode",
            embedding=[1.0, 0.0, 0.0],
        )
        collection = _FakeCollection(seed_rows=[existing])
        database = _FakeDatabase(collection)

        # Orthogonal embedding - cosine = 0.0 against the candidate.
        # Pre-fix this would have skipped the judge entirely.
        new_node = ExtractedNode(
            name="prefers-blue",
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers blue", "category": "ui"},
        )
        raw = _make_raw(new_node)

        # Judge says "no" so we still end up with no supersession,
        # but the **judge was called** - that's what we're pinning.
        judge = _StubJudgeLLM(
            {"is_contradiction": False, "confidence": 0.05, "reasoning": "x"}
        )
        embedding_model = _FakeEmbedding([0.0, 1.0, 0.0])

        decisions = await resolve_supersessions(
            database=database,
            user_id=_USER_ID,
            llm=judge,  # type: ignore[arg-type]
            embedding_model=embedding_model,
            raws=[raw],
        )

        assert judge.calls == 1, (
            "judge MUST be called even when cosine is low - the cosine "
            "gate was the QA-failing pre-filter (#032 fix-1)."
        )
        assert decisions == []


class TestSupersessionCandidateCap:
    """#032 fix-1: the resolver hits the judge AT MOST
    ``settings.dedup.supersession_candidate_cap`` times per incoming
    row, even when many same-partition candidates exist."""

    async def test_caps_judge_calls_at_k(self, mocker) -> None:
        # Override the cap to a small number for the test so we don't
        # need to seed 9 rows just to prove a cap of 8.
        mocker.patch(
            "tree.memory.extraction.preference_supersession.settings.dedup."
            "supersession_candidate_cap",
            3,
        )
        # Seed 5 same-partition candidates, all "not contradiction".
        now = datetime.now(tz=UTC)
        seed: list[dict[str, Any]] = []
        for i in range(5):
            seed.append(
                _seed_preference_row(
                    name=f"prefers-x-{i}",
                    category="ui",
                    statement=f"prefers x{i}",
                    embedding=[0.0] * 3,
                    created_at=now - timedelta(seconds=i),
                )
            )
        collection = _FakeCollection(seed_rows=seed)
        database = _FakeDatabase(collection)

        new_node = ExtractedNode(
            name="prefers-y",
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers y", "category": "ui"},
        )
        raw = _make_raw(new_node)
        judge = _StubJudgeLLM(
            {"is_contradiction": False, "confidence": 0.05, "reasoning": "x"}
        )
        embedding_model = _FakeEmbedding([0.1, 0.0, 0.0])

        decisions = await resolve_supersessions(
            database=database,
            user_id=_USER_ID,
            llm=judge,  # type: ignore[arg-type]
            embedding_model=embedding_model,
            raws=[raw],
        )

        assert decisions == []
        assert judge.calls == 3, (
            f"expected at most K=3 judge calls under cap-3 setting; got {judge.calls}"
        )

    async def test_first_contradiction_wins(self, mocker) -> None:
        """If the K candidates are judged most-recent-first and the
        first one returns CONTRADICT, the judge is NOT called for the
        remaining K-1 candidates."""

        mocker.patch(
            "tree.memory.extraction.preference_supersession.settings.dedup."
            "supersession_candidate_cap",
            4,
        )
        now = datetime.now(tz=UTC)
        # Three candidates; most recent is the "fresh" dark-mode one.
        seed = [
            _seed_preference_row(
                name="prefers-stale-1",
                category="ui",
                statement="prefers monokai",
                embedding=[0.0] * 3,
                created_at=now - timedelta(hours=2),
            ),
            _seed_preference_row(
                name="prefers-stale-2",
                category="ui",
                statement="prefers serif fonts",
                embedding=[0.0] * 3,
                created_at=now - timedelta(hours=1),
            ),
            _seed_preference_row(
                name="prefers-dark-mode",
                category="ui",
                statement="prefers dark mode",
                embedding=[0.0] * 3,
                # MOST recent.
                created_at=now,
            ),
        ]
        collection = _FakeCollection(seed_rows=seed)
        database = _FakeDatabase(collection)

        new_node = ExtractedNode(
            name="prefers-light-mode",
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers light mode", "category": "ui"},
        )
        raw = _make_raw(new_node)
        # Only the FIRST call should return contradict; if the resolver
        # keeps going after a contradiction, .payloads[1] would be hit
        # and the test would fail with "payload list exhausted".
        judge = _StubJudgeLLM(
            payloads=[
                {"is_contradiction": True, "confidence": 0.93, "reasoning": "x"},
            ]
        )
        embedding_model = _FakeEmbedding([0.5, 0.0, 0.0])

        decisions = await resolve_supersessions(
            database=database,
            user_id=_USER_ID,
            llm=judge,  # type: ignore[arg-type]
            embedding_model=embedding_model,
            raws=[raw],
        )

        assert judge.calls == 1
        assert len(decisions) == 1
        assert decisions[0].superseded
        # Most recent candidate is the dark-mode one - that must be the
        # superseded row.
        dark_id = build_node_id(_USER_ID, NodeType.PREFERENCE, "prefers-dark-mode")
        assert decisions[0].old_node_id == dark_id
        # Also: the most-recent candidate's statement appears in the
        # judge prompt (proving most-recent-first ordering).
        assert "prefers dark mode" in judge.prompts[0]


class TestPreferenceSupersessionWritePayload:
    """#032 QA finding (b): the new-row supersession upsert MUST
    include ``properties`` + ``embedding`` so the resolver is
    self-sufficient (queryable without ``apply_writes``)."""

    async def test_new_row_upsert_writes_properties_and_embedding(
        self,
    ) -> None:
        existing = _seed_preference_row(
            name="prefers-dark-mode",
            category="ui",
            statement="prefers dark mode",
            embedding=[1.0, 0.0, 0.0],
        )
        collection = _FakeCollection(seed_rows=[existing])
        database = _FakeDatabase(collection)

        new_node = ExtractedNode(
            name="prefers-light-mode",
            type=NodeType.PREFERENCE,
            properties={
                "statement": "prefers light mode",
                "category": "ui",
                "strength": "moderate",
            },
        )
        raw = _make_raw(new_node)
        judge = _StubJudgeLLM(
            {"is_contradiction": True, "confidence": 0.9, "reasoning": "x"}
        )
        # Distinct, non-zero embedding so we can pin the value lands.
        embedding_model = _FakeEmbedding([0.7, 0.7, 0.1])

        await resolve_supersessions(
            database=database,
            user_id=_USER_ID,
            llm=judge,  # type: ignore[arg-type]
            embedding_model=embedding_model,
            raws=[raw],
        )

        new_id = build_node_id(_USER_ID, NodeType.PREFERENCE, "prefers-light-mode")
        new_row = collection.rows[new_id]
        # properties survived the upsert
        assert new_row["properties"]["statement"] == "prefers light mode"
        assert new_row["properties"]["category"] == "ui"
        assert new_row["properties"]["strength"] == "moderate"
        # embedding is the statement embedding (not the slug embedding)
        assert new_row["embedding"] == [0.7, 0.7, 0.1]


class TestFactSupersession:
    async def test_fact_contradiction_writes_superseded_by_same_subject_predicate(
        self,
    ) -> None:
        # Existing fact: Earth orbits Sun.
        existing_id = build_node_id(_USER_ID, NodeType.FACT, "earth-orbits-sun")
        existing = {
            "_id": existing_id,
            "user_id": _USER_ID,
            "kind": "node",
            "type": NodeType.FACT.value,
            "name": "earth-orbits-sun",
            "properties": {
                "subject": "earth",
                "predicate": "orbits",
                "object": "sun",
            },
            "embedding": [1.0, 0.0, 0.0],
            "valid_from": None,
            "valid_until": None,
        }
        collection = _FakeCollection(seed_rows=[existing])
        database = _FakeDatabase(collection)

        # New fact: Earth orbits Mars (same subject + predicate).
        new_node = ExtractedNode(
            name="earth-orbits-mars",
            type=NodeType.FACT,
            properties={
                "subject": "earth",
                "predicate": "orbits",
                "object": "mars",
            },
        )
        raw = _make_raw(new_node)

        judge = _StubJudgeLLM(
            {"is_contradiction": True, "confidence": 0.96, "reasoning": "x"}
        )
        embedding_model = _FakeEmbedding([0.95, 0.05, 0.0])
        now = datetime.now(tz=UTC)

        decisions = await resolve_supersessions(
            database=database,
            user_id=_USER_ID,
            llm=judge,  # type: ignore[arg-type]
            embedding_model=embedding_model,
            raws=[raw],
            now=now,
        )

        assert len(decisions) == 1
        decision = decisions[0]
        assert decision.superseded is True
        assert decision.old_node_id == existing_id

        # Old fact superseded.
        assert collection.rows[existing_id]["valid_until"] == now
        # superseded_by edge present.
        new_id = build_node_id(_USER_ID, NodeType.FACT, "earth-orbits-mars")
        edge_id = f"{new_id}|{EdgeType.SUPERSEDED_BY.value}|{existing_id}"
        assert edge_id in collection.rows
        assert collection.rows[edge_id]["properties"]["reason"] == "contradiction"


class TestDeterministicHasEdgeWriter:
    async def test_writes_has_for_every_preference(self) -> None:
        collection = _FakeCollection()
        database = _FakeDatabase(collection)

        node_a = ExtractedNode(
            name="prefers-dark-mode",
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers dark mode", "category": "ui"},
        )
        node_b = ExtractedNode(
            name="prefers-vegetarian",
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers vegetarian", "category": "food"},
        )
        raw = _make_raw(node_a, node_b)

        count = await write_self_has_preference_edges(
            database=database,
            user_id=_USER_ID,
            raws=[raw],
        )

        assert count == 2
        # Both edges live at deterministic ``_id`` from the self-person.
        self_id = build_node_id(_USER_ID, NodeType.PERSON, "self")
        for pref_name in ("prefers-dark-mode", "prefers-vegetarian"):
            pref_id = build_node_id(_USER_ID, NodeType.PREFERENCE, pref_name)
            edge_id = f"{self_id}|{EdgeType.HAS.value}|{pref_id}"
            assert edge_id in collection.rows
            assert collection.rows[edge_id]["type"] == EdgeType.HAS.value
            assert collection.rows[edge_id]["source_node_id"] == self_id
            assert collection.rows[edge_id]["target_node_id"] == pref_id

    async def test_no_op_when_no_preference_rows(self) -> None:
        collection = _FakeCollection()
        database = _FakeDatabase(collection)

        non_pref = ExtractedNode(
            name="alice",
            type=NodeType.PERSON,
            subtype="individual",
            properties={},
        )
        raw = _make_raw(non_pref)

        count = await write_self_has_preference_edges(
            database=database, user_id=_USER_ID, raws=[raw]
        )

        assert count == 0
        assert collection.rows == {}

    async def test_idempotent_re_run(self) -> None:
        # Two calls in a row produce a single edge row (upsert).
        collection = _FakeCollection()
        database = _FakeDatabase(collection)
        node = ExtractedNode(
            name="prefers-dark-mode",
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers dark mode", "category": "ui"},
        )
        raw = _make_raw(node)

        await write_self_has_preference_edges(
            database=database, user_id=_USER_ID, raws=[raw]
        )
        await write_self_has_preference_edges(
            database=database, user_id=_USER_ID, raws=[raw]
        )

        edges = [r for r in collection.rows.values() if r["kind"] == "edge"]
        assert len(edges) == 1


# ---------------------------------------------------------------------------
# Slugify + canonicalize-names (#032 fix-3)
# ---------------------------------------------------------------------------


class TestSlugify:
    """Pin the deterministic-slug contract used by
    :func:`canonicalize_preference_names`."""

    @pytest.mark.parametrize(
        ("inp", "expected"),
        [
            ("Prefers DARK Mode for editors", "prefers-dark-mode-for-editors"),
            ("prefers-light-mode", "prefers-light-mode"),
            ("I really love café !!", "i-really-love-cafe"),
            ("   ", ""),
            ("", ""),
            ("prefers dark mode", "prefers-dark-mode"),
            ("prefers   multiple    spaces", "prefers-multiple-spaces"),
            ("--leading-and-trailing--", "leading-and-trailing"),
        ],
    )
    def test_known_inputs(self, inp: str, expected: str) -> None:
        assert slugify(inp) == expected

    def test_deterministic(self) -> None:
        for text in (
            "Prefers dark mode",
            "I love café AT NIGHT",
            "Some other statement!?",
        ):
            assert slugify(text) == slugify(text)

    def test_max_len_caps_length(self) -> None:
        long_text = "prefers " + " ".join(f"word{i}" for i in range(30))
        slug = slugify(long_text, max_len=30)
        assert len(slug) <= 30
        # Trimmed on a word boundary.
        assert not slug.endswith("-")


class TestCanonicalizePreferenceNames:
    def test_overwrites_llm_name_with_slug_of_statement(self) -> None:
        node = ExtractedNode(
            name="prefers dark mode for editors",  # sentence form, with spaces
            type=NodeType.PREFERENCE,
            properties={
                "statement": "prefers dark mode for editors",
                "category": "ui",
            },
        )
        raw = _make_raw(node)
        canonicalize_preference_names([raw])
        assert node.name == "prefers-dark-mode-for-editors"

    def test_two_distinct_statements_get_distinct_slugs(self) -> None:
        n1 = ExtractedNode(
            name="ignored",
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers dark mode", "category": "ui"},
        )
        n2 = ExtractedNode(
            name="also-ignored",
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers light mode", "category": "ui"},
        )
        raw = _make_raw(n1, n2)
        canonicalize_preference_names([raw])
        assert n1.name == "prefers-dark-mode"
        assert n2.name == "prefers-light-mode"
        assert n1.name != n2.name

    def test_non_preference_nodes_untouched(self) -> None:
        person = ExtractedNode(
            name="Alice Smith",
            type=NodeType.PERSON,
            subtype="individual",
            properties={},
        )
        raw = _make_raw(person)
        canonicalize_preference_names([raw])
        # Person name MUST NOT be slugified - resolution + canonical
        # logic owns person-name normalization.
        assert person.name == "Alice Smith"

    def test_missing_statement_leaves_name_alone(self) -> None:
        node = ExtractedNode(
            name="some-fallback",
            type=NodeType.PREFERENCE,
            # No 'statement' key.
            properties={"category": "ui"},
        )
        raw = _make_raw(node)
        canonicalize_preference_names([raw])
        assert node.name == "some-fallback"

    def test_idempotent(self) -> None:
        node = ExtractedNode(
            name="prefers dark mode",
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers dark mode", "category": "ui"},
        )
        raw = _make_raw(node)
        canonicalize_preference_names([raw])
        first = node.name
        canonicalize_preference_names([raw])
        assert node.name == first


class TestResolverNameSlugConsistency:
    """End-to-end-in-unit-land: an LLM that emits the SAME statement
    under two different ``name`` shapes (sentence form, then kebab
    form) must converge on the same ``_id`` after
    :func:`canonicalize_preference_names`."""

    async def test_same_statement_different_names_same_id_after_canonicalize(
        self,
    ) -> None:
        n_run_1 = ExtractedNode(
            name="prefers dark mode",  # sentence
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers dark mode", "category": "ui"},
        )
        n_run_2 = ExtractedNode(
            name="prefers-DARK-MODE",  # weird casing / kebab
            type=NodeType.PREFERENCE,
            properties={"statement": "prefers dark mode", "category": "ui"},
        )
        raw_a = _make_raw(n_run_1)
        raw_b = _make_raw(n_run_2)
        canonicalize_preference_names([raw_a])
        canonicalize_preference_names([raw_b])
        assert n_run_1.name == n_run_2.name == "prefers-dark-mode"
        # And the deterministic id agrees too.
        id_a = build_node_id(_USER_ID, NodeType.PREFERENCE, n_run_1.name)
        id_b = build_node_id(_USER_ID, NodeType.PREFERENCE, n_run_2.name)
        assert id_a == id_b
