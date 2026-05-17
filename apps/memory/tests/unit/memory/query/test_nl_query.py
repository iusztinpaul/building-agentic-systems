import pytest
from beanie import PydanticObjectId
from pymongo.errors import OperationFailure

from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.memory.query.nl_query import (
    _replace_embedding_placeholder,
    build_nl_query_system_prompt,
    execute_nl_query,
    nl_to_pipeline,
    validate_pipeline,
)
from tree.models.exceptions import PipelineValidationError

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


class TestValidatePipeline:
    def test_allowed_stages_pass(self):
        pipeline = [
            {"$match": {"kind": "node"}},
            {"$project": {"name": 1}},
            {"$limit": 10},
        ]
        result = validate_pipeline(pipeline, _USER_ID)

        # Should have the original stages plus the appended $project for embedding.
        assert any("$match" in s for s in result)
        assert result[-1] == {"$project": {"embedding": 0}}

    def test_blocked_out_stage_raises(self):
        pipeline = [{"$match": {"kind": "node"}}, {"$out": "evil_collection"}]

        with pytest.raises(PipelineValidationError, match="not allowed"):
            validate_pipeline(pipeline, _USER_ID)

    def test_blocked_where_stage_raises(self):
        pipeline = [{"$where": "this.kind == 'node'"}]

        with pytest.raises(PipelineValidationError, match="not allowed"):
            validate_pipeline(pipeline, _USER_ID)

    def test_blocked_merge_stage_raises(self):
        pipeline = [{"$match": {}}, {"$merge": "other"}]

        with pytest.raises(PipelineValidationError, match="not allowed"):
            validate_pipeline(pipeline, _USER_ID)

    def test_lookup_stage_rejected(self):
        """Blocker #1 — ``$lookup`` is no longer in the allow-list.

        ``$lookup`` could carry a sub-pipeline that bypasses ``_inject_user_id``
        (which only walks the top-level stages). Removing it from the
        allow-list eliminates the cross-tenant-join leak surface. Realistic
        joins use ``$graphLookup`` (already user-scoped via
        ``restrictSearchWithMatch``) instead.
        """

        pipeline = [
            {"$match": {"kind": "node"}},
            {
                "$lookup": {
                    "from": "knowledge_graph",
                    "pipeline": [{"$match": {"kind": "edge"}}],
                    "as": "x",
                }
            },
        ]

        with pytest.raises(PipelineValidationError, match="not allowed"):
            validate_pipeline(pipeline, _USER_ID)

    def test_lookup_with_localfield_also_rejected(self):
        """``$lookup`` is rejected whether or not it uses a sub-pipeline."""

        pipeline = [
            {"$match": {"kind": "node"}},
            {
                "$lookup": {
                    "from": "knowledge_graph",
                    "localField": "_id",
                    "foreignField": "source_node_id",
                    "as": "edges",
                }
            },
        ]

        with pytest.raises(PipelineValidationError, match="not allowed"):
            validate_pipeline(pipeline, _USER_ID)

    def test_facet_stage_rejected(self):
        """Blocker #1 (cycle 2) — ``$facet`` is no longer in the allow-list.

        ``$facet`` carries one sub-pipeline per output field. Each sub-pipeline
        can include any stage (including ``$lookup``), and ``_inject_user_id``
        only walks the top-level outer pipeline. A planted shape of
        ``[$match, $facet{leaked: [$lookup{pipeline: [$match {kind: edge}]}]}]``
        was demonstrated to leak the other tenant's documents at runtime.
        Removing ``$facet`` from the allow-list closes the leak surface
        without forcing recursive sub-pipeline validation.
        """

        pipeline = [
            {"$match": {"kind": "node"}},
            {
                "$facet": {
                    "leaked": [
                        {
                            "$lookup": {
                                "from": "knowledge_graph",
                                "pipeline": [{"$match": {"kind": "edge"}}],
                                "as": "all",
                            }
                        },
                        {"$limit": 5},
                    ]
                }
            },
            {"$limit": 5},
        ]

        with pytest.raises(PipelineValidationError, match="not allowed"):
            validate_pipeline(pipeline, _USER_ID)

    def test_unionwith_stage_rejected(self):
        """``$unionWith`` carries a sub-pipeline that targets a collection.

        Even if the union targets ``knowledge_graph`` itself, the inner
        ``pipeline`` is not walked by ``_inject_user_id``, so the union would
        merge in documents from every tenant. Belt-and-braces: ``$unionWith``
        was already absent from the allow-list, but the regression test
        pins that decision so a future maintainer can't quietly add it.
        """

        pipeline = [
            {"$match": {"kind": "node"}},
            {
                "$unionWith": {
                    "coll": "knowledge_graph",
                    "pipeline": [{"$match": {"kind": "edge"}}],
                }
            },
        ]

        with pytest.raises(PipelineValidationError, match="not allowed"):
            validate_pipeline(pipeline, _USER_ID)

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
        result = validate_pipeline(pipeline, _USER_ID)

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
            validate_pipeline(pipeline, _USER_ID)

    def test_limit_injected_when_missing(self):
        pipeline = [{"$match": {"kind": "node"}}]
        result = validate_pipeline(pipeline, _USER_ID, max_results=25)

        limit_stages = [s for s in result if "$limit" in s]
        assert len(limit_stages) == 1
        assert limit_stages[0]["$limit"] == 25

    def test_limit_not_duplicated_when_present(self):
        pipeline = [{"$match": {"kind": "node"}}, {"$limit": 5}]
        result = validate_pipeline(pipeline, _USER_ID)

        limit_stages = [s for s in result if "$limit" in s]
        assert len(limit_stages) == 1
        assert limit_stages[0]["$limit"] == 5

    def test_existing_limit_clamped_to_max_results(self):
        pipeline = [{"$match": {"kind": "node"}}, {"$limit": 100}]
        result = validate_pipeline(pipeline, _USER_ID, max_results=5)

        limit_stages = [s for s in result if "$limit" in s]
        assert len(limit_stages) == 1
        assert limit_stages[0]["$limit"] == 5

    def test_existing_limit_below_max_results_unchanged(self):
        pipeline = [{"$match": {"kind": "node"}}, {"$limit": 3}]
        result = validate_pipeline(pipeline, _USER_ID, max_results=10)

        limit_stages = [s for s in result if "$limit" in s]
        assert len(limit_stages) == 1
        assert limit_stages[0]["$limit"] == 3

    def test_embedding_stripped(self):
        pipeline = [{"$match": {"kind": "node"}}, {"$limit": 10}]
        result = validate_pipeline(pipeline, _USER_ID)

        assert result[-1] == {"$project": {"embedding": 0}}

    def test_empty_pipeline_raises(self):
        with pytest.raises(PipelineValidationError, match="empty"):
            validate_pipeline([], _USER_ID)

    def test_empty_stage_raises(self):
        with pytest.raises(PipelineValidationError, match="empty"):
            validate_pipeline([{}], _USER_ID)

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
            validate_pipeline(pipeline, _USER_ID)

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
        result = validate_pipeline(pipeline, _USER_ID)

        assert "$vectorSearch" in result[0]

    @pytest.mark.parametrize(
        "first_stage",
        [
            pytest.param(
                {"$group": {"_id": "$type", "items": {"$push": "$$ROOT"}}},
                id="group",
            ),
            pytest.param({"$sample": {"size": 10}}, id="sample"),
            pytest.param({"$sort": {"name": 1}}, id="sort"),
            pytest.param({"$project": {"name": 1, "user_id": 1}}, id="project"),
            pytest.param({"$unwind": "$tags"}, id="unwind"),
            pytest.param(
                {
                    "$bucket": {
                        "groupBy": "$score",
                        "boundaries": [0, 5, 10],
                        "default": "other",
                        "output": {"items": {"$push": "$$ROOT"}},
                    }
                },
                id="bucket",
            ),
            pytest.param({"$count": "total"}, id="count"),
            pytest.param({"$sortByCount": "$user_id"}, id="sortbycount"),
            pytest.param({"$addFields": {"x": 1}}, id="addfields"),
        ],
    )
    def test_tenant_match_prepended_when_first_stage_is_not_match_or_vectorsearch(
        self, first_stage
    ):
        """Blocker #1 (cycle 3) — `_inject_user_id` must enforce tenant scoping.

        The cycle-2 fix removed sub-pipeline-bearing stages, but `_inject_user_id`
        still had "modify-existing" semantics: it patched any `$match` /
        `$vectorSearch` / `$graphLookup` that was already in the pipeline but
        did NOT prepend a tenant filter when the pipeline didn't lead with
        one. The Tester demonstrated runtime leaks for `$group`, `$sample`,
        `$sort`, `$project`, `$unwind`, `$bucket`, `$count`, and
        `$sortByCount` as first stages.

        Contract: when the first stage is not `$match` or `$vectorSearch`,
        `validate_pipeline` must prepend `{"$match": {"user_id": user_id}}`
        as the new leading stage so every downstream operator runs against a
        tenant-scoped subset.
        """

        pipeline = [first_stage, {"$limit": 10}]

        result = validate_pipeline(pipeline, _USER_ID)

        assert result[0] == {"$match": {"user_id": _USER_ID}}, (
            f"Expected leading tenant $match, got {result[0]!r}"
        )

    def test_tenant_match_not_prepended_when_first_stage_is_match(self):
        """When the pipeline already leads with `$match`, no duplicate prepend.

        The existing `_inject_user_id` walks `$match` stages and merges in the
        tenant filter; prepending again would just produce a redundant
        leading `$match` stage. The contract is "tenant scope on or before
        the first non-trivial stage", which the in-place merge already
        provides for `$match`-led pipelines.
        """

        pipeline = [{"$match": {"kind": "node"}}, {"$limit": 10}]

        result = validate_pipeline(pipeline, _USER_ID)

        # The leading stage should be the original `$match` with `user_id`
        # merged in — NOT a separate prepended `$match`.
        assert "$match" in result[0]
        assert result[0]["$match"].get("user_id") == _USER_ID
        # And the second stage should be the original `$limit`, not another
        # `$match`.
        assert "$limit" in result[1]

    def test_tenant_match_not_prepended_when_first_stage_is_vectorsearch(self):
        """When the pipeline leads with `$vectorSearch`, tenant filter goes inside its `filter`.

        Prepending a `$match` ahead of `$vectorSearch` would violate Mongo's
        rule that `$vectorSearch` must be the first stage. The injection
        path that merges `user_id` into `$vectorSearch.filter` already
        scopes the search.
        """

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

        result = validate_pipeline(pipeline, _USER_ID)

        assert "$vectorSearch" in result[0]
        assert result[0]["$vectorSearch"]["filter"]["user_id"] == _USER_ID


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


class TestNlToPipeline:
    async def test_returns_validated_pipeline(self, mocker):
        mock_llm = mocker.AsyncMock()
        mock_llm.generate_json.return_value = {
            "pipeline": [
                {"$match": {"kind": "node"}},
                {"$limit": 10},
            ]
        }

        result = await nl_to_pipeline(mock_llm, "find all nodes")

        assert any("$match" in s for s in result)
        mock_llm.generate_json.assert_called_once()

    async def test_missing_pipeline_key_raises(self, mocker):
        mock_llm = mocker.AsyncMock()
        mock_llm.generate_json.return_value = {"wrong_key": []}

        with pytest.raises(PipelineValidationError, match="pipeline"):
            await nl_to_pipeline(mock_llm, "find all nodes")

    async def test_pipeline_not_a_list_raises(self, mocker):
        mock_llm = mocker.AsyncMock()
        mock_llm.generate_json.return_value = {"pipeline": "not a list"}

        with pytest.raises(PipelineValidationError, match="pipeline"):
            await nl_to_pipeline(mock_llm, "find all nodes")

    async def test_preserves_embedding_placeholder(self, mocker):
        """nl_to_pipeline no longer replaces placeholders — that's done later."""

        mock_llm = mocker.AsyncMock()
        mock_llm.generate_json.return_value = {
            "pipeline": [
                {
                    "$vectorSearch": {
                        "index": "vector_index",
                        "path": "embedding",
                        "queryVector": "__EMBED__",
                        "queryText": "semantic search",
                        "limit": 10,
                    }
                },
                {"$limit": 5},
            ]
        }

        result = await nl_to_pipeline(mock_llm, "semantic search")

        assert result[0]["$vectorSearch"]["queryVector"] == "__EMBED__"

    async def test_returns_pipeline_without_embedding(self, mocker):
        mock_llm = mocker.AsyncMock()
        mock_llm.generate_json.return_value = {
            "pipeline": [
                {"$match": {"kind": "node"}},
                {"$limit": 5},
            ]
        }

        result = await nl_to_pipeline(mock_llm, "find nodes")

        assert any("$match" in s for s in result)


class _AsyncCursorStub:
    """Minimal async iterator that yields documents from a list."""

    def __init__(self, docs: list[dict]) -> None:
        self._docs = list(docs)
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._docs):
            raise StopAsyncIteration
        doc = self._docs[self._idx]
        self._idx += 1
        return doc


class TestExecuteNlQuery:
    @pytest.fixture()
    def mock_deps(self, mocker):
        """Shared mocks for execute_nl_query tests."""

        mock_llm = mocker.AsyncMock()
        mock_embed = mocker.AsyncMock()
        mock_embed.embed.return_value = [[0.1, 0.2]]

        mock_collection = mocker.AsyncMock()
        mock_collection.aggregate.return_value = _AsyncCursorStub(
            [{"_id": "person:alice", "name": "Alice"}]
        )

        mock_client = mocker.MagicMock()
        mock_client.__getitem__.return_value.__getitem__.return_value = mock_collection

        return {
            "client": mock_client,
            "database": "test_db",
            "llm": mock_llm,
            "embedding_model": mock_embed,
            "collection": mock_collection,
        }

    async def test_successful_query(self, mock_deps):
        mock_deps["llm"].generate_json.return_value = {
            "pipeline": [
                {"$match": {"kind": "node"}},
                {"$limit": 10},
            ]
        }

        results = await execute_nl_query(
            client=mock_deps["client"],
            database=mock_deps["database"],
            query="find all people",
            llm=mock_deps["llm"],
            embedding_model=mock_deps["embedding_model"],
            user_id=_USER_ID,
        )

        assert len(results) == 1
        assert results[0]["_id"] == "person:alice"
        mock_deps["collection"].aggregate.assert_called_once()

    async def test_retries_on_validation_error(self, mock_deps):
        mock_deps["llm"].generate_json.side_effect = [
            {"pipeline": [{"$out": "evil"}]},  # First attempt: blocked stage
            {
                "pipeline": [
                    {"$match": {"kind": "node"}},
                    {"$limit": 10},
                ]
            },  # Second attempt: valid
        ]

        results = await execute_nl_query(
            client=mock_deps["client"],
            database=mock_deps["database"],
            query="find nodes",
            llm=mock_deps["llm"],
            embedding_model=mock_deps["embedding_model"],
            user_id=_USER_ID,
            max_retries=1,
        )

        assert len(results) == 1
        assert mock_deps["llm"].generate_json.call_count == 2

    async def test_retries_on_operation_failure(self, mock_deps):
        mock_deps["llm"].generate_json.return_value = {
            "pipeline": [
                {"$match": {"kind": "node"}},
                {"$limit": 10},
            ]
        }
        mock_deps["collection"].aggregate.side_effect = [
            OperationFailure("bad query"),
            _AsyncCursorStub([{"_id": "person:bob", "name": "Bob"}]),
        ]

        results = await execute_nl_query(
            client=mock_deps["client"],
            database=mock_deps["database"],
            query="find nodes",
            llm=mock_deps["llm"],
            embedding_model=mock_deps["embedding_model"],
            user_id=_USER_ID,
            max_retries=1,
        )

        assert len(results) == 1

    async def test_raises_after_max_retries_exhausted(self, mock_deps):
        mock_deps["llm"].generate_json.return_value = {"pipeline": [{"$out": "evil"}]}

        with pytest.raises(PipelineValidationError):
            await execute_nl_query(
                client=mock_deps["client"],
                database=mock_deps["database"],
                query="find nodes",
                llm=mock_deps["llm"],
                embedding_model=mock_deps["embedding_model"],
                user_id=_USER_ID,
                max_retries=1,
            )

        assert mock_deps["llm"].generate_json.call_count == 2

    async def test_retry_prompt_includes_original_query_and_error(self, mock_deps):
        mock_deps["llm"].generate_json.side_effect = [
            {"pipeline": [{"$out": "evil"}]},
            {
                "pipeline": [
                    {"$match": {"kind": "node"}},
                    {"$limit": 10},
                ]
            },
        ]

        await execute_nl_query(
            client=mock_deps["client"],
            database=mock_deps["database"],
            query="find all people",
            llm=mock_deps["llm"],
            embedding_model=mock_deps["embedding_model"],
            user_id=_USER_ID,
            max_retries=1,
        )

        retry_prompt = mock_deps["llm"].generate_json.call_args_list[1][0][0]
        assert "find all people" in retry_prompt
        assert "error" in retry_prompt.lower()

    async def test_max_results_forwarded_to_pipeline(self, mock_deps):
        """When max_results is passed, the executed pipeline contains a $limit with that value."""

        mock_deps["llm"].generate_json.return_value = {
            "pipeline": [
                {"$match": {"kind": "node"}},
            ]
        }

        await execute_nl_query(
            client=mock_deps["client"],
            database=mock_deps["database"],
            query="find nodes",
            llm=mock_deps["llm"],
            embedding_model=mock_deps["embedding_model"],
            user_id=_USER_ID,
            max_results=7,
        )

        executed_pipeline = mock_deps["collection"].aggregate.call_args[0][0]
        limit_stages = [s for s in executed_pipeline if "$limit" in s]
        assert len(limit_stages) == 1
        assert limit_stages[0]["$limit"] == 7

    async def test_zero_retries_fails_immediately(self, mock_deps):
        mock_deps["llm"].generate_json.return_value = {"pipeline": [{"$out": "evil"}]}

        with pytest.raises(PipelineValidationError):
            await execute_nl_query(
                client=mock_deps["client"],
                database=mock_deps["database"],
                query="find nodes",
                llm=mock_deps["llm"],
                embedding_model=mock_deps["embedding_model"],
                user_id=_USER_ID,
                max_retries=0,
            )

        assert mock_deps["llm"].generate_json.call_count == 1
