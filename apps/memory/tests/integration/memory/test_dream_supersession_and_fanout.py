"""Integration tests for the #052 dream supersession flag + scheduled fan-out.

These exercise the parts of #052 that DO NOT need ``$vectorSearch`` (so they
run in CI without the mongot Search Index Management service):

* the flag-gated LLM contradiction-judge supersession sweep, driven over the
  dream's incremental delta via the reused
  ``tree.memory.extraction.preference_supersession.resolve_supersessions``;
* the per-user fan-out parent flow ``dream_consolidation_all_users``.

The supersession path is isolated from the duplicate sweep by seeding the
preference rows with EMPTY embeddings: the duplicate sweep's driving query
requires a non-empty ``embedding`` (so it skips them and never issues a
``$vectorSearch``), while the supersession sweep judges on statement text and
drives them. The LLM + embedding clients are faked for determinism (no real
Gemini / Voyage). The flow's DB plumbing is redirected to the test database
the same way ``test_dream_consolidation.py`` does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.config.app_config import load_app_config
from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.memory.consolidation.dream import (
    dream_consolidation,
    dream_consolidation_all_users,
)
from tree.memory.consolidation.meta_state import load_watermark, record_dream_run
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM

TEST_DATABASE = "integration_tests_twin"
_DIMS = 8

pytestmark = [pytest.mark.slow]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def kg_collection(mongo_client):
    """The ``knowledge_graph`` collection with NO vector index.

    The supersession resolver uses a plain partition ``find`` (no
    ``$vectorSearch``), so we deliberately skip ``ensure_indexes`` — that
    keeps these tests off the mongot dependency and runnable in CI.
    """

    db = mongo_client[TEST_DATABASE]
    col = db["knowledge_graph"]
    yield col
    await db.drop_collection("knowledge_graph")


@pytest.fixture(autouse=True)
def _redirect_flow_db(mocker, mongo_client):
    """Point the flow's ``init_mongodb`` + db name at the test database."""

    mocker.patch(
        "tree.memory.consolidation.dream.init_mongodb",
        return_value=mongo_client,
    )
    mocker.patch(
        "tree.memory.consolidation.dream.settings.mongo.mongo_initdb_database",
        TEST_DATABASE,
    )


def _database(mongo_client):
    return mongo_client[TEST_DATABASE]


@pytest.fixture
def dream_cfg(mocker):
    """Override the dream config block the flow reads via ``_live_app_config``.

    The flow calls ``_live_app_config()`` (a fresh ``load_app_config()``) for
    its gates, so monkeypatching the module-level ``app_config`` singleton has
    no effect. This installer patches ``_live_app_config`` to return a config
    whose ``dream`` block carries the given overrides — the precise seam the
    flow consults.
    """

    def _install(**overrides):
        cfg = load_app_config()
        cfg.dream = cfg.dream.model_copy(update=overrides)
        mocker.patch(
            "tree.memory.consolidation.dream._live_app_config",
            return_value=cfg,
        )
        return cfg

    return _install


def _preference_doc(
    *,
    user_id: PydanticObjectId,
    name: str,
    statement: str,
    category: str,
    updated_at: datetime,
    created_at: datetime | None = None,
) -> dict:
    """A current preference node with NO embedding (skips the duplicate sweep).

    ``_id`` is built the same way ``resolve_supersessions`` re-derives it —
    ``"{user_id}:preference:{normalize(name)}"`` — so the resolver's
    ``exclude_id`` self-skip lines up with the stored row.
    """

    normalized = " ".join(name.strip().lower().split())
    return {
        "_id": f"{user_id}:preference:{normalized}",
        "user_id": user_id,
        "kind": "node",
        "type": NodeType.PREFERENCE.value,
        "name": name,
        "canonical_name": name,
        "subtype": "explicit",
        "properties": {"statement": statement, "category": category},
        "aliases": [],
        "confidence": 1.0,
        "embedding": [],
        "sources": [],
        "merged_into": None,
        "valid_until": None,
        "created_at": created_at or updated_at,
        "updated_at": updated_at,
    }


def _self_person_doc(user_id: PydanticObjectId) -> dict:
    """An active ``person:self`` node — the active-user signal for fan-out."""

    now = datetime.now(UTC)
    return {
        "_id": f"{user_id}:person:self",
        "user_id": user_id,
        "kind": "node",
        "type": NodeType.PERSON.value,
        "subtype": "individual",
        "name": "self",
        "canonical_name": "Self",
        "properties": {"is_active_user": True, "name": "Self"},
        "embedding": [],
        "aliases": [],
        "confidence": 1.0,
        "sources": [],
        "created_at": now,
        "updated_at": now,
    }


@pytest.fixture
def _fake_judge_llm(mocker):
    """Patch the dream module's LLM + embedding factories with fakes.

    The returned installer seeds the :class:`FakeLLM` judge verdicts in call
    order — each ``generate_json`` pops the next canned JSON. The fake
    embedding model returns zero vectors so the resolver's statement-embed
    step is offline.
    """

    llm = FakeLLM(responses=[])

    def _install(responses):
        llm._responses = list(responses)
        llm._call_count = 0
        mocker.patch("tree.memory.consolidation.dream.get_llm", return_value=llm)
        mocker.patch(
            "tree.memory.consolidation.dream.get_search_embedding_model",
            return_value=FakeEmbeddingModel(dimensions=_DIMS),
        )
        return llm

    return _install


# ---------------------------------------------------------------------------
# Supersession flag — ON
# ---------------------------------------------------------------------------


async def test_flag_on_contradiction_supersedes(
    mongo_client, kg_collection, _fake_judge_llm, dream_cfg
) -> None:
    """A contradicting newer preference supersedes the older one via the judge."""

    dream_cfg(enabled=True, enable_supersession_judge=True)
    llm = _fake_judge_llm([{"is_contradiction": True, "confidence": 0.95}])
    user_id = PydanticObjectId()
    last_run = datetime(2026, 5, 10, tzinfo=UTC)
    fresh = last_run + timedelta(days=1)
    old = last_run - timedelta(days=7)

    await record_dream_run(
        database=_database(mongo_client),
        user_id=user_id,
        run_start=last_run,
        last_run_id="seed",
        last_stats={},
    )

    old_id = f"{user_id}:preference:prefers light mode"
    await kg_collection.insert_many(
        [
            _preference_doc(
                user_id=user_id,
                name="prefers dark mode",
                statement="prefers dark mode",
                category="ui",
                updated_at=fresh,
            ),
            _preference_doc(
                user_id=user_id,
                name="prefers light mode",
                statement="prefers light mode",
                category="ui",
                updated_at=old,
                created_at=old,
            ),
        ]
    )

    with prefect_tags("tests"):
        await dream_consolidation(user_id=user_id, dry_run=False)

    # First-contradiction-wins: the judge fired on the single candidate.
    assert llm.call_count == 1

    old_row = await kg_collection.find_one({"_id": old_id})
    assert old_row is not None
    assert old_row.get("valid_until") is not None

    new_id = f"{user_id}:preference:prefers dark mode"
    edge = await kg_collection.find_one(
        {
            "user_id": user_id,
            "kind": "edge",
            "type": EdgeType.SUPERSEDED_BY.value,
            "source_node_id": new_id,
            "target_node_id": old_id,
        }
    )
    assert edge is not None


async def test_flag_on_no_contradiction_keeps_both(
    mongo_client, kg_collection, _fake_judge_llm, dream_cfg
) -> None:
    """Judge says NOT a contradiction ⇒ no supersession write."""

    dream_cfg(enabled=True, enable_supersession_judge=True)
    _fake_judge_llm([{"is_contradiction": False, "confidence": 0.9}])
    user_id = PydanticObjectId()
    last_run = datetime(2026, 5, 10, tzinfo=UTC)
    fresh = last_run + timedelta(days=1)
    old = last_run - timedelta(days=7)

    await record_dream_run(
        database=_database(mongo_client),
        user_id=user_id,
        run_start=last_run,
        last_run_id="seed",
        last_stats={},
    )

    old_id = f"{user_id}:preference:prefers tea"
    await kg_collection.insert_many(
        [
            _preference_doc(
                user_id=user_id,
                name="prefers green tea",
                statement="prefers green tea",
                category="drinks",
                updated_at=fresh,
            ),
            _preference_doc(
                user_id=user_id,
                name="prefers tea",
                statement="prefers tea",
                category="drinks",
                updated_at=old,
                created_at=old,
            ),
        ]
    )

    with prefect_tags("tests"):
        await dream_consolidation(user_id=user_id, dry_run=False)

    old_row = await kg_collection.find_one({"_id": old_id})
    assert old_row.get("valid_until") is None
    edge = await kg_collection.find_one(
        {"user_id": user_id, "kind": "edge", "type": EdgeType.SUPERSEDED_BY.value}
    )
    assert edge is None


# ---------------------------------------------------------------------------
# Supersession flag — OFF (default) + dry_run
# ---------------------------------------------------------------------------


async def test_flag_off_default_constructs_no_llm(
    mongo_client, kg_collection, mocker, dream_cfg
) -> None:
    """Default flag OFF: NO LLM/embedding/resolver touched; no supersession."""

    # Hold the master switch on; leave enable_supersession_judge at its
    # default (False) — the no-cost contract.
    cfg = dream_cfg(enabled=True)
    assert cfg.dream.enable_supersession_judge is False

    mocker.patch(
        "tree.memory.consolidation.dream.get_llm",
        side_effect=AssertionError("get_llm must not be called (flag off)"),
    )
    mocker.patch(
        "tree.memory.consolidation.dream.get_search_embedding_model",
        side_effect=AssertionError("embedding model must not be built (flag off)"),
    )
    mocker.patch(
        "tree.memory.consolidation.dream.resolve_supersessions",
        side_effect=AssertionError("resolver must not be called (flag off)"),
    )

    user_id = PydanticObjectId()
    last_run = datetime(2026, 5, 10, tzinfo=UTC)
    fresh = last_run + timedelta(days=1)
    old = last_run - timedelta(days=7)

    await record_dream_run(
        database=_database(mongo_client),
        user_id=user_id,
        run_start=last_run,
        last_run_id="seed",
        last_stats={},
    )

    await kg_collection.insert_many(
        [
            _preference_doc(
                user_id=user_id,
                name="prefers dark mode",
                statement="prefers dark mode",
                category="ui",
                updated_at=fresh,
            ),
            _preference_doc(
                user_id=user_id,
                name="prefers light mode",
                statement="prefers light mode",
                category="ui",
                updated_at=old,
                created_at=old,
            ),
        ]
    )

    with prefect_tags("tests"):
        await dream_consolidation(user_id=user_id, dry_run=False)

    edge = await kg_collection.find_one(
        {"user_id": user_id, "kind": "edge", "type": EdgeType.SUPERSEDED_BY.value}
    )
    assert edge is None


async def test_flag_on_dry_run_skips_sweep_and_holds_watermark(
    mongo_client, kg_collection, mocker, dream_cfg
) -> None:
    """Flag ON + dry_run=True: sweep skipped, no LLM, no writes, watermark held."""

    dream_cfg(enabled=True, enable_supersession_judge=True)
    mocker.patch(
        "tree.memory.consolidation.dream.get_llm",
        side_effect=AssertionError("get_llm must not be called on a dry run"),
    )
    resolver = mocker.patch(
        "tree.memory.consolidation.dream.resolve_supersessions",
        side_effect=AssertionError("resolver must not be called on a dry run"),
    )

    user_id = PydanticObjectId()
    last_run = datetime(2026, 5, 10, tzinfo=UTC)
    fresh = last_run + timedelta(days=1)
    old = last_run - timedelta(days=7)

    await record_dream_run(
        database=_database(mongo_client),
        user_id=user_id,
        run_start=last_run,
        last_run_id="seed",
        last_stats={},
    )

    old_id = f"{user_id}:preference:prefers light mode"
    await kg_collection.insert_many(
        [
            _preference_doc(
                user_id=user_id,
                name="prefers dark mode",
                statement="prefers dark mode",
                category="ui",
                updated_at=fresh,
            ),
            _preference_doc(
                user_id=user_id,
                name="prefers light mode",
                statement="prefers light mode",
                category="ui",
                updated_at=old,
                created_at=old,
            ),
        ]
    )

    with prefect_tags("tests"):
        report = await dream_consolidation(user_id=user_id, dry_run=True)

    resolver.assert_not_called()
    old_row = await kg_collection.find_one({"_id": old_id})
    assert old_row.get("valid_until") is None
    assert report.watermark_advanced is False
    wm = await load_watermark(database=_database(mongo_client), user_id=user_id)
    assert wm.last_run_at == last_run


# ---------------------------------------------------------------------------
# Scheduled per-user fan-out
# ---------------------------------------------------------------------------


async def test_fan_out_runs_per_active_user(
    mongo_client, kg_collection, mocker, dream_cfg
) -> None:
    """The parent flow enumerates active users and runs the per-user dream each.

    A user with no delta is a clean noop (its per-user run still executes and
    reports zero pairs). ``dream.dry_run`` propagates to every per-user call.
    """

    dream_cfg(enabled=True, dry_run=True)
    user_a = PydanticObjectId()
    user_b = PydanticObjectId()
    await kg_collection.insert_many(
        [_self_person_doc(user_a), _self_person_doc(user_b)]
    )

    # Spy on the per-user dream so we assert fan-out without re-running the
    # whole per-user pipeline (covered by the other tests).
    seen: list[tuple[PydanticObjectId, bool | None]] = []

    async def _fake_dream(*, user_id, dry_run=None):
        seen.append((user_id, dry_run))

    mocker.patch(
        "tree.memory.consolidation.dream.dream_consolidation",
        side_effect=_fake_dream,
    )

    with prefect_tags("tests"):
        stats = await dream_consolidation_all_users()

    assert stats.enabled is True
    assert stats.users_total == 2
    assert stats.succeeded == 2
    assert stats.failed == 0
    assert {uid for uid, _ in seen} == {user_a, user_b}
    # dream.dry_run propagated to every per-user run.
    assert all(dry_run is True for _, dry_run in seen)


async def test_fan_out_disabled_runs_zero_dreams(
    mongo_client, kg_collection, mocker, dream_cfg
) -> None:
    """``dream.enabled=False`` ⇒ the parent flow runs zero per-user dreams."""

    dream_cfg(enabled=False)
    user_a = PydanticObjectId()
    await kg_collection.insert_many([_self_person_doc(user_a)])

    spy = mocker.patch(
        "tree.memory.consolidation.dream.dream_consolidation",
        side_effect=AssertionError("no per-user dream may run when disabled"),
    )

    with prefect_tags("tests"):
        stats = await dream_consolidation_all_users()

    assert stats.enabled is False
    assert stats.users_total == 0
    spy.assert_not_called()


async def test_fan_out_isolates_one_user_failure(
    mongo_client, kg_collection, mocker, dream_cfg
) -> None:
    """One user's per-user failure does not prevent the others from running."""

    dream_cfg(enabled=True)
    good = PydanticObjectId()
    bad = PydanticObjectId()
    await kg_collection.insert_many([_self_person_doc(good), _self_person_doc(bad)])

    ran: list[PydanticObjectId] = []

    async def _fake_dream(*, user_id, dry_run=None):
        ran.append(user_id)
        if user_id == bad:
            raise RuntimeError("tenant blew up")

    mocker.patch(
        "tree.memory.consolidation.dream.dream_consolidation",
        side_effect=_fake_dream,
    )

    with prefect_tags("tests"):
        stats = await dream_consolidation_all_users()

    # Both users attempted; the failure is isolated and recorded.
    assert set(ran) == {good, bad}
    assert stats.users_total == 2
    assert stats.succeeded == 1
    assert stats.failed == 1
    assert str(bad) in stats.failures
