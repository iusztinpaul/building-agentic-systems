"""Integration tests for the ``--reset-ontology`` migration path (#033).

The migration talks to live Mongo AND live mongot — step 4.5
(``_ensure_kg_indexes``) calls ``ensure_indexes`` inline, which in turn
calls ``list_search_indexes`` / ``create_search_index`` against mongot.
Tests that reach step 4.5 are therefore marked
``@pytest.mark.requires_mongot`` so CI's
``-m "not requires_mongot"`` selector excludes them (mongot's gRPC
Search Index Management channel is unreliable on GitHub runners — see
``tracker/done/024-ci-skip-mongot-and-simplify.done.md``). The two
short-circuit tests (``test_dry_run_lists_drops_without_writes`` and
``test_aborts_when_seed_user_missing``) exit before step 4.5 so they
need no mongot and run in CI.

These tests are marked ``@pytest.mark.slow`` because each one rebuilds
the ``knowledge_graph`` collection from scratch and exercises the live
``ensure_indexes`` call.

Test surface:

* ``test_dry_run_lists_drops_without_writes`` — pinned by AC #1.
* ``test_reset_ontology_drops_collections_and_recreates_self_person``
  — pinned by AC #3.
* ``test_aborts_when_seed_user_missing`` — pinned by AC #4.
* ``test_reset_ontology_is_idempotent`` — re-running with the same
  ``--identifier`` after a successful reset is a no-op.
* ``test_default_path_unchanged_under_pole_o`` — pinned by AC #2: the
  default Phase-1 bootstrap path is byte-identical post-Phase-3.
"""

from __future__ import annotations

import importlib.util
import logging
import pathlib

import pytest
from beanie import PydanticObjectId

from tree.config.settings import settings
from tree.entities.documents import Document, SourceType
from tree.entities.extraction_audit import ExtractionDroppedField, ExtractionRejection
from tree.entities.knowledge_graph import KnowledgeGraphEntry
from tree.entities.users import User

from tests.integration.conftest import TEST_DATABASE


# ---------------------------------------------------------------------------
# Import the migration script as a module (mirrors the unit-test pattern).
# ---------------------------------------------------------------------------

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[3] / "scripts" / "migrate_multi_tenancy.py"
)
_spec = importlib.util.spec_from_file_location("migrate_multi_tenancy_e2e", _SCRIPT)
_module = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(_module)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _migration_uses_test_database(mocker):
    """Pin :data:`settings.mongo.mongo_initdb_database` to the test DB.

    The migration script reads ``settings.mongo.mongo_initdb_database``
    when calling ``init_mongodb`` and ``ensure_indexes``. These tests
    rely on the integration suite's shared ``TEST_DATABASE``, so we
    override that single field for the duration of the test.
    """

    mocker.patch.object(settings.mongo, "mongo_initdb_database", TEST_DATABASE)


@pytest.fixture(autouse=True)
def _silence_migration_logger():
    """Bubble migration ``INFO`` lines into the captured logs.

    The migration script logs every step at ``INFO``; tests rely on
    ``caplog`` to assert dry-run plan output, so we make sure the
    handlers see the records.
    """

    logger = logging.getLogger(_module.__name__)
    logger.propagate = True
    logger.setLevel(logging.DEBUG)


async def _seed_user(identifier: str) -> User:
    """Insert a fresh seed ``User`` (the after_insert hook lands the
    self-person node)."""

    user = User(identifier=identifier)
    await user.insert()
    return user


async def _seed_legacy_kg_rows(user_id: PydanticObjectId) -> None:
    """Write a handful of pre-Phase-3-shaped rows directly via the
    pymongo collection so they bypass the strict Beanie validator.

    The migration's drop step must wipe these regardless of their
    schema — we're proving the wipe-and-rebuild contract, not the
    on-disk validity of legacy data.
    """

    col = KnowledgeGraphEntry.get_pymongo_collection()
    docs = [
        # A pre-#028 ``task`` standalone node row (no subtype slot).
        {
            "_id": f"{user_id}:task:legacy-task-1",
            "user_id": user_id,
            "kind": "node",
            "type": "task",
            "name": "legacy-task-1",
            "properties": {"content": "legacy"},
        },
        # A pre-#029 ``todo`` edge row (no semantic_type).
        {
            "_id": f"{user_id}:person:legacy|todo|{user_id}:task:legacy-task-1",
            "user_id": user_id,
            "kind": "edge",
            "type": "todo",
            "source_node_id": f"{user_id}:person:legacy",
            "target_node_id": f"{user_id}:task:legacy-task-1",
            "properties": {},
        },
        # A garbage row to prove the drop is unconditional.
        {
            "_id": f"{user_id}:scratch:please-drop-me",
            "user_id": user_id,
            "kind": "node",
            "type": "scratch",
            "name": "please-drop-me",
        },
    ]
    await col.insert_many(docs)


async def _seed_audit_rows(user_id: PydanticObjectId) -> None:
    """Stamp one row in each audit collection so the drop has something
    to wipe."""

    from datetime import UTC, datetime

    await ExtractionRejection(
        user_id=user_id,
        timestamp=datetime.now(UTC),
        rejection_reason="unknown_type",
        raw_row={"type": "legacy_thing"},
    ).insert()
    await ExtractionDroppedField(
        user_id=user_id,
        timestamp=datetime.now(UTC),
        row_type="person",
        dropped_field="legacy_field",
        raw_value=None,
        reason="unknown_field",
    ).insert()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestResetOntologyMigrationE2E:
    """Live end-to-end exercise of the ``--reset-ontology`` path
    against the integration-test Mongo instance.

    Per the AC, each test runs with ``trigger_pipelines=False`` so the
    Prefect side-effect is not part of the assertion surface — the
    pipelines are tested independently and the migration's contract is
    "drops happen + self-person re-created + indexes ensured".
    """

    async def test_dry_run_lists_drops_without_writes(
        self, mongo_client, caplog
    ) -> None:
        """AC #1: ``--dry-run --reset-ontology`` against a populated DB
        prints the row counts that WOULD be dropped, makes no writes."""

        user = await _seed_user(f"dry-run-{PydanticObjectId()}@example.com")
        await _seed_legacy_kg_rows(user.id)
        await _seed_audit_rows(user.id)

        # Snapshot pre-state for the post-condition.
        kg_col = KnowledgeGraphEntry.get_pymongo_collection()
        pre_kg_count = await kg_col.count_documents({})
        pre_rejection_count = (
            await ExtractionRejection.get_pymongo_collection().count_documents({})
        )
        pre_dropped_count = (
            await ExtractionDroppedField.get_pymongo_collection().count_documents({})
        )
        assert pre_kg_count >= 3, "fixture wrote 3 legacy rows + self-person"
        assert pre_rejection_count >= 1
        assert pre_dropped_count >= 1

        with caplog.at_level(logging.INFO, logger=_module.__name__):
            result = await _module._run_migration(
                identifier=user.identifier,
                name=None,
                dry_run=True,
                trigger_pipelines=False,
                reset_ontology=True,
            )

        # The dry-run plan is the seed user (live; not synthesized).
        assert result.id == user.id

        # Counts are echoed in the plan output. We assert the
        # would-drop lines name the right collection so the
        # operator can correlate the report with the live state.
        messages = "\n".join(rec.message for rec in caplog.records)
        assert "DRY RUN" in messages
        assert f"DROP knowledge_graph (current row count: {pre_kg_count})" in messages
        assert (
            f"DROP extraction_rejections (current row count: {pre_rejection_count})"
            in messages
        )
        assert (
            "DROP extraction_dropped_fields (current row count: "
            f"{pre_dropped_count})" in messages
        )

        # POST-CONDITION: nothing changed.
        assert await kg_col.count_documents({}) == pre_kg_count
        assert (
            await ExtractionRejection.get_pymongo_collection().count_documents({})
            == pre_rejection_count
        )
        assert (
            await ExtractionDroppedField.get_pymongo_collection().count_documents({})
            == pre_dropped_count
        )

    @pytest.mark.requires_mongot
    async def test_reset_ontology_drops_collections_and_recreates_self_person(
        self, mongo_client
    ) -> None:
        """AC #3: live ``--reset-ontology`` drops the three collections,
        re-creates ``person:self`` under the POLE+O shape, ensures the
        Phase-3 partial index is present."""

        user = await _seed_user(f"reset-{PydanticObjectId()}@example.com")
        await _seed_legacy_kg_rows(user.id)
        await _seed_audit_rows(user.id)

        # The seed user's after_insert hook landed the self-person too;
        # that ROW (not the legacy ones) is the post-migration survivor.
        kg_col = KnowledgeGraphEntry.get_pymongo_collection()
        assert await kg_col.count_documents({}) >= 4  # 3 legacy + self-person

        result = await _module._run_migration(
            identifier=user.identifier,
            name=None,
            dry_run=False,
            trigger_pipelines=False,
            reset_ontology=True,
        )

        assert result.id == user.id

        # POST-CONDITION 1: legacy rows are gone, only person:self
        # remains under the POLE+O shape.
        kg_rows = await kg_col.find({}).to_list()
        assert len(kg_rows) == 1, (
            f"expected only the recreated person:self after reset, got "
            f"{[r['_id'] for r in kg_rows]!r}"
        )
        self_row = kg_rows[0]
        assert self_row["_id"] == f"{user.id}:person:self"
        assert self_row["type"] == "person"
        # POLE+O subtype slot is filled by the after_insert hook (#028).
        assert self_row.get("subtype") == "individual"
        assert self_row["properties"].get("is_active_user") is True
        assert self_row["user_id"] == user.id

        # POST-CONDITION 2: audit collections are empty.
        assert (
            await ExtractionRejection.get_pymongo_collection().count_documents({}) == 0
        )
        assert (
            await ExtractionDroppedField.get_pymongo_collection().count_documents({})
            == 0
        )

        # POST-CONDITION 3: the Phase-3 partial index
        # ``user_type_semantic_type`` is present after ``ensure_indexes``.
        index_info = await kg_col.index_information()
        assert "user_type_semantic_type" in index_info, (
            f"expected user_type_semantic_type partial index after "
            f"reset; got {sorted(index_info)!r}"
        )

    async def test_aborts_when_seed_user_missing(self, mongo_client) -> None:
        """AC #4: ``--reset-ontology --identifier=<missing>`` aborts
        with a clear error pointing operators at the bootstrap path."""

        ghost_identifier = f"ghost-{PydanticObjectId()}@example.com"
        # Pin: the ghost user does not exist in this test database.
        assert await User.find_one(User.identifier == ghost_identifier) is None

        with pytest.raises(_module.MigrationAbort) as exc:
            await _module._run_migration(
                identifier=ghost_identifier,
                name=None,
                dry_run=False,
                trigger_pipelines=False,
                reset_ontology=True,
            )

        message = str(exc.value)
        assert ghost_identifier in message
        # The error must instruct the operator to run the bootstrap
        # path first; otherwise they cannot recover.
        assert "without --reset-ontology" in message

    @pytest.mark.requires_mongot
    async def test_reset_ontology_is_idempotent(self, mongo_client) -> None:
        """Re-running ``--reset-ontology`` is a no-op for the user-facing
        state.

        First run: legacy rows -> single person:self.
        Second run: starts with only person:self -> still only person:self.
        Both runs return the same seed user.
        """

        user = await _seed_user(f"idemp-{PydanticObjectId()}@example.com")
        await _seed_legacy_kg_rows(user.id)

        first = await _module._run_migration(
            identifier=user.identifier,
            name=None,
            dry_run=False,
            trigger_pipelines=False,
            reset_ontology=True,
        )
        kg_col = KnowledgeGraphEntry.get_pymongo_collection()
        first_rows = await kg_col.find({}).to_list()
        assert len(first_rows) == 1
        first_self = first_rows[0]

        second = await _module._run_migration(
            identifier=user.identifier,
            name=None,
            dry_run=False,
            trigger_pipelines=False,
            reset_ontology=True,
        )
        second_rows = await kg_col.find({}).to_list()
        assert len(second_rows) == 1
        second_self = second_rows[0]

        assert first.id == second.id == user.id
        # Same logical row both runs (re-created under the same _id).
        assert first_self["_id"] == second_self["_id"]
        assert second_self.get("subtype") == "individual"

    @pytest.mark.requires_mongot
    async def test_default_path_unchanged_under_pole_o(self, mongo_client) -> None:
        """AC #2: the default (Phase-1 bootstrap) path is byte-identical
        post-Phase-3.

        Seed a fresh document with no user_id; run the default path;
        assert the document was backfilled and a single person:self
        node exists in the KG. The point of this test is to catch a
        future refactor that accidentally re-routes the default path
        through the reset-ontology helper.
        """

        identifier = f"bootstrap-{PydanticObjectId()}@example.com"
        # Insert a Document with no user_id (legacy shape) via the raw
        # pymongo collection so the Beanie required-field check doesn't
        # bite. The Phase-1 migration is exactly designed to backfill
        # this column.
        doc_col = Document.get_pymongo_collection()
        legacy_doc = {
            "title": "legacy",
            "content": "Some legacy content.",
            "source_type": SourceType.HUGGINGFACE.value,
            "source_uri": f"https://example.com/{PydanticObjectId()}",
            "authors": ["legacy"],
            "metadata": {},
        }
        insert_result = await doc_col.insert_one(legacy_doc)
        legacy_doc_id = insert_result.inserted_id

        result = await _module._run_migration(
            identifier=identifier,
            name="Bootstrap User",
            dry_run=False,
            trigger_pipelines=False,
            reset_ontology=False,
        )

        # Phase-1 contract: seed user created; document backfilled with
        # the seed user_id; KG contains exactly the self-person row.
        seed_user = await User.find_one(User.identifier == identifier)
        assert seed_user is not None
        assert seed_user.id == result.id

        backfilled = await doc_col.find_one({"_id": legacy_doc_id})
        assert backfilled is not None
        assert backfilled["user_id"] == seed_user.id

        kg_col = KnowledgeGraphEntry.get_pymongo_collection()
        kg_rows = await kg_col.find({}).to_list()
        # Same expectation as Phase-1 in the post-Phase-3 schema.
        assert len(kg_rows) == 1
        self_row = kg_rows[0]
        assert self_row["_id"] == f"{seed_user.id}:person:self"
        # POLE+O subtype slot is now expected on the freshly-created
        # self-person row (regression check on #028's User.after_insert
        # hook).
        assert self_row.get("subtype") == "individual"
