import pytest

from tree.config.settings import settings
from tree.db import init_mongodb

TEST_DATABASE = "unit_tests_twin"


@pytest.fixture(scope="session", autouse=True)
async def _init_beanie():
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(), TEST_DATABASE
    )

    yield

    await client.drop_database(TEST_DATABASE)
    await client.close()
