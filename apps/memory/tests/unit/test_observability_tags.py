"""Unit tests for the centralized four-tag family (design change 2026-06-10).

The whole Opik telemetry surface is tagged from EXACTLY four tags, used as
combinations:

* ``ingestion`` — writes into memory
* ``retrieval`` — reads memory
* ``batch``     — offline via Prefect (flows / tasks)
* ``mcp``       — online via the MCP server tools

These tests pin (a) the family is exactly those four constants, (b) the
combination constants only ever draw from the family, and (c) the MCP tools wear
the right combination. Centralizing the constants in
:mod:`tree.observability` means a stray tag anywhere fails one of these.
"""

from __future__ import annotations

import tree.observability as obs

# The closed set — nothing outside this is a valid Opik tag.
_FAMILY = {"ingestion", "retrieval", "batch", "mcp"}


class TestTagFamily:
    def test_family_is_exactly_four(self) -> None:
        assert {
            obs.TAG_INGESTION,
            obs.TAG_RETRIEVAL,
            obs.TAG_BATCH,
            obs.TAG_MCP,
        } == _FAMILY

    def test_combinations_only_use_family_tags(self) -> None:
        for combo in (
            obs.TAGS_INGESTION_BATCH,
            obs.TAGS_INGESTION_MCP,
            obs.TAGS_RETRIEVAL_MCP,
            obs.TAGS_MCP,
        ):
            assert set(combo) <= _FAMILY, combo

    def test_combination_values(self) -> None:
        assert set(obs.TAGS_INGESTION_BATCH) == {"ingestion", "batch"}
        assert set(obs.TAGS_INGESTION_MCP) == {"ingestion", "mcp"}
        assert set(obs.TAGS_RETRIEVAL_MCP) == {"retrieval", "mcp"}
        assert set(obs.TAGS_MCP) == {"mcp"}

    def test_pipeline_metadata_replaces_pipeline_name_tag(self) -> None:
        # Pipeline identity moved from a tag to metadata.
        assert obs.pipeline_metadata("extraction") == {"pipeline": "extraction"}
        assert obs.pipeline_metadata("data", shard=2) == {
            "pipeline": "data",
            "shard": 2,
        }


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
        assert set(obs.TAGS_RETRIEVAL_MCP) == {"retrieval", "mcp"}

    def test_ingestion_tools_carry_ingestion_mcp(self) -> None:
        assert set(obs.TAGS_INGESTION_MCP) == {"ingestion", "mcp"}


def _module_source(mod) -> str:
    import inspect

    return inspect.getsource(mod)
