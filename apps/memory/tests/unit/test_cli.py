"""Unit tests for :mod:`tree.cli` — the shared script glue.

Only the logic-bearing helpers: ``build_online_source`` (URL vs file detect)
and ``wait_for_dispatch`` (deployment vs in-process branching). The Prefect
client plumbing (``trigger_deployment`` / ``wait_for_flow_run``) is
infrastructure and is exercised by running the real pipelines (see AGENTS.md
"Running pipelines & E2E"), not unit-tested.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tree.cli import build_online_source, wait_for_dispatch
from tree.data.online_pipeline import FileSource, UrlSource


class TestBuildOnlineSource:
    @pytest.mark.parametrize(
        "url", ["https://example.com/post", "http://example.com/post"]
    )
    def test_http_url_builds_url_source(self, url: str) -> None:
        # Act
        source = build_online_source(url, title=None)

        # Assert
        assert isinstance(source, UrlSource)
        assert source.uri == url

    def test_local_file_builds_file_source_with_content(self, tmp_path: Path) -> None:
        # Arrange — files are read at the CLI edge; path resolved for stable dedup.
        file = tmp_path / "notes.md"
        file.write_text("# hello")

        # Act
        source = build_online_source(str(file), title="My notes")

        # Assert
        assert isinstance(source, FileSource)
        assert source.path == str(file.resolve())
        assert source.content == "# hello"
        assert source.title == "My notes"


class TestWaitForDispatch:
    async def test_waits_on_the_submitted_flow_run(self, mocker) -> None:
        # Arrange
        mock_wait = mocker.patch("tree.cli.wait_for_flow_run", new_callable=AsyncMock)

        # Act — dispatch always creates a worker-side run; nothing to branch on.
        await wait_for_dispatch({"status": "scheduled", "flow_run_id": "abc"})

        # Assert
        mock_wait.assert_awaited_once_with("abc")
