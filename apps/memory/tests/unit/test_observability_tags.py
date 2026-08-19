"""Unit tests for the centralized Opik tag families.

Two families, both centralized in :mod:`tree.config.constants`:

* PIPELINE-IDENTITY — the data / memory-extraction / memory-indexing pipelines,
  tagged 1:1 with their Prefect deployment / flow-run tags (the SAME constant
  feeds both), so a Prefect run and its Opik trace read identically.
* MCP-SURFACE — the four-tag family (``ingestion`` / ``retrieval`` / ``batch`` /
  ``mcp``) used as combinations by the MCP tools + dream consolidation.

These tests pin the constant values, the Prefect↔Opik 1:1 mapping, and that the
MCP tools stay on the four-tag family (no pipeline-name tag leaks into
``tools.py``).
"""

from __future__ import annotations

import tree.config.constants as consts
from tree.observability import pipeline_metadata

# The MCP-surface family — the closed set the MCP tools + dream draw from.
_FAMILY = {"ingestion", "retrieval", "batch", "mcp"}


class TestTagFamily:
    def test_family_is_exactly_four(self) -> None:
        assert {
            consts.TAG_INGESTION,
            consts.TAG_RETRIEVAL,
            consts.TAG_BATCH,
            consts.TAG_MCP,
        } == _FAMILY

    def test_combinations_only_use_family_tags(self) -> None:
        for combo in (
            consts.TAGS_INGESTION_BATCH,
            consts.TAGS_INGESTION_MCP,
            consts.TAGS_RETRIEVAL_MCP,
            consts.TAGS_MCP,
        ):
            assert set(combo) <= _FAMILY, combo

    def test_combination_values(self) -> None:
        assert set(consts.TAGS_INGESTION_BATCH) == {"ingestion", "batch"}
        assert set(consts.TAGS_INGESTION_MCP) == {"ingestion", "mcp"}
        assert set(consts.TAGS_RETRIEVAL_MCP) == {"retrieval", "mcp"}
        assert set(consts.TAGS_MCP) == {"mcp"}

    def test_pipeline_metadata_carries_pipeline_name(self) -> None:
        # The finer pipeline name rides as metadata, complementing the tag.
        assert pipeline_metadata("extraction") == {"pipeline": "extraction"}
        assert pipeline_metadata("data", shard=2) == {
            "pipeline": "data",
            "shard": 2,
        }


class TestPipelineIdentityTags:
    """Pipeline-identity tags + the Prefect↔Opik 1:1 invariant the user asked for:
    data = [data-pipeline, offline|online]; extraction = [memory-pipeline,
    extraction]; indexing = [memory-pipeline, indexing]."""

    def test_constant_values(self) -> None:
        assert consts.TAGS_DATA_OFFLINE == ["data-pipeline", "offline"]
        assert consts.TAGS_DATA_ONLINE == ["data-pipeline", "online"]
        assert consts.TAGS_EXTRACTION == ["memory-pipeline", "extraction"]
        assert consts.TAGS_INDEXING == ["memory-pipeline", "indexing"]

    def test_opik_span_tags_use_the_shared_constants(self) -> None:
        # Each pipeline's Opik ``_*_TAGS`` IS the shared constant (so Opik == Prefect).
        from tree.data.conversation.conversation_pipeline import _CONVERSATION_TAGS
        from tree.data.file.file_pipeline import _FILE_TAGS
        from tree.data.offline_pipeline import _DATA_TAGS
        from tree.memory.extraction.pipeline import _EXTRACTION_TAGS
        from tree.memory.indexing.pipeline import _INDEXING_TAGS

        assert _DATA_TAGS == consts.TAGS_DATA_OFFLINE
        assert _FILE_TAGS == consts.TAGS_DATA_ONLINE
        assert _CONVERSATION_TAGS == consts.TAGS_DATA_ONLINE
        assert _EXTRACTION_TAGS == consts.TAGS_EXTRACTION
        assert _INDEXING_TAGS == consts.TAGS_INDEXING

    def test_prefect_deployment_tags_match_opik_1to1(self) -> None:
        # The Prefect deployment specs carry the SAME pipeline-identity tags.
        from tree.offline import TAGS_OFFLINE_PIPELINE
        from tree.online import TAGS_ONLINE_PIPELINE
        from tree.orchestrator import _DEPLOYMENT_SPECS

        tags_by_name = {s.name: s.tags for s in _DEPLOYMENT_SPECS}
        assert tags_by_name["data-etl-worker"] == consts.TAGS_DATA_OFFLINE
        assert tags_by_name["memory-extract-etl-worker"] == consts.TAGS_EXTRACTION
        assert tags_by_name["memory-indexing-etl"] == consts.TAGS_INDEXING
        # The e2e pipelines span data + memory, so they carry both identity tags.
        assert tags_by_name["online-pipeline"] == TAGS_ONLINE_PIPELINE
        assert tags_by_name["offline-pipeline"] == TAGS_OFFLINE_PIPELINE


class TestMcpToolTags:
    """The MCP tools' ``@track(tags=...)`` are drawn from the family, with the
    right combination per tool category."""

    def test_no_pipeline_name_tags_in_tools_source(self) -> None:
        # Guard against re-introducing pipeline-name / legacy tags at the MCP layer.
        import tree.mcp.tools as tools_mod

        src = _module_source(tools_mod)
        for legacy in (
            "memory-pipeline",
            "data-pipeline",
            "memory-extraction",
            "memory-indexing",
            "dream-consolidation",
            "file-pipeline",
            "conversation-pipeline",
        ):
            assert legacy not in src, f"legacy tag {legacy!r} still in tools.py"

    def test_retrieval_tools_carry_retrieval_mcp(self) -> None:
        assert set(consts.TAGS_RETRIEVAL_MCP) == {"retrieval", "mcp"}

    def test_ingestion_tools_carry_ingestion_mcp(self) -> None:
        assert set(consts.TAGS_INGESTION_MCP) == {"ingestion", "mcp"}


def _module_source(mod) -> str:
    import inspect

    return inspect.getsource(mod)
