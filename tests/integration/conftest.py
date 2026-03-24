import pymongo.errors
import pytest

from twin.config.settings import settings
from twin.db import ALL_DOCUMENT_MODELS, init_mongodb

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
        await test_col.aggregate([{"$listSearchIndexes": {}}]).to_list(length=1)
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
    yield

    for model in ALL_DOCUMENT_MODELS:
        await model.find_all().delete()
