import pytest

from twin.entities.knowledge_graph import EdgeType, NodeType
from twin.memory.query.nl_query import (
    _replace_embedding_placeholder,
    build_nl_query_system_prompt,
    validate_pipeline,
)
from twin.models.exceptions import PipelineValidationError


class TestValidatePipeline:
    def test_allowed_stages_pass(self):
        pipeline = [
            {"$match": {"kind": "node"}},
            {"$project": {"name": 1}},
            {"$limit": 10},
        ]
        result = validate_pipeline(pipeline)

        # Should have the original stages plus the appended $project for embedding.
        assert any("$match" in s for s in result)
        assert result[-1] == {"$project": {"embedding": 0}}

    def test_blocked_out_stage_raises(self):
        pipeline = [{"$match": {"kind": "node"}}, {"$out": "evil_collection"}]

        with pytest.raises(PipelineValidationError, match="not allowed"):
            validate_pipeline(pipeline)

    def test_blocked_where_stage_raises(self):
        pipeline = [{"$where": "this.kind == 'node'"}]

        with pytest.raises(PipelineValidationError, match="not allowed"):
            validate_pipeline(pipeline)

    def test_blocked_merge_stage_raises(self):
        pipeline = [{"$match": {}}, {"$merge": "other"}]

        with pytest.raises(PipelineValidationError, match="not allowed"):
            validate_pipeline(pipeline)

    def test_lookup_wrong_collection_raises(self):
        pipeline = [
            {"$match": {"kind": "node"}},
            {
                "$lookup": {
                    "from": "evil_collection",
                    "localField": "_id",
                    "foreignField": "source_node_id",
                    "as": "edges",
                }
            },
        ]

        with pytest.raises(PipelineValidationError, match="from"):
            validate_pipeline(pipeline)

    def test_graphlookup_correct_collection_passes(self):
        pipeline = [
            {"$match": {"kind": "node"}},
            {
                "$graphLookup": {
                    "from": "knowledge_graph",
                    "startWith": "$_id",
                    "connectFromField": "target_node_id",
                    "connectToField": "source_node_id",
                    "as": "connected",
                }
            },
            {"$limit": 10},
        ]
        result = validate_pipeline(pipeline)

        assert any("$graphLookup" in s for s in result)

    def test_graphlookup_wrong_collection_raises(self):
        pipeline = [
            {"$match": {"kind": "node"}},
            {
                "$graphLookup": {
                    "from": "other_collection",
                    "startWith": "$_id",
                    "connectFromField": "target_node_id",
                    "connectToField": "source_node_id",
                    "as": "connected",
                }
            },
        ]

        with pytest.raises(PipelineValidationError, match="from"):
            validate_pipeline(pipeline)

    def test_limit_injected_when_missing(self):
        pipeline = [{"$match": {"kind": "node"}}]
        result = validate_pipeline(pipeline, max_results=25)

        limit_stages = [s for s in result if "$limit" in s]
        assert len(limit_stages) == 1
        assert limit_stages[0]["$limit"] == 25

    def test_limit_not_duplicated_when_present(self):
        pipeline = [{"$match": {"kind": "node"}}, {"$limit": 5}]
        result = validate_pipeline(pipeline)

        limit_stages = [s for s in result if "$limit" in s]
        assert len(limit_stages) == 1
        assert limit_stages[0]["$limit"] == 5

    def test_embedding_stripped(self):
        pipeline = [{"$match": {"kind": "node"}}, {"$limit": 10}]
        result = validate_pipeline(pipeline)

        assert result[-1] == {"$project": {"embedding": 0}}

    def test_empty_pipeline_raises(self):
        with pytest.raises(PipelineValidationError, match="empty"):
            validate_pipeline([])

    def test_empty_stage_raises(self):
        with pytest.raises(PipelineValidationError, match="empty"):
            validate_pipeline([{}])

    def test_vector_search_must_be_first(self):
        pipeline = [
            {"$match": {"kind": "node"}},
            {
                "$vectorSearch": {
                    "index": "vector_index",
                    "path": "embedding",
                    "queryVector": [0.1, 0.2],
                    "limit": 10,
                }
            },
        ]

        with pytest.raises(PipelineValidationError, match="first stage"):
            validate_pipeline(pipeline)

    def test_vector_search_at_first_position_passes(self):
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "vector_index",
                    "path": "embedding",
                    "queryVector": [0.1, 0.2],
                    "limit": 10,
                }
            },
            {"$limit": 5},
        ]
        result = validate_pipeline(pipeline)

        assert "$vectorSearch" in result[0]


class TestBuildSystemPrompt:
    def test_contains_all_node_types(self):
        prompt = build_nl_query_system_prompt()

        for nt in NodeType:
            assert nt.value in prompt, f"Missing node type: {nt.value}"

    def test_contains_all_edge_types(self):
        prompt = build_nl_query_system_prompt()

        for et in EdgeType:
            assert et.value in prompt, f"Missing edge type: {et.value}"

    def test_contains_placeholder_instruction(self):
        prompt = build_nl_query_system_prompt()

        assert "__EMBED__" in prompt

    def test_contains_collection_name(self):
        prompt = build_nl_query_system_prompt()

        assert "knowledge_graph" in prompt

    def test_contains_index_info(self):
        prompt = build_nl_query_system_prompt()

        assert "vector_index" in prompt
        assert "text index" in prompt.lower() or "Text index" in prompt


class TestReplaceEmbeddingPlaceholder:
    async def test_replaces_placeholder(self, mocker):
        mock_model = mocker.AsyncMock()
        mock_model.embed.return_value = [[0.1, 0.2, 0.3]]

        pipeline = [
            {
                "$vectorSearch": {
                    "index": "vector_index",
                    "path": "embedding",
                    "queryVector": "__EMBED__",
                    "queryText": "test query",
                    "limit": 10,
                }
            },
            {"$limit": 10},
        ]

        result = await _replace_embedding_placeholder(pipeline, mock_model)

        assert result[0]["$vectorSearch"]["queryVector"] == [0.1, 0.2, 0.3]
        assert "queryText" not in result[0]["$vectorSearch"]
        mock_model.embed.assert_called_once_with(["test query"])

    async def test_no_vector_search_unchanged(self, mocker):
        mock_model = mocker.AsyncMock()

        pipeline = [
            {"$match": {"kind": "node"}},
            {"$limit": 10},
        ]

        result = await _replace_embedding_placeholder(pipeline, mock_model)

        assert result == pipeline
        mock_model.embed.assert_not_called()

    async def test_missing_query_text_raises(self, mocker):
        mock_model = mocker.AsyncMock()

        pipeline = [
            {
                "$vectorSearch": {
                    "index": "vector_index",
                    "path": "embedding",
                    "queryVector": "__EMBED__",
                    "limit": 10,
                }
            },
        ]

        with pytest.raises(PipelineValidationError, match="queryText"):
            await _replace_embedding_placeholder(pipeline, mock_model)
