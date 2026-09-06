"""Unit tests for :mod:`tree.cli` — the shared script glue.

Only the logic-bearing helpers: ``build_online_source`` (URL vs file detect)
and ``wait_for_dispatch`` (blocking on a submitted run). The Prefect client
plumbing (``wait_for_flow_run``) is infrastructure and is exercised by running
the real pipelines (see AGENTS.md "Running pipelines & E2E"), not unit-tested.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tree.cli import (
    build_online_source,
    warn_ignored_config_overrides,
    wait_for_dispatch,
)
from tree.data.online_pipeline import FileSource, UrlSource


class TestBuildOnlineSource:
    @pytest.mark.parametrize(
        "url", ["https://example.com/post", "http://example.com/post"]
    )
    def test_http_url_builds_url_source(self, url: str) -> None:
        source = build_online_source(url, title=None)

        assert isinstance(source, UrlSource)
        assert source.uri == url

    def test_local_file_builds_file_source_with_content(self, tmp_path: Path) -> None:
        # Arrange — files are read at the CLI edge; path resolved for stable dedup.
        file = tmp_path / "notes.md"
        file.write_text("# hello")

        source = build_online_source(str(file), title="My notes")

        assert isinstance(source, FileSource)
        assert source.path == str(file.resolve())
        assert source.content == "# hello"
        assert source.title == "My notes"


class TestWaitForDispatch:
    async def test_waits_on_the_submitted_flow_run(self, mocker) -> None:
        mock_wait = mocker.patch("tree.cli.wait_for_flow_run", new_callable=AsyncMock)

        # Act — dispatch always creates a worker-side run; nothing to branch on.
        await wait_for_dispatch({"status": "scheduled", "flow_run_id": "abc"})

        mock_wait.assert_awaited_once_with("abc")


class TestWarnIgnoredConfigOverrides:
    """``TREE_…`` overrides set in the DISPATCHING shell never reach the flow.

    ``dispatch_*_pipeline`` forwards no environment: the flow reads its config
    through ``_live_app_config()`` inside the ``serve-workflows`` process. A
    reader who prefixes the make command therefore changes nothing and has no
    hint why (the loop this warning closes).
    """

    def test_it_warns_naming_every_matching_variable(self, caplog, monkeypatch) -> None:
        monkeypatch.setenv("TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE", "5")
        monkeypatch.setenv("TREE_MEMORY__CLUSTERING__UMAP__N_NEIGHBORS", "3")

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            warn_ignored_config_overrides("TREE_MEMORY__CLUSTERING__")

        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE" in message
        assert "TREE_MEMORY__CLUSTERING__UMAP__N_NEIGHBORS" in message
        assert "make memory-serve-workflows" in message

    def test_it_says_nothing_when_no_override_is_set(self, caplog, monkeypatch) -> None:
        monkeypatch.delenv(
            "TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE", raising=False
        )

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            warn_ignored_config_overrides("TREE_MEMORY__CLUSTERING__")

        # The normal run must stay quiet — a warning nobody needs is noise.
        assert caplog.records == []

    def test_it_ignores_variables_outside_the_prefix(self, caplog, monkeypatch) -> None:
        monkeypatch.setenv("TREE_MEMORY__MODE", "rag")

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            warn_ignored_config_overrides("TREE_MEMORY__CLUSTERING__")

        assert caplog.records == []
