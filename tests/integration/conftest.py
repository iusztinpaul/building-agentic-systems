import pytest

from twin.config.settings import settings
from twin.db import ALL_DOCUMENT_MODELS, init_mongodb

TEST_DATABASE = "integration_tests_twin"


@pytest.fixture(scope="session")
async def mongo_client():
    client = await init_mongodb(settings.mongo.mongo_uri, TEST_DATABASE)

    yield client

    await client.drop_database(TEST_DATABASE)
    await client.close()


@pytest.fixture(autouse=True)
async def _clean_collections(mongo_client):
    yield

    for model in ALL_DOCUMENT_MODELS:
        await model.find_all().delete()
