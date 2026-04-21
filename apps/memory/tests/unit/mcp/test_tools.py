from datetime import datetime, timezone

from bson import ObjectId

from twin.mcp.tools import _serialize


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
