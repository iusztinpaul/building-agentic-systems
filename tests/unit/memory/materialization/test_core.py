from twin.memory.materialization.core import (
    _node_to_text,
    build_materialization_pipeline,
)


class TestBuildMaterializationPipeline:
    def test_returns_list(self):
        pipeline = build_materialization_pipeline()
        assert isinstance(pipeline, list)
        assert len(pipeline) > 0

    def test_starts_with_node_match(self):
        pipeline = build_materialization_pipeline()
        assert pipeline[0] == {"$match": {"kind": "node"}}

    def test_contains_union_with(self):
        pipeline = build_materialization_pipeline()
        union_stages = [s for s in pipeline if "$unionWith" in s]
        assert len(union_stages) == 1

    def test_union_with_has_edge_pipeline(self):
        pipeline = build_materialization_pipeline()
        union = next(s for s in pipeline if "$unionWith" in s)
        edge_pipeline = union["$unionWith"]["pipeline"]
        assert edge_pipeline[0] == {"$match": {"kind": "edge"}}

    def test_ends_with_out(self):
        pipeline = build_materialization_pipeline()
        assert "$out" in pipeline[-1]
        assert pipeline[-1]["$out"] == "knowledge_graph"

    def test_node_group_key(self):
        pipeline = build_materialization_pipeline()
        group_stage = pipeline[1]
        assert "$group" in group_stage
        group_id = group_stage["$group"]["_id"]
        assert group_id == {"name": "$name", "type": "$type"}

    def test_node_project_composite_id(self):
        pipeline = build_materialization_pipeline()
        project_stage = pipeline[2]
        assert "$project" in project_stage
        node_id = project_stage["$project"]["_id"]
        assert node_id == {"$concat": ["$_id.type", ":", "$_id.name"]}

    def test_node_project_preserves_name(self):
        pipeline = build_materialization_pipeline()
        project_stage = pipeline[2]
        assert project_stage["$project"]["name"] == "$_id.name"

    def test_edge_group_key(self):
        pipeline = build_materialization_pipeline()
        union = next(s for s in pipeline if "$unionWith" in s)
        edge_pipeline = union["$unionWith"]["pipeline"]
        group_stage = edge_pipeline[1]
        assert "$group" in group_stage
        group_id = group_stage["$group"]["_id"]
        assert "source_node_id" in group_id
        assert "source_type" in group_id
        assert "target_node_id" in group_id
        assert "target_type" in group_id
        assert "type" in group_id

    def test_edge_project_composite_node_ids(self):
        pipeline = build_materialization_pipeline()
        union = next(s for s in pipeline if "$unionWith" in s)
        edge_pipeline = union["$unionWith"]["pipeline"]
        project_stage = edge_pipeline[2]
        assert "$project" in project_stage
        proj = project_stage["$project"]
        assert proj["source_node_id"] == {
            "$concat": ["$_id.source_type", ":", "$_id.source_node_id"]
        }
        assert proj["target_node_id"] == {
            "$concat": ["$_id.target_type", ":", "$_id.target_node_id"]
        }


class TestNodeToText:
    def test_basic_node(self):
        node = {"_id": "alice", "type": "person", "properties": {"aliases": ["ali"]}}
        text = _node_to_text(node)
        assert "person: alice" in text
        assert "aliases" in text

    def test_node_with_content(self):
        node = {
            "_id": "chunk-0",
            "type": "chunk",
            "properties": {"content": "Hello world", "source_type": "substack"},
        }
        text = _node_to_text(node)
        # Content should appear last.
        assert text.endswith("Hello world")
        assert "source_type" in text

    def test_empty_properties(self):
        node = {"_id": "test", "type": "task", "properties": {}}
        text = _node_to_text(node)
        assert "task: test" in text

    def test_missing_fields(self):
        text = _node_to_text({})
        assert ": " in text
