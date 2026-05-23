from unittest.mock import AsyncMock

import pytest

from tree.config.settings import settings
from tree.db import init_mongodb

TEST_DATABASE = "unit_tests_twin"


@pytest.fixture(autouse=True)
def _noop_voyage_rate_limit(mocker) -> None:
    """No-op the shared Voyage ``rate_limit`` for unit tests (no Prefect server).

    Both Voyage clients (ADR-002 §1) await
    ``rate_limit("voyage-embeddings", strict=False)`` immediately before every
    real network POST. With ``strict=False`` a missing limit is already a no-op,
    but the call still spends ~3s trying to reach a Prefect server that unit
    boxes don't run. Stub it in BOTH client modules so unit tests stay fast and
    server-independent. Tests that assert on the limiter re-patch the same
    per-module target locally with a spy, which transparently overrides this
    autouse stub for their duration.
    """

    mocker.patch("tree.models.voyage_embedding.rate_limit", new_callable=AsyncMock)
    mocker.patch(
        "tree.models.voyage_multimodal_embedding.rate_limit", new_callable=AsyncMock
    )


@pytest.fixture(scope="session", autouse=True)
async def _init_beanie():
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(), TEST_DATABASE
    )

    yield

    await client.drop_database(TEST_DATABASE)
    await client.close()
