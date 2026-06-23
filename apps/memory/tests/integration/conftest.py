from __future__ import annotations

from dataclasses import dataclass

import pymongo.errors
import pytest
from beanie import PydanticObjectId

from tree.config.settings import settings
from tree.db import ALL_DOCUMENT_MODELS, init_mongodb
from tree.entities.documents import Document, SourceType
from tree.entities.users import User

TEST_DATABASE = "integration_tests_twin"


@pytest.fixture(scope="session")
async def mongo_client():
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(), TEST_DATABASE
    )

    yield client

    await client.drop_database(TEST_DATABASE)
    await client.close()


@pytest.fixture(scope="session")
async def mongot_available(mongo_client) -> bool:
    """Check if the mongot search index service is reachable."""

    db = mongo_client.get_database(TEST_DATABASE)
    test_col = db.get_collection("_mongot_probe")
    try:
        await test_col.insert_one({"_probe": True})
        await test_col.create_search_index(
            {"definition": {"mappings": {"dynamic": True}}, "name": "probe_index"}
        )
    except pymongo.errors.OperationFailure as e:
        if "Search Index Management service" in str(e):
            return False
    finally:
        await db.drop_collection("_mongot_probe")
    return True


@pytest.fixture()
async def _skip_without_mongot(mongot_available) -> None:
    if not mongot_available:
        pytest.skip("mongot search index service is not available")


@pytest.fixture(autouse=True)
async def _clean_collections(mongo_client):
    """Guarantee a clean DB at the START of every integration test.

    Wipes BEFORE the test because the suite shares one session-scoped database
    run sequentially: a teardown-only wipe left every test at the mercy of the
    previous test's teardown completing fully, so a test that asserts on a whole
    collection (``all nodes belong to my user``, ``the self node exists``) would
    intermittently see another tenant's rows or a half-wiped graph. Wiping in
    setup makes each test deterministically start from empty; the session
    ``mongo_client`` fixture drops the whole DB at the end, so no teardown wipe
    is needed.
    """

    for model in ALL_DOCUMENT_MODELS:
        await model.find_all().delete()
    yield


# ---------------------------------------------------------------------------
# Multi-tenancy isolation fixture (Phase-1 acceptance gate)
# ---------------------------------------------------------------------------


@dataclass
class TwoUserContent:
    """Container returned by :func:`two_users_with_content`.

    Holds the two seed users plus the ``Document`` rows the fixture
    inserted for each. The isolation test inspects the documents' ids
    when calling ``memory_extract_etl_worker.fn(document_ids=...)`` and asserts
    no row of either tenant leaks into the other tenant's queries.

    Distinct unique tokens (``antelope``/``amber`` for User A,
    ``badger``/``bramble`` for User B) make it easy to detect leaks: a
    User-A query returning a node whose properties mention ``badger`` is
    a hard failure.
    """

    user_a: User
    user_b: User
    doc_a: Document
    doc_b: Document
    content_a: str
    content_b: str


# These conversation strings deliberately contain distinctive tokens
# that cannot collide across tenants. The tests assert that ``antelope``
# / ``amber`` never appear in User-B query results, and ``badger`` /
# ``bramble`` never appear in User-A results.
_CONTENT_A = (
    "Alice owns the antelope analytics project. "
    "She is responsible for the amber dashboard release."
)
_CONTENT_B = (
    "Bob owns the badger reporting service. "
    "He is responsible for the bramble migration."
)


@pytest.fixture()
async def two_users_with_content(mongo_client) -> TwoUserContent:
    """Seed two users (A, B) with one short, tenant-distinct conversation each.

    The fixture does NOT run extraction or indexing — those steps are
    test-specific and the test that consumes this fixture wires them up
    with the appropriate fakes. The fixture's job is to land valid
    upstream state (the user rows + a per-user ``Document`` row).
    """

    user_a = User(identifier=f"a-{PydanticObjectId()}@example.com")
    user_b = User(identifier=f"b-{PydanticObjectId()}@example.com")
    await user_a.insert()
    await user_b.insert()

    doc_a = Document(
        title="User A conversation",
        content=_CONTENT_A,
        source_type=SourceType.CONVERSATION,
        source_uri=f"conversation://a-{PydanticObjectId()}",
        user_id=user_a.id,
        authors=["Alice"],
    )
    doc_b = Document(
        title="User B conversation",
        content=_CONTENT_B,
        source_type=SourceType.CONVERSATION,
        source_uri=f"conversation://b-{PydanticObjectId()}",
        user_id=user_b.id,
        authors=["Bob"],
    )
    await doc_a.insert()
    await doc_b.insert()

    return TwoUserContent(
        user_a=user_a,
        user_b=user_b,
        doc_a=doc_a,
        doc_b=doc_b,
        content_a=_CONTENT_A,
        content_b=_CONTENT_B,
    )
