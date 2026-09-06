"""Unit tests for ``scripts/visualize_embeddings.py`` (ADR-007 Decision 8).

The script is glue, so every boundary (Mongo, tenant resolution, the map read,
the file writer, the browser) is mocked; what is asserted is the CONTRACT an
operator sees: no **Clustering run** → the command to run and exit 1, a stale
run → the warning on the FIRST line, and the flags reaching the renderer.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner

from tree.memory.clustering.types import EmbeddingMap, MapPoint, MemoryClusterInfo
from tree.memory.visualize.embeddings import NO_CLUSTERING_RUN_MESSAGE


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.visualize_embeddings as module

    return module


@pytest.fixture
def mocked_boundaries(mocker, cli_module, tmp_path):
    """Stub Mongo, tenant resolution, the file writer and the browser."""

    mocker.patch.object(cli_module, "init_mongodb", new_callable=AsyncMock)
    mocker.patch.object(
        cli_module,
        "resolve_user_id",
        new_callable=AsyncMock,
        return_value=PydanticObjectId(),
    )
    mocker.patch.object(cli_module.webbrowser, "open", return_value=True)
    return mocker.patch.object(
        cli_module,
        "render_embedding_map_file",
        return_value=tmp_path / "embedding-map-20260906-101500.html",
    )


def _embedding_map(*, unclustered: int = 0, total_children: int = 12) -> EmbeddingMap:
    drawn = total_children - unclustered
    return EmbeddingMap(
        run_id="run-1",
        clusters=[
            MemoryClusterInfo(
                cluster_id=0,
                label="Agent memory design",
                summary="How agents remember.",
                keywords=["memory", "agents", "design"],
                size=drawn,
                sample_chunk_ids=["chunk-0"],
                centroid_x=0.0,
                centroid_y=0.0,
            )
        ],
        points=[
            MapPoint(
                chunk_id=f"chunk-{index}",
                x=float(index),
                y=0.0,
                cluster_id=0,
                title="Memory for AI Agents",
                heading_path=["Memory"],
                snippet="A passage.",
            )
            for index in range(drawn)
        ],
        total_children=total_children,
        unclustered=unclustered,
    )


@pytest.fixture
def loaded_map(mocker, cli_module):
    """Patch the map read; the test supplies the ``EmbeddingMap`` (or ``None``)."""

    def _load(embedding_map: EmbeddingMap | None):
        return mocker.patch.object(
            cli_module,
            "load_embedding_map",
            new_callable=AsyncMock,
            return_value=embedding_map,
        )

    return _load


class TestNoClusteringRun:
    """Story 4: nobody has clustered yet — an explanation, not an empty canvas."""

    def test_it_prints_the_command_to_run_and_exits_one(
        self, cli_module, mocked_boundaries, loaded_map
    ) -> None:
        loaded_map(None)

        result = CliRunner().invoke(cli_module.main, ["--no-open"])

        assert result.exit_code == 1
        assert NO_CLUSTERING_RUN_MESSAGE in result.output

    def test_it_writes_no_file(self, cli_module, mocked_boundaries, loaded_map) -> None:
        loaded_map(None)

        CliRunner().invoke(cli_module.main, ["--no-open"])

        mocked_boundaries.assert_not_called()


class TestRenderedMap:
    """Story 5: the operator renders from the terminal."""

    def test_it_prints_the_written_path_and_the_summary(
        self, cli_module, mocked_boundaries, loaded_map
    ) -> None:
        loaded_map(_embedding_map())

        result = CliRunner().invoke(cli_module.main, ["--no-open"])

        assert result.exit_code == 0
        assert "Wrote " in result.output
        assert "embedding-map-20260906-101500.html" in result.output
        assert "Embedding map: 12 chunks in 1 clusters (+0 noise)" in result.output

    def test_a_stale_run_warns_on_the_very_first_line(
        self, cli_module, mocked_boundaries, loaded_map
    ) -> None:
        # Story 3: the operator must read "stale" before reading a file path.
        loaded_map(_embedding_map(unclustered=3))

        result = CliRunner().invoke(cli_module.main, ["--no-open"])

        assert result.output.splitlines()[0] == (
            "3 of 12 chunks have no cluster assignment (or a stale one) — run "
            "make memory-run-clustering-pipeline"
        )

    def test_a_fresh_run_prints_no_warning_line(
        self, cli_module, mocked_boundaries, loaded_map
    ) -> None:
        loaded_map(_embedding_map())

        result = CliRunner().invoke(cli_module.main, ["--no-open"])

        assert "no cluster assignment" not in result.output

    @pytest.mark.parametrize(
        ("flag", "expected"), [("--hulls", True), ("--no-hulls", False)]
    )
    def test_the_hulls_flag_reaches_the_payload(
        self, cli_module, mocked_boundaries, loaded_map, flag: str, expected: bool
    ) -> None:
        loaded_map(_embedding_map())

        CliRunner().invoke(cli_module.main, [flag, "--no-open"])

        payload = mocked_boundaries.call_args.args[0]
        assert payload["hulls"] is expected

    def test_output_pins_the_destination_file(
        self, cli_module, mocked_boundaries, loaded_map
    ) -> None:
        loaded_map(_embedding_map())

        CliRunner().invoke(cli_module.main, ["--output", "/tmp/map.html", "--no-open"])

        assert mocked_boundaries.call_args.args[1] == "/tmp/map.html"

    def test_no_open_skips_the_browser(
        self, cli_module, mocked_boundaries, loaded_map
    ) -> None:
        loaded_map(_embedding_map())

        CliRunner().invoke(cli_module.main, ["--no-open"])

        cli_module.webbrowser.open.assert_not_called()

    def test_by_default_it_opens_the_map_in_a_browser(
        self, cli_module, mocked_boundaries, loaded_map
    ) -> None:
        loaded_map(_embedding_map())

        CliRunner().invoke(cli_module.main, [])

        cli_module.webbrowser.open.assert_called_once()


class TestMakefileWiring:
    def test_the_make_target_is_wired_to_this_script(self) -> None:
        """``make memory-visualize-embeddings`` is the operator's entry point.

        A script nobody can reach through the Makefile is a script nobody runs;
        the two knobs (HULLS, OUTPUT) have to be forwarded or the target is a
        worse version of the raw command.
        """

        makefile = (Path(__file__).resolve().parents[3] / "Makefile").read_text(
            encoding="utf-8"
        )

        assert "visualize-embeddings:" in makefile
        assert "scripts/visualize_embeddings.py $(USER_FLAGS)" in makefile
        assert "$(if $(filter true,$(HULLS)),--hulls,)" in makefile
        assert '$(if $(OUTPUT),--output "$(OUTPUT)",)' in makefile
