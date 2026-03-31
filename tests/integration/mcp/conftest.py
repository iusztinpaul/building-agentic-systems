from unittest.mock import MagicMock

import pytest

TEST_DATABASE = "integration_tests_twin"


@pytest.fixture()
def make_mcp_ctx(mongo_client):
    """Factory fixture: build a minimal MCP Context mock with lifespan_context."""

    def _factory(llm=None, embedding_model=None):
        ctx = MagicMock()
        ctx.lifespan_context = {
            "client": mongo_client,
            "database": TEST_DATABASE,
            "llm": llm,
            "embedding_model": embedding_model,
        }
        return ctx

    return _factory
