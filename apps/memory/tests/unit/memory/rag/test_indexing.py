from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, MagicMock

import pytest
from beanie import PydanticObjectId

from tests.unit.conftest import TEST_DATABASE
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.clusters import (
    ClusterCentroid,
    MEMORY_CLUSTERS_COLLECTION,
    MemoryCluster,
)
from tree.entities.memory import ChunkViz, MEMORY_COLLECTION, MemoryEntry, NodeType
from tree.memory.rag.embedding import child_embedding_text
from tree.memory.rag.indexing import (
    _backfill_filter,
    _build_vector_index_definition,
    _CANONICAL_NAME_INDEX,
    _ensure_vector_index,
    _TEXT_INDEX_FIELDS,
    _TEXT_INDEX_NAME,
    _VECTOR_INDEX_FILTER_PATHS,
    VECTOR_INDEX_NAME,
    _VECTOR_INDEX_POLL_S,
    _VECTOR_INDEX_READY_TIMEOUT_S,
    _wait_for_vector_index_ready,
    embed_nodes,
    ensure_indexes,
    index_entry_is_queryable,
    node_embedding_text,
    reset_embeddings,
)
from tree.memory.embedding_text import node_to_embedding_text
from tree.models.base import BaseEmbeddingModel, EmbeddingRole
from tree.models.fake_model import FakeEmbeddingModel


_TEST_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


@pytest.fixture(autouse=True)
def _no_mongot_sync_sleeps(mocker):
    """Zero out the real ``asyncio.sleep`` waits in ``_ensure_vector_index``.

    The production code sleeps 2-3s per call to let mongot process index
    drops/creates — pointless against the mocked collections used here, yet it
    made each test in this file take 3-5 wall-clock seconds (~28s of the suite).

    The stub MUST replace the ``asyncio`` binding ON the core module, not
    ``asyncio.sleep`` itself: ``core.asyncio`` IS the shared asyncio module, so
    patching its ``sleep`` attribute no-ops sleep PROCESS-WIDE for the test's
    duration — any live pymongo periodic executor on the loop then hot-spins
    its zero-interval heartbeat into an AsyncMock (unbounded call history +
    GC churn), intermittently wedging the whole suite for minutes.

    Returns the stub so the readiness tests can assert HOW LONG the poll slept
    without ever waiting that long.
    """

    sleep = AsyncMock()
    mocker.patch(
        "tree.memory.rag.indexing.asyncio",
        new=SimpleNamespace(sleep=sleep),
    )
    return sleep


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


class _AsyncCursorEmpty:
    """Mock async cursor that yields nothing."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def to_list(self):
        return []


class _AsyncCursorWithItem:
    """Mock async cursor that returns one item from to_list."""

    def __init__(self, item: dict):
        self._item = item

    async def to_list(self):
        return [self._item]


class _AsyncCursorFromList:
    """Mock async cursor that iterates a list (async)."""

    def __init__(self, items: list[dict]):
        self._items = list(items)

    def __aiter__(self):
        async def _agen():
            for item in self._items:
                yield item

        return _agen()

    async def to_list(self):
        return list(self._items)


def _make_collection(
    *,
    initial_indexes: list[dict] | None = None,
) -> MagicMock:
    """Build a mock collection with the wait-loop hooks satisfied.

    The first call to ``list_search_indexes()`` (without a name) returns
    the supplied ``initial_indexes`` so the reconcile logic sees the
    desired starting state; subsequent calls (with the index name) return
    a non-empty result so the wait-loop in ``_ensure_vector_index`` exits
    immediately.

    ``index_information`` (used by ``_drop_legacy_compound_indexes``)
    returns an empty dict so the legacy-drop loop is a no-op by default;
    individual tests can override.
    """

    initial = initial_indexes or []
    collection = AsyncMock()
    call_count = 0

    async def _list_search(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _AsyncCursorFromList(initial)
        return _AsyncCursorWithItem({"name": VECTOR_INDEX_NAME})

    collection.list_search_indexes = _list_search
    collection.create_search_index = AsyncMock()
    collection.drop_search_index = AsyncMock()
    collection.index_information = AsyncMock(return_value={})
    collection.drop_index = AsyncMock()
    return collection


def _wire_client(collection: MagicMock) -> MagicMock:
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=collection)
    client = MagicMock()
    client.__getitem__ = MagicMock(return_value=db)
    return client


# ---------------------------------------------------------------------------
# ensure_indexes — index definition shape
# ---------------------------------------------------------------------------


class TestEnsureIndexes:
    async def test_creates_compound_indexes(self) -> None:
        collection = _make_collection()
        client = _wire_client(collection)

        await ensure_indexes(
            client,
            "test_db",
            embedding_model=FakeEmbeddingModel(dimensions=8),
            user_id=_TEST_USER_ID,
        )

        created_names = {
            call.kwargs.get("name") for call in collection.create_index.call_args_list
        }

        # Post-#019: compound indexes carry the ``user_*`` prefix and
        # ``user_id`` as the leading key.
        assert "user_kind_source_node" in created_names
        assert "user_kind_target_node" in created_names
        assert "user_kind_embedding" in created_names
        # Verify the leading-key contract for one of them.
        source_call = next(
            call
            for call in collection.create_index.call_args_list
            if call.kwargs.get("name") == "user_kind_source_node"
        )
        assert source_call.args[0][0] == ("user_id", 1)

    async def test_vector_index_includes_filter_fields(self) -> None:
        """The created vector index must declare ``user_id``, ``kind``,
        ``type``, ``subtype`` AND ``merged_into`` as filter paths so
        $vectorSearch can prune cross-tenant rows, non-child chunks and
        tombstones server-side."""

        collection = _make_collection()
        client = _wire_client(collection)

        await ensure_indexes(
            client,
            "test_db",
            embedding_model=FakeEmbeddingModel(dimensions=8),
            user_id=_TEST_USER_ID,
        )

        collection.create_search_index.assert_awaited_once()
        model = collection.create_search_index.await_args.kwargs["model"]
        fields = model["definition"]["fields"]
        filter_paths = {f["path"] for f in fields if f.get("type") == "filter"}

        assert "user_id" in filter_paths
        assert "kind" in filter_paths
        assert "type" in filter_paths
        # ADR-006 decision 2: the child-only seed search filters on subtype.
        assert "subtype" in filter_paths
        assert "merged_into" in filter_paths

    async def test_vector_index_uses_live_model_dimensions(self) -> None:
        """``numDimensions`` is sourced from the live embedding model, not
        the YAML default."""

        collection = _make_collection()
        client = _wire_client(collection)

        await ensure_indexes(
            client,
            "test_db",
            embedding_model=FakeEmbeddingModel(dimensions=42),
            user_id=_TEST_USER_ID,
        )

        model = collection.create_search_index.await_args.kwargs["model"]
        vector_field = next(
            f for f in model["definition"]["fields"] if f.get("type") == "vector"
        )
        assert vector_field["numDimensions"] == 42

    async def test_canonical_name_index_created(self) -> None:
        """A non-unique, sparse compound (user_id, canonical_name) index
        must be created."""

        collection = _make_collection()
        client = _wire_client(collection)

        await ensure_indexes(
            client,
            "test_db",
            embedding_model=FakeEmbeddingModel(dimensions=8),
            user_id=_TEST_USER_ID,
        )

        canonical_call = next(
            call
            for call in collection.create_index.call_args_list
            if call.kwargs.get("name") == _CANONICAL_NAME_INDEX
        )
        keys = canonical_call.args[0]
        # user_id is the leading key (post-#019).
        assert keys == [("user_id", 1), ("canonical_name", 1)]
        assert canonical_call.kwargs.get("sparse") is True
        assert canonical_call.kwargs.get("unique") is False

    async def test_text_index_covers_top_level_aliases(self) -> None:
        """The text index definition must cover both ``aliases``
        (top-level) and ``properties.aliases`` (legacy/back-compat)."""

        collection = _make_collection()
        client = _wire_client(collection)

        await ensure_indexes(
            client,
            "test_db",
            embedding_model=FakeEmbeddingModel(dimensions=8),
            user_id=_TEST_USER_ID,
        )

        text_call = next(
            call
            for call in collection.create_index.call_args_list
            if call.kwargs.get("name") == _TEXT_INDEX_NAME
        )
        fields = text_call.args[0]
        paths = {path for path, _ in fields}
        assert "aliases" in paths
        assert "properties.aliases" in paths

    async def test_dimension_mismatch_drops_and_recreates_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Existing vector index with a different ``numDimensions`` must
        be dropped + recreated, with a WARNING that names both numbers."""

        existing = {
            "name": VECTOR_INDEX_NAME,
            "latestDefinition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 1536,
                        "similarity": "cosine",
                    },
                    {"type": "filter", "path": "kind"},
                    {"type": "filter", "path": "type"},
                    {"type": "filter", "path": "merged_into"},
                ]
            },
        }
        collection = _make_collection(initial_indexes=[existing])
        client = _wire_client(collection)

        with caplog.at_level("WARNING", logger="tree.memory.rag.indexing"):
            await ensure_indexes(
                client,
                "test_db",
                embedding_model=FakeEmbeddingModel(dimensions=768),
                user_id=_TEST_USER_ID,
            )

        collection.drop_search_index.assert_awaited_once_with(VECTOR_INDEX_NAME)
        collection.create_search_index.assert_awaited_once()

        warning_text = " ".join(
            record.getMessage()
            for record in caplog.records
            if record.levelname == "WARNING"
        )
        assert "1536" in warning_text
        assert "768" in warning_text

    async def test_dimension_match_with_full_filters_is_noop(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the live vector index already has the target dimension AND
        every required filter path, the reconcile logic must NOT drop or
        recreate it (search-index ops only — classic indexes are still
        re-asserted because ``create_index`` is itself idempotent)."""

        existing = {
            "name": VECTOR_INDEX_NAME,
            "latestDefinition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 8,
                        "similarity": "cosine",
                    },
                    {"type": "filter", "path": "user_id"},
                    {"type": "filter", "path": "kind"},
                    {"type": "filter", "path": "type"},
                    {"type": "filter", "path": "subtype"},
                    {"type": "filter", "path": "merged_into"},
                ]
            },
        }
        collection = _make_collection(initial_indexes=[existing])
        client = _wire_client(collection)

        with caplog.at_level("WARNING", logger="tree.memory.rag.indexing"):
            await ensure_indexes(
                client,
                "test_db",
                embedding_model=FakeEmbeddingModel(dimensions=8),
                user_id=_TEST_USER_ID,
            )

        collection.drop_search_index.assert_not_awaited()
        collection.create_search_index.assert_not_awaited()

    async def test_pre_adr006_index_missing_subtype_self_heals(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The live index of an environment provisioned before ADR-006 has every
        filter path EXCEPT ``subtype`` — the next indexing run must quietly drop
        and recreate it (no WARNING; only a dimension mismatch warns)."""

        existing = {
            "name": VECTOR_INDEX_NAME,
            "latestDefinition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 8,
                        "similarity": "cosine",
                    },
                    {"type": "filter", "path": "user_id"},
                    {"type": "filter", "path": "kind"},
                    {"type": "filter", "path": "type"},
                    {"type": "filter", "path": "merged_into"},
                ]
            },
        }
        collection = _make_collection(initial_indexes=[existing])
        client = _wire_client(collection)

        with caplog.at_level("WARNING", logger="tree.memory.rag.indexing"):
            await ensure_indexes(
                client,
                "test_db",
                embedding_model=FakeEmbeddingModel(dimensions=8),
                user_id=_TEST_USER_ID,
            )

        collection.drop_search_index.assert_awaited_once_with(VECTOR_INDEX_NAME)
        collection.create_search_index.assert_awaited_once()
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == []

    async def test_missing_filter_paths_triggers_recreate_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An existing index missing a filter path must be recreated,
        but with no WARNING (only dimension mismatch warns)."""

        existing = {
            "name": VECTOR_INDEX_NAME,
            "latestDefinition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 8,
                        "similarity": "cosine",
                    },
                    {"type": "filter", "path": "kind"},
                    {"type": "filter", "path": "type"},
                ]
            },
        }
        collection = _make_collection(initial_indexes=[existing])
        client = _wire_client(collection)

        with caplog.at_level("WARNING", logger="tree.memory.rag.indexing"):
            await ensure_indexes(
                client,
                "test_db",
                embedding_model=FakeEmbeddingModel(dimensions=8),
                user_id=_TEST_USER_ID,
            )

        collection.drop_search_index.assert_awaited_once_with(VECTOR_INDEX_NAME)
        collection.create_search_index.assert_awaited_once()
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == []


# ---------------------------------------------------------------------------
# _ensure_vector_index — readiness poll (#127)
# ---------------------------------------------------------------------------


class _ScriptedCatalogue:
    """Collection double whose ``$listSearchIndexes`` answers a script.

    Each poll consumes the next catalogue state; the LAST state repeats
    forever, so "never becomes queryable" is a one-state script and
    "BUILDING then READY" is a two-state one. Every probe is recorded, which
    is how the tests count polls.
    """

    def __init__(self, states: list[list[dict]]) -> None:
        self._states = list(states)
        self.probes: list[str | None] = []

    async def list_search_indexes(self, name: str | None = None):
        self.probes.append(name)
        state = self._states[0] if len(self._states) == 1 else self._states.pop(0)
        return _AsyncCursorFromList(state)


def _entry(**fields) -> dict:
    return {"name": VECTOR_INDEX_NAME, **fields}


class TestVectorIndexReadiness:
    """ADR-008: readiness is OBSERVED (``queryable`` / ``status``), not guessed
    from the entry merely existing."""

    async def test_returns_when_queryable(self, caplog) -> None:
        collection = _ScriptedCatalogue([[_entry(status="READY", queryable=True)]])

        with caplog.at_level("INFO", logger="tree.memory.rag.indexing"):
            await _wait_for_vector_index_ready(collection)

        assert len(collection.probes) == 1
        assert f"Vector search index '{VECTOR_INDEX_NAME}' ready (status=READY)" in (
            caplog.text
        )

    async def test_polls_until_queryable(self, _no_mongot_sync_sleeps) -> None:
        collection = _ScriptedCatalogue(
            [
                [_entry(status="BUILDING", queryable=False)],
                [_entry(status="READY", queryable=True)],
            ]
        )

        await _wait_for_vector_index_ready(collection)

        assert len(collection.probes) == 2
        # Exactly ONE wait, of the declared poll interval — a mid-build index
        # must not be polled in a hot loop.
        assert _no_mongot_sync_sleeps.await_args_list == [call(_VECTOR_INDEX_POLL_S)]
        assert _VECTOR_INDEX_POLL_S == 5

    async def test_absent_entry_keeps_polling(self) -> None:
        # mongot has not published the freshly created index yet: "not there"
        # is "not yet", so the poll waits rather than declaring it ready.
        collection = _ScriptedCatalogue([[], [_entry(status="READY", queryable=True)]])

        await _wait_for_vector_index_ready(collection)

        assert len(collection.probes) == 2

    async def test_raises_on_failed(self) -> None:
        collection = _ScriptedCatalogue([[_entry(status="FAILED", queryable=False)]])

        with pytest.raises(RuntimeError) as excinfo:
            await _wait_for_vector_index_ready(collection)

        assert VECTOR_INDEX_NAME in str(excinfo.value)
        assert "status=FAILED" in str(excinfo.value)

    async def test_raises_on_failed_even_when_queryable(self) -> None:
        """FAILED is checked BEFORE ``queryable``.

        The docs allow a FAILED index to report ``queryable: true`` — it is
        then serving the PREVIOUS definition, which right after a recreate is
        the stale-dimensions state, not a success.
        """

        collection = _ScriptedCatalogue([[_entry(status="FAILED", queryable=True)]])

        with pytest.raises(RuntimeError, match="status=FAILED"):
            await _wait_for_vector_index_ready(collection)

    async def test_times_out_fail_open(self, caplog, _no_mongot_sync_sleeps) -> None:
        collection = _ScriptedCatalogue([[_entry(status="BUILDING", queryable=False)]])

        with caplog.at_level("WARNING", logger="tree.memory.rag.indexing"):
            await _wait_for_vector_index_ready(collection)  # fail-open: no raise

        expected_polls = _VECTOR_INDEX_READY_TIMEOUT_S // _VECTOR_INDEX_POLL_S
        assert expected_polls == 60
        assert len(collection.probes) == expected_polls
        assert _no_mongot_sync_sleeps.await_count == expected_polls
        warning = caplog.text
        assert "text_only" in warning
        assert "not queryable after 300 s" in warning
        assert "last status=BUILDING" in warning

    async def test_entry_without_readiness_fields_is_ready(
        self, caplog, _no_mongot_sync_sleeps
    ) -> None:
        """The LOCAL mongot reports NEITHER ``status`` NOR ``queryable``.

        Verified 2026-09-12 with ``mongosh`` against ``docker/mongot``: the
        entry is ``{id, name, type, latestDefinition}``. Waiting for
        ``queryable is True`` would burn the full 300 s cap on every local
        indexing run, so the absence of both fields reads as ready — through
        ``_ensure_vector_index``, to pin the real call site.
        """

        collection = _make_collection()  # its poll answers {"name": ...} only

        with caplog.at_level("INFO", logger="tree.memory.rag.indexing"):
            await _ensure_vector_index(collection, 8)

        assert "treating it as ready" in caplog.text
        _no_mongot_sync_sleeps.assert_not_awaited()


class TestIndexEntryIsQueryable:
    """The shared readiness rule — three-valued, with ``None`` meaning "this
    deployment does not report readiness" (NOT "not ready")."""

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            ({"status": "READY", "queryable": True}, True),
            ({"status": "STALE", "queryable": True}, True),
            ({"status": "BUILDING", "queryable": False}, False),
            ({"status": "PENDING"}, False),
            ({"status": "READY"}, True),
            ({"queryable": True}, True),
            # Local mongot: neither field — undetermined, not "not ready".
            ({"id": "1", "name": VECTOR_INDEX_NAME, "type": "vectorSearch"}, None),
        ],
    )
    def test_truth_table(self, entry: dict, expected: bool | None) -> None:
        assert index_entry_is_queryable(entry) is expected


# ---------------------------------------------------------------------------
# ensure_indexes — module-level constants
# ---------------------------------------------------------------------------


class TestVectorIndexDefinition:
    def test_definition_includes_required_filters(self) -> None:
        defn = _build_vector_index_definition(dimensions=16)
        filter_paths = {f["path"] for f in defn["fields"] if f.get("type") == "filter"}
        # Post-#019: user_id is required as a filter path so $vectorSearch
        # can prune cross-tenant rows server-side.
        assert set(_VECTOR_INDEX_FILTER_PATHS) == {
            "user_id",
            "kind",
            "type",
            "subtype",
            "merged_into",
        }
        assert _VECTOR_INDEX_FILTER_PATHS[0] == "user_id"
        assert set(_VECTOR_INDEX_FILTER_PATHS).issubset(filter_paths)

    def test_text_index_fields_constant(self) -> None:
        paths = {path for path, _ in _TEXT_INDEX_FIELDS}
        assert "name" in paths
        assert "aliases" in paths
        assert "properties.aliases" in paths


# ---------------------------------------------------------------------------
# embed_nodes — backfill semantics
# ---------------------------------------------------------------------------


class _SpyEmbeddingModel(BaseEmbeddingModel):
    """Embedding model that records the texts it was asked to embed."""

    def __init__(self, dimensions: int = 4) -> None:
        self._dimensions = dimensions
        self.calls: list[list[str]] = []
        self.roles: list[EmbeddingRole | None] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
        self.calls.append(list(texts))
        self.roles.append(input_type)
        return [[0.1] * self._dimensions for _ in texts]


class TestBackfillSelection:
    """ADR-006 decision 4: the backfill embeds child chunks + entity nodes ONLY.

    The rule lives in the QUERY, so these assert on the filter the function
    issues — parents and documents are never fetched, let alone embedded.
    """

    def test_filter_is_scoped_to_the_tenant_and_to_unembedded_rows(self) -> None:
        query = _backfill_filter(_TEST_USER_ID)

        assert query["user_id"] == _TEST_USER_ID
        assert query["kind"] == "node"
        assert query["embedding"] == {"$in": [[], None]}

    def test_filter_selects_child_chunks_and_llm_extractable_entities(self) -> None:
        child_branch, entity_branch = _backfill_filter(_TEST_USER_ID)["$or"]

        assert child_branch == {"type": "chunk", "subtype": "child"}
        entity_types = entity_branch["type"]["$in"]
        assert "person" in entity_types
        # Structural rows are not LLM-extractable, so no branch can ever match
        # a document row or a PARENT chunk — both are vector-less by design.
        assert "document" not in entity_types
        assert "chunk" not in entity_types


class TestNodeEmbeddingText:
    def test_child_chunk_embeds_its_contextual_header(self) -> None:
        row = {
            "type": "chunk",
            "subtype": "child",
            "properties": {
                "title": "Memory for AI Agents",
                "heading_path": ["Retrieval", "Parent-document retrieval"],
                "content": "It stores parents without vectors.",
            },
        }

        text = node_embedding_text(row)

        # Rebuilt from the row's OWN denormalised properties — no join, and
        # byte-identical to what the worker's embed_children task produced.
        assert text == child_embedding_text(
            title="Memory for AI Agents",
            heading_path=["Retrieval", "Parent-document retrieval"],
            content="It stores parents without vectors.",
        )
        assert text != "It stores parents without vectors."

    def test_entity_node_embeds_the_generic_node_text(self) -> None:
        row = {"type": "person", "name": "alice", "properties": {"email": "a@b.c"}}

        assert node_embedding_text(row) == node_to_embedding_text(row)

    def test_preference_and_fact_use_statement_and_object(self) -> None:
        """The backfill must embed a stored preference on its STATEMENT.

        After an **Embedding reset** the backfill re-embeds every preference and
        fact; on the generic node-text ("preference: …\\nstatement: …") the
        supersession resolver would compare a statement against a node-text and
        silently stop matching (ADR-009 §7).
        """

        preference = {
            "type": "preference",
            "name": "prefers dark mode",
            "properties": {"statement": "prefers dark mode", "polarity": "like"},
        }
        fact = {
            "type": "fact",
            "name": "lives in Bucharest",
            "properties": {"subject": "paul", "object": "Bucharest"},
        }
        person = {"type": "person", "name": "alice", "properties": {"email": "a@b.c"}}
        blank_preference = {
            "type": "preference",
            "name": "malformed",
            "properties": {"statement": "   "},
        }

        assert node_embedding_text(preference) == "prefers dark mode"
        assert node_embedding_text(fact) == "Bucharest"
        assert node_embedding_text(person) == node_to_embedding_text(person)
        # A blank statement is not embeddable — fall back rather than embed "".
        assert node_embedding_text(blank_preference) == node_to_embedding_text(
            blank_preference
        )


class TestEmbedNodesIsBackfillOnly:
    async def test_skips_nodes_with_non_empty_embedding(self, mocker) -> None:
        """Nodes with a non-empty ``embedding`` must NOT be re-embedded —
        the query filter is ``embedding in [[], None]`` so anything else is
        skipped."""

        # Arrange — only the "empty" node should make it through the query.
        empty_node = {
            "_id": "person:alice",
            "type": "person",
            "kind": "node",
            "properties": {},
            "embedding": [],
        }
        # The filled node is excluded by the query filter — we should never
        # see it in the fetched docs list.
        fetched_docs = [empty_node]

        collection = AsyncMock()
        collection.find = MagicMock(
            return_value=AsyncMock(to_list=AsyncMock(return_value=fetched_docs))
        )

        client = _wire_client(collection)
        spy_model = _SpyEmbeddingModel(dimensions=4)

        embedded = await embed_nodes(client, "test_db", spy_model, _TEST_USER_ID)

        assert embedded == 1
        # The query the function issues is the ONE backfill selection rule.
        assert collection.find.call_args.args[0] == _backfill_filter(_TEST_USER_ID)
        # Only one batch with the single empty-embedding node was embedded.
        assert len(spy_model.calls) == 1
        assert len(spy_model.calls[0]) == 1

    async def test_embeds_the_child_and_the_entity_of_a_mixed_fixture(
        self, mocker
    ) -> None:
        """A fixture with one unembedded child, parent, document and person:
        the child and the person are embedded, on their own texts."""

        child = {
            "_id": "u:chunk:a#parent-0#child-0",
            "kind": "node",
            "type": "chunk",
            "subtype": "child",
            "embedding": [],
            "properties": {
                "title": "Memory for AI Agents",
                "heading_path": ["Retrieval"],
                "content": "children carry the vector",
            },
        }
        person = {
            "_id": "u:person:alice",
            "kind": "node",
            "type": "person",
            "embedding": [],
            "properties": {},
            "name": "alice",
        }
        # The parent and the document rows are excluded by the QUERY, so a
        # correct implementation never even fetches them.
        fetched = [row for row in (child, person)]

        collection = AsyncMock()
        collection.find = MagicMock(
            return_value=AsyncMock(to_list=AsyncMock(return_value=fetched))
        )
        client = _wire_client(collection)
        spy_model = _SpyEmbeddingModel(dimensions=4)

        embedded = await embed_nodes(client, "test_db", spy_model, _TEST_USER_ID)

        assert embedded == 2
        assert spy_model.calls == [
            [
                child_embedding_text(
                    title="Memory for AI Agents",
                    heading_path=["Retrieval"],
                    content="children carry the vector",
                ),
                node_to_embedding_text(person),
            ]
        ]

    async def test_no_docs_no_embed(self) -> None:
        """When every node already has a non-empty embedding, the embed
        call is a complete no-op (no batch call, zero count)."""

        collection = AsyncMock()
        collection.find = MagicMock(
            return_value=AsyncMock(to_list=AsyncMock(return_value=[]))
        )
        client = _wire_client(collection)
        spy_model = _SpyEmbeddingModel(dimensions=4)

        embedded = await embed_nodes(client, "test_db", spy_model, _TEST_USER_ID)

        assert embedded == 0
        assert spy_model.calls == []

    async def test_skipped_placeholder_vector_is_not_persisted(
        self, mocker, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ``[]`` placeholder (Voyage content rejection) must NOT be written.

        The skipped node stays unembedded (``embedding`` untouched, i.e. still
        ``[]``/``None``) so a later backfill run retries it — the resilience
        contract from this change. We assert the bulk_write op set excludes the
        skipped doc and the returned count reflects only the persisted node, and
        that the summary log surfaces the skip gap for ops visibility.
        """

        # Arrange — two fetched docs; the batcher returns a good vector for the
        # first and an empty placeholder for the second (poison content).
        good = {
            "_id": "person:alice",
            "type": "person",
            "kind": "node",
            "embedding": [],
        }
        skipped = {
            "_id": "chunk:poison",
            "type": "chunk",
            "subtype": "child",
            "kind": "node",
            "properties": {"content": "poison"},
            "embedding": [],
        }
        collection = AsyncMock()
        collection.find = MagicMock(
            return_value=AsyncMock(
                to_list=AsyncMock(return_value=[good, skipped]),
            )
        )
        client = _wire_client(collection)
        mocker.patch(
            "tree.memory.rag.indexing.embed_texts",
            new=AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4], []]),
        )

        with caplog.at_level("INFO", logger="tree.memory.rag.indexing"):
            embedded = await embed_nodes(
                client, "test_db", _SpyEmbeddingModel(dimensions=4), _TEST_USER_ID
            )

        # Assert: only the good node is persisted; the poison node is left alone.
        assert embedded == 1
        bulk_ops = collection.bulk_write.call_args.args[0]
        assert len(bulk_ops) == 1
        written_id = bulk_ops[0]._filter["_id"]
        assert written_id == "person:alice"

        # Assert: the summary log surfaces the skipped (retriable) node so the
        # "Embedded N" count is not misread as "all fetched nodes are done".
        summary = next(
            r.getMessage() for r in caplog.records if "Embedded" in r.getMessage()
        )
        assert "(1 skipped, will retry)" in summary


# ---------------------------------------------------------------------------
# Embedding role on the backfill (ADR-009 decision 5)
# ---------------------------------------------------------------------------


class TestEmbeddingRoles:
    """The backfill refills exactly the vectors the inline path writes, so it
    embeds under the SAME **Embedding role**: ``document`` (ADR-009 §5).

    A role-less backfill would leave re-embedded rows in a different corner of
    the embedding space than the inline-written rows — same 1024 dimensions,
    same index, silently worse retrieval.
    """

    async def test_backfill_embeds_as_document(self) -> None:
        person = {
            "_id": "u:person:alice",
            "kind": "node",
            "type": "person",
            "embedding": [],
            "properties": {},
            "name": "alice",
        }
        collection = AsyncMock()
        collection.find = MagicMock(
            return_value=AsyncMock(to_list=AsyncMock(return_value=[person]))
        )
        client = _wire_client(collection)
        spy_model = _SpyEmbeddingModel(dimensions=4)

        await embed_nodes(client, "test_db", spy_model, _TEST_USER_ID)

        assert spy_model.roles == ["document"]


# ---------------------------------------------------------------------------
# The Embedding reset (ADR-009 §7)
# ---------------------------------------------------------------------------
#
# Deliberately NOT mock tests. Every claim here is about what Mongo holds
# afterwards — "the vector is gone", "the map coordinates are gone", "the OTHER
# tenant's row is byte-identical", "a second call writes nothing" — and an
# ``update_many`` double can answer none of them. Rows go in through the
# ``MemoryEntry`` ODM (so the fixtures obey the validators production rows do)
# and come back out through raw pymongo.

_RESET_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
_VECTOR = [0.1, 0.2, 0.3, 0.4]


@pytest.fixture
async def reset_client():
    """A client on the unit-test database; both collections cleared afterwards."""

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(), TEST_DATABASE
    )
    yield client
    database = client[TEST_DATABASE]
    await database[MEMORY_COLLECTION].delete_many({})
    await database[MEMORY_CLUSTERS_COLLECTION].delete_many({})


@pytest.fixture
def tenant() -> PydanticObjectId:
    """A fresh tenant per test — every clause of the reset filter is user-scoped."""

    return PydanticObjectId()


async def _seed_embedded_child(
    user_id: PydanticObjectId, row_id: str, *, clustered: bool = True
) -> None:
    """An embedded **Child chunk**, by default carrying map coordinates."""

    await MemoryEntry(
        id=row_id,
        user_id=user_id,
        kind="node",
        type=NodeType.CHUNK,
        subtype="child",
        name=row_id,
        properties={"content": "a passage", "title": "Memory", "heading_path": ["R"]},
        embedding=list(_VECTOR),
        cluster_id=2 if clustered else None,
        viz=ChunkViz(x=1.0, y=2.0, run_id="run-1") if clustered else None,
        created_at=_RESET_NOW,
        updated_at=_RESET_NOW,
    ).insert()


async def _seed_vectorless_row(
    user_id: PydanticObjectId,
    row_id: str,
    *,
    node_type: NodeType,
    subtype: str | None = None,
) -> None:
    """A row that is vector-less BY DESIGN: a parent chunk or a document."""

    await MemoryEntry(
        id=row_id,
        user_id=user_id,
        kind="node",
        type=node_type,
        subtype=subtype,
        name=row_id,
        properties={"content": "not embedded"},
        embedding=[],
        created_at=_RESET_NOW,
        updated_at=_RESET_NOW,
    ).insert()


async def _seed_embedded_entity(
    user_id: PydanticObjectId,
    row_id: str,
    *,
    node_type: NodeType,
    properties: dict | None = None,
) -> None:
    """An embedded LLM-extractable entity node (person, preference, ...)."""

    await MemoryEntry(
        id=row_id,
        user_id=user_id,
        kind="node",
        type=node_type,
        name=row_id,
        properties=properties or {},
        embedding=list(_VECTOR),
        created_at=_RESET_NOW,
        updated_at=_RESET_NOW,
    ).insert()


async def _seed_user_with_six_embedded_rows(user_id: PydanticObjectId) -> None:
    """3 embedded children + 2 people + 1 preference = 6 resettable rows.

    Plus a parent chunk and a document row, which carry no vector by design and
    must therefore stay invisible to the reset.
    """

    for index in range(3):
        await _seed_embedded_child(user_id, f"{user_id}:chunk:child-{index}")
    await _seed_vectorless_row(
        user_id, f"{user_id}:chunk:parent-0", node_type=NodeType.CHUNK, subtype="parent"
    )
    await _seed_vectorless_row(
        user_id, f"{user_id}:document:doc-0", node_type=NodeType.DOCUMENT
    )
    await _seed_embedded_entity(
        user_id, f"{user_id}:person:alice", node_type=NodeType.PERSON
    )
    await _seed_embedded_entity(
        user_id, f"{user_id}:person:bob", node_type=NodeType.PERSON
    )
    await _seed_embedded_entity(
        user_id,
        f"{user_id}:preference:dark-mode",
        node_type=NodeType.PREFERENCE,
        properties={"statement": "prefers dark mode"},
    )


class TestResetEmbeddings:
    async def test_resets_only_this_users_embedded_rows(
        self, reset_client, tenant
    ) -> None:
        other_tenant = PydanticObjectId()
        await _seed_user_with_six_embedded_rows(tenant)
        await _seed_embedded_child(other_tenant, f"{other_tenant}:chunk:child-0")
        collection = reset_client[TEST_DATABASE][MEMORY_COLLECTION]
        before_other = await collection.find_one({"user_id": other_tenant})

        count = await reset_embeddings(reset_client, TEST_DATABASE, tenant)

        assert count == 6
        rows = await collection.find({"user_id": tenant}).to_list()
        emptied = [row for row in rows if row["_id"] in _resettable_ids(tenant)]
        assert len(emptied) == 6
        assert all(row["embedding"] == [] for row in emptied)
        # The children lose their map coordinates too, so the Embedding map
        # warns instead of drawing points from the old embedding space.
        children = [row for row in emptied if row.get("subtype") == "child"]
        assert len(children) == 3
        assert all(row["cluster_id"] is None for row in children)
        assert all(row["viz"] is None for row in children)
        # Another tenant's row is untouched, byte for byte.
        assert await collection.find_one({"user_id": other_tenant}) == before_other

    async def test_leaves_vectorless_rows_alone(self, reset_client, tenant) -> None:
        """A parent chunk and a document row are vector-less BY DESIGN.

        They fail the eligibility ``$or``, so the reset can never touch them —
        and the backfill would never refill them if it did.
        """

        await _seed_user_with_six_embedded_rows(tenant)
        collection = reset_client[TEST_DATABASE][MEMORY_COLLECTION]
        structural = {"$in": [f"{tenant}:chunk:parent-0", f"{tenant}:document:doc-0"]}
        before = await collection.find({"_id": structural}).to_list()

        await reset_embeddings(reset_client, TEST_DATABASE, tenant)

        assert await collection.find({"_id": structural}).to_list() == before

    async def test_is_idempotent(self, reset_client, tenant) -> None:
        await _seed_user_with_six_embedded_rows(tenant)
        collection = reset_client[TEST_DATABASE][MEMORY_COLLECTION]
        await reset_embeddings(reset_client, TEST_DATABASE, tenant)
        after_first = await collection.find({"user_id": tenant}).to_list()

        count = await reset_embeddings(reset_client, TEST_DATABASE, tenant)

        assert count == 0
        # No write at all, not "a write that changed nothing": ``updated_at``
        # would have moved if the second call had issued its update_many.
        assert await collection.find({"user_id": tenant}).to_list() == after_first

    async def test_dry_run_counts_without_writing(self, reset_client, tenant) -> None:
        await _seed_user_with_six_embedded_rows(tenant)
        collection = reset_client[TEST_DATABASE][MEMORY_COLLECTION]
        before = await collection.find({"user_id": tenant}).sort("_id").to_list()

        count = await reset_embeddings(
            reset_client, TEST_DATABASE, tenant, dry_run=True
        )

        assert count == 6
        assert (
            await collection.find({"user_id": tenant}).sort("_id").to_list() == before
        )

    async def test_reset_rows_are_backfill_eligible(self, reset_client, tenant) -> None:
        """Reset ⊆ backfill: every vector emptied is one the backfill rebuilds.

        The two filters share :func:`_embeddable_row_clause`, so this is a
        by-construction property — asserted live because the cost of being wrong
        is a vector nothing ever refills.
        """

        await _seed_user_with_six_embedded_rows(tenant)
        collection = reset_client[TEST_DATABASE][MEMORY_COLLECTION]

        count = await reset_embeddings(reset_client, TEST_DATABASE, tenant)

        backfill_ids = {
            row["_id"]
            for row in await collection.find(_backfill_filter(tenant)).to_list()
        }
        assert _resettable_ids(tenant) <= backfill_ids
        assert count == len(_resettable_ids(tenant))

    async def test_cluster_rows_are_left_alone(self, reset_client, tenant) -> None:
        """``memory_clusters`` is the next **Clustering run**'s to replace."""

        await _seed_user_with_six_embedded_rows(tenant)
        await MemoryCluster(
            id=f"{tenant}:cluster:2",
            user_id=tenant,
            run_id="run-1",
            cluster_id=2,
            label="Agent memory design",
            summary="How agents remember.",
            keywords=["memory", "agents", "rag"],
            size=3,
            sample_chunk_ids=[f"{tenant}:chunk:child-0"],
            centroid=ClusterCentroid(x=0.0, y=0.0),
            created_at=_RESET_NOW,
        ).insert()
        clusters = reset_client[TEST_DATABASE][MEMORY_CLUSTERS_COLLECTION]
        before = await clusters.find({"user_id": tenant}).to_list()

        await reset_embeddings(reset_client, TEST_DATABASE, tenant)

        assert await clusters.find({"user_id": tenant}).to_list() == before
        assert len(before) == 1


def _resettable_ids(user_id: PydanticObjectId) -> set[str]:
    """The 6 ids ``_seed_user_with_six_embedded_rows`` gives a vector."""

    return {
        *(f"{user_id}:chunk:child-{index}" for index in range(3)),
        f"{user_id}:person:alice",
        f"{user_id}:person:bob",
        f"{user_id}:preference:dark-mode",
    }
