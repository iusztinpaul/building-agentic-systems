"""Verify ``user_id`` is propagated through every extraction-pipeline entry point.

The Prefect flow ``memory_extract_etl_worker`` declares ``user_id`` as a required,
non-Optional parameter; calling without it raises ``TypeError`` before any
work happens. The same is true for the MCP-side helper
``run_extraction_for_documents`` and the indexing flow ``memory_indexing``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tree.memory.extraction.pipeline import (
    memory_extract_etl_worker,
    run_extraction_for_documents,
)
from tree.memory.indexing.pipeline import memory_indexing


class TestRequiredUserIdAtEntryPoints:
    async def test_memory_extraction_without_user_id_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="user_id"):
            # ``.fn`` is the underlying coroutine; calling it without
            # ``user_id`` reproduces Python's standard signature error.
            await memory_extract_etl_worker.fn(document_ids=["x"])  # type: ignore[call-arg]

    async def test_memory_indexing_without_user_id_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="user_id"):
            await memory_indexing.fn()  # type: ignore[call-arg]

    async def test_run_helper_without_user_id_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="user_id"):
            await run_extraction_for_documents(  # type: ignore[call-arg]
                ["x"],
                client=MagicMock(),
                database_name="test",
            )
