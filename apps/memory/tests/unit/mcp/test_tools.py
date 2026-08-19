import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from beanie import PydanticObjectId
from bson import ObjectId

from tree.data.online_pipeline import UrlSource
from tree.mcp.tools import _ingest, _serialize

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


class TestSerialize:
    def test_strips_embedding_field(self):
        docs = [
            {"_id": "person:alice", "name": "Alice", "embedding": [0.1, 0.2]},
            {"_id": "person:bob", "name": "Bob", "embedding": [0.3, 0.4]},
        ]
        result = _serialize(docs)

        assert "embedding" not in result
        assert "Alice" in result
        assert "Bob" in result

    def test_handles_objectid(self):
        oid = ObjectId("507f1f77bcf86cd799439011")
        docs = [{"_id": "person:alice", "source": oid}]
        result = _serialize(docs)

        assert "507f1f77bcf86cd799439011" in result

    def test_handles_datetime(self):
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        docs = [{"_id": "person:alice", "created_at": dt}]
        result = _serialize(docs)

        assert "2024" in result

    def test_empty_list(self):
        result = _serialize([])

        assert result == "[]"

    def test_does_not_mutate_original(self):
        docs = [{"_id": "person:alice", "embedding": [0.1]}]
        _serialize(docs)

        assert "embedding" in docs[0]


class TestIngestTail:
    """``_ingest`` delegates to ``dispatch_online_pipeline`` and serializes to JSON.

    The submit contract itself (status derived from the new flow run, failures
    propagating) is the dispatcher's, covered in ``tests/unit/test_online.py``.
    """

    async def test_merges_dup_extra_into_the_dispatch_result(self, mocker):
        mock_dispatch = mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            return_value={"status": "scheduled", "flow_run_id": "run-1"},
        )

        result = await _ingest(
            UrlSource(uri="https://example.com"),
            user_id=_USER_ID,
            dup_extra={"url": "https://example.com"},
        )

        assert json.loads(result) == {
            "status": "scheduled",
            "flow_run_id": "run-1",
            "url": "https://example.com",
        }
        mock_dispatch.assert_awaited_once_with(
            UrlSource(uri="https://example.com"), _USER_ID
        )
