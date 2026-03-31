from twin.memory.materialization.core import _node_to_text


class TestNodeToText:
    def test_basic_node(self):
        node = {
            "_id": "person:alice",
            "type": "person",
            "properties": {"aliases": ["ali"]},
        }
        text = _node_to_text(node)
        assert "person: person:alice" in text
        assert "aliases" in text

    def test_node_with_content(self):
        node = {
            "_id": "chunk:chunk-0",
            "type": "chunk",
            "properties": {"content": "Hello world", "source_type": "substack"},
        }
        text = _node_to_text(node)
        # Content should appear last.
        assert text.endswith("Hello world")
        assert "source_type" in text

    def test_empty_properties(self):
        node = {"_id": "task:test", "type": "task", "properties": {}}
        text = _node_to_text(node)
        assert "task: task:test" in text

    def test_missing_fields(self):
        text = _node_to_text({})
        assert ": " in text
