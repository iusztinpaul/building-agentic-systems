"""Unit tests for :mod:`tree.cli` — the shared script glue.

Only the logic-bearing helpers: ``build_online_source`` (URL vs file detect)
and ``wait_for_dispatch`` (blocking on a submitted run). The Prefect client
plumbing (``wait_for_flow_run``) is infrastructure and is exercised by running
the real pipelines (see AGENTS.md "Running pipelines & E2E"), not unit-tested.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tree.cli import (
    build_online_source,
    warn_ignored_config_overrides,
    wait_for_dispatch,
)
from tree.data.online_pipeline import FileSource, UrlSource
from tree.online import IngestReceipt


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

        # Act — every dispatcher but the online one answers a plain dict.
        await wait_for_dispatch({"status": "scheduled", "flow_run_id": "abc"})

        mock_wait.assert_awaited_once_with("abc")

    async def test_waits_on_a_dispatched_ingest_receipt(self, mocker) -> None:
        mock_wait = mocker.patch("tree.cli.wait_for_flow_run", new_callable=AsyncMock)

        await wait_for_dispatch(
            IngestReceipt(
                source_uri="file:///tmp/notes.md",
                duplicate=False,
                flow_run_id="abc",
                status="scheduled",
            )
        )

        mock_wait.assert_awaited_once_with("abc")

    async def test_wait_for_dispatch_skips_duplicate_receipt(
        self, mocker, caplog
    ) -> None:
        mock_wait = mocker.patch("tree.cli.wait_for_flow_run", new_callable=AsyncMock)
        receipt = IngestReceipt(
            source_uri="file:///tmp/notes.md",
            duplicate=True,
            document_id="507f1f77bcf86cd799439012",
            flow_run_id=None,
            status="duplicate",
        )

        with caplog.at_level(logging.INFO, logger="tree.cli"):
            await wait_for_dispatch(receipt)

        # A duplicate dispatched no run, so there is nothing to poll: say which
        # Document already holds the source and return.
        mock_wait.assert_not_awaited()
        assert (
            "Already ingested: file:///tmp/notes.md (document 507f1f77bcf86cd799439012)"
            in caplog.text
        )


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

    def test_several_prefixes_are_one_warning_naming_all_of_them(
        self, caplog, monkeypatch
    ) -> None:
        """Every dispatcher warns for 2-3 sections; one call, one line."""

        monkeypatch.setenv("TREE_MODELS__LLM__PROVIDER", "modal")
        monkeypatch.setenv("TREE_MODAL__REQUEST_TIMEOUT_S", "600")

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            warn_ignored_config_overrides("TREE_MODELS__", "TREE_MODAL__")

        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "TREE_MODELS__LLM__PROVIDER" in message
        assert "TREE_MODAL__REQUEST_TIMEOUT_S" in message

    def test_the_suite_s_single_underscore_dry_run_rail_is_not_an_override(
        self, caplog, monkeypatch
    ) -> None:
        """``TREE_MODAL_DRY_RUN`` is a CLI rail, not a ``TREE_MODAL__`` key.

        Every unit test runs with it set (``tests/unit/conftest.py``'s
        ``_modal_dry_run``), and operators set it for a dry deploy — one
        underscore short of the section prefix. A warning here would fire on
        every clean shell and train the reader to ignore the line.
        """

        monkeypatch.setenv("TREE_MODAL_DRY_RUN", "1")
        # Hermetic: `make` exports `.env`, which may itself carry an override.
        for name in [
            name
            for name in os.environ
            if name.startswith(("TREE_MODELS__", "TREE_MODAL__"))
        ]:
            monkeypatch.delenv(name, raising=False)

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            warn_ignored_config_overrides("TREE_MODELS__", "TREE_MODAL__")

        assert caplog.records == []
