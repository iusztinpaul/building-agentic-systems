"""Unit tests for the **Embedding map** payload builder.

Covers the pure ``EmbeddingMap`` → payload transform (colours, legend, warning,
summary, the fixed-layout keys), the palette's wrap at 20 and the map's file
writer. The template that CONSUMES those keys lives in
``tree.memory.visualize.graph`` and is asserted there; the Mongo read that
produces an ``EmbeddingMap`` lives in ``tree.memory.clustering.store``.
"""

import re
from pathlib import Path

import pytest

from tree.config.paths import GRAPHS_DIR
from tree.memory.clustering.types import EmbeddingMap, MapPoint, MemoryClusterInfo
from tree.memory.visualize.embeddings import (
    CLUSTER_PALETTE,
    NO_CLUSTERING_RUN_MESSAGE,
    NOISE_COLOUR,
    UNCLUSTERED_COLOUR,
    cluster_colour,
    render_embedding_map_file,
    to_embedding_map_payload,
    unclustered_warning,
)


def _cluster(cluster_id: int, label: str, size: int) -> MemoryClusterInfo:
    return MemoryClusterInfo(
        cluster_id=cluster_id,
        label=label,
        summary=f"About {label}.",
        keywords=["a", "b", "c"],
        size=size,
        sample_chunk_ids=[f"chunk-{cluster_id}-0"],
        centroid_x=float(cluster_id),
        centroid_y=float(cluster_id),
    )


def _point(index: int, cluster_id: int, *, title: str | None = "Paper") -> MapPoint:
    return MapPoint(
        chunk_id=f"chunk-{index}",
        x=float(index),
        y=float(index) / 2,
        cluster_id=cluster_id,
        title=title,
        heading_path=["Memory", "Retrieval"],
        snippet=f"Snippet {index}.",
    )


def _map(
    *,
    sizes: dict[int, int] | None = None,
    noise: int = 5,
    unclustered: int = 3,
    total_children: int = 50,
) -> EmbeddingMap:
    """An ``EmbeddingMap`` with the ACs' shape: {0: 30 pts, 1: 12 pts} + noise."""

    sizes = {0: 30, 1: 12} if sizes is None else sizes
    clusters = [
        _cluster(cluster_id, f"Cluster {cluster_id} topic", size)
        for cluster_id, size in sizes.items()
    ]
    points: list[MapPoint] = []
    index = 0
    for cluster_id, size in sizes.items():
        for _ in range(size):
            points.append(_point(index, cluster_id))
            index += 1
    for _ in range(noise):
        points.append(_point(index, -1))
        index += 1
    return EmbeddingMap(
        run_id="run-1",
        clusters=clusters,
        points=points,
        total_children=total_children,
        unclustered=unclustered,
    )


# ---------------------------------------------------------------------------
# cluster_colour — the 20-colour categorical palette
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cluster_id", [0, 1, 7, 19])
def test_cluster_colour_indexes_the_palette(cluster_id: int) -> None:
    assert cluster_colour(cluster_id) == CLUSTER_PALETTE[cluster_id]


def test_cluster_colour_wraps_the_palette_after_twenty_clusters() -> None:
    # Arrange / Act: a 25-cluster run — ids 20..24 have no palette slot of
    # their own.
    colours = [cluster_colour(cluster_id) for cluster_id in range(25)]

    # Assert: the palette wraps rather than running out, and a real cluster is
    # never coloured like noise.
    assert colours[20] == CLUSTER_PALETTE[0]
    assert colours[24] == CLUSTER_PALETTE[4]
    assert NOISE_COLOUR not in colours


def test_cluster_colour_paints_noise_grey() -> None:
    assert cluster_colour(-1) == NOISE_COLOUR
    assert NOISE_COLOUR not in CLUSTER_PALETTE


def test_no_clustering_run_message_names_the_command_that_fixes_it() -> None:
    # Assert: the message a surface prints instead of an empty map is
    # actionable — it names the pipeline to run.
    assert "make memory-run-clustering-pipeline" in NO_CLUSTERING_RUN_MESSAGE


# ---------------------------------------------------------------------------
# unclustered_warning — the stale-map contract (ADR-007 §8)
# ---------------------------------------------------------------------------


def test_unclustered_warning_reports_both_counts_and_the_command() -> None:
    warning = unclustered_warning(_map())

    assert warning == (
        "3 of 50 chunks have no cluster assignment (or a stale one) — "
        "run make memory-run-clustering-pipeline"
    )


def test_unclustered_warning_is_none_when_every_chunk_is_on_the_map() -> None:
    assert unclustered_warning(_map(unclustered=0, total_children=47)) is None


# ---------------------------------------------------------------------------
# to_embedding_map_payload — the Embedding map payload
# ---------------------------------------------------------------------------


def test_payload_draws_one_fixed_node_per_point() -> None:
    payload = to_embedding_map_payload(_map())

    # Assert: 30 + 12 + 5 points, no edges, coordinates honoured verbatim.
    assert len(payload["nodes"]) == 47
    assert payload["edges"] == []
    assert payload["layout"] == "fixed"
    assert payload["nodeSize"] == 4


def test_payload_colours_nodes_by_cluster_and_noise_grey() -> None:
    payload = to_embedding_map_payload(_map())
    nodes = payload["nodes"]

    colours = {
        cluster_id: {n["color"] for n in nodes if n["cluster_id"] == cluster_id}
        for cluster_id in (0, 1, -1)
    }

    assert colours[0] == {CLUSTER_PALETTE[0]}
    assert colours[1] == {CLUSTER_PALETTE[1]}
    assert colours[-1] == {NOISE_COLOUR}


def test_payload_nodes_carry_coordinates_no_label_and_the_tooltip_meta() -> None:
    payload = to_embedding_map_payload(_map())

    for node in payload["nodes"]:
        assert isinstance(node["x"], float)
        assert isinstance(node["y"], float)
        # No canvas text: hundreds of chunk titles would be unreadable.
        assert node["label"] == ""
        assert set(node["meta"]) == {"cluster", "document", "heading_path", "snippet"}

    first = payload["nodes"][0]
    assert first["type"] == "chunk"
    assert first["name"] == "Paper"
    assert first["meta"]["cluster"] == "Cluster 0 topic"
    assert first["meta"]["heading_path"] == "Memory > Retrieval"
    assert first["meta"]["snippet"] == "Snippet 0."


def test_payload_names_an_untitled_document() -> None:
    embedding_map = _map()
    embedding_map.points[0].title = None

    node = to_embedding_map_payload(embedding_map)["nodes"][0]

    assert node["name"] == "(untitled)"
    assert node["meta"]["document"] == "(untitled)"


def test_payload_meta_labels_noise_points_as_noise() -> None:
    payload = to_embedding_map_payload(_map())

    noise_node = next(n for n in payload["nodes"] if n["cluster_id"] == -1)

    assert noise_node["meta"]["cluster"] == "noise"


def test_payload_legend_lists_clusters_by_size_then_the_two_greys() -> None:
    payload = to_embedding_map_payload(_map())

    assert payload["legend"] == [
        {"label": "Cluster 0 topic", "size": 30, "color": CLUSTER_PALETTE[0]},
        {"label": "Cluster 1 topic", "size": 12, "color": CLUSTER_PALETTE[1]},
        {"label": "noise", "size": 5, "color": NOISE_COLOUR},
        {
            "label": "unclustered / stale (not shown)",
            "size": 3,
            "color": UNCLUSTERED_COLOUR,
        },
    ]


def test_payload_legend_orders_clusters_largest_first() -> None:
    # Arrange: the small cluster comes FIRST in the map (store order is not a
    # contract of the builder).
    payload = to_embedding_map_payload(_map(sizes={0: 4, 1: 40}))

    assert [row["label"] for row in payload["legend"][:2]] == [
        "Cluster 1 topic",
        "Cluster 0 topic",
    ]


def test_payload_carries_the_warning_when_chunks_are_unclustered() -> None:
    payload = to_embedding_map_payload(_map())

    assert payload["warning"] == (
        "3 of 50 chunks have no cluster assignment (or a stale one) — "
        "run make memory-run-clustering-pipeline"
    )


def test_payload_drops_the_warning_and_the_grey_row_when_nothing_is_stale() -> None:
    payload = to_embedding_map_payload(_map(unclustered=0, total_children=47))

    assert payload["warning"] is None
    assert all(row["color"] != UNCLUSTERED_COLOUR for row in payload["legend"]), (
        payload["legend"]
    )


def test_payload_omits_the_noise_row_when_the_run_left_no_noise() -> None:
    payload = to_embedding_map_payload(_map(noise=0))

    assert all(row["label"] != "noise" for row in payload["legend"])


@pytest.mark.parametrize("hulls", [True, False])
def test_payload_hulls_mirrors_the_argument(hulls: bool) -> None:
    # The key's PRESENCE is what shows the toggle; its value is the initial
    # checked state.
    assert to_embedding_map_payload(_map(), hulls=hulls)["hulls"] is hulls


def test_payload_summary_counts_chunks_clusters_and_noise() -> None:
    payload = to_embedding_map_payload(_map())

    assert payload["summary"] == "Embedding map: 47 chunks in 2 clusters (+5 noise)"


# ---------------------------------------------------------------------------
# render_embedding_map_file — the self-contained HTML file
# ---------------------------------------------------------------------------


def test_render_embedding_map_file_embeds_the_fixed_layout_and_hull_machinery(
    tmp_path: Path,
) -> None:
    payload = to_embedding_map_payload(_map(), hulls=True)
    out = tmp_path / "m.html"

    path = render_embedding_map_file(payload, out)

    assert path == out
    html = out.read_text(encoding="utf-8")
    # The payload keys reach the browser…
    assert '"layout": "fixed"' in html
    assert '"nodeSize": 4' in html
    # …and so does everything that consumes them.
    assert "convexHull" in html
    assert "hulls-toggle" in html
    assert "graphToViewport" in html
    assert 'renderer.on("afterRender", drawHulls)' in html
    assert 'renderer.on("resize", drawHulls)' in html


def test_render_embedding_map_file_carries_the_warning_and_every_cluster_label(
    tmp_path: Path,
) -> None:
    payload = to_embedding_map_payload(_map())
    out = tmp_path / "m.html"

    render_embedding_map_file(payload, out)

    html = out.read_text(encoding="utf-8")
    assert "have no cluster assignment (or a stale one)" in html
    for label in ("Cluster 0 topic", "Cluster 1 topic", "unclustered / stale"):
        assert label in html


def test_render_embedding_map_file_escapes_script_close_in_a_label(
    tmp_path: Path,
) -> None:
    # Arrange: an LLM-written cluster label containing </script> must not close
    # the inline <script> block.
    embedding_map = _map()
    embedding_map.clusters[0].label = "</script>evil"
    payload = to_embedding_map_payload(embedding_map)
    out = tmp_path / "m.html"

    render_embedding_map_file(payload, out)

    html = out.read_text(encoding="utf-8")
    assert "</script>evil" not in html
    assert "<\\/script>evil" in html


def test_render_embedding_map_file_defaults_to_a_stamped_file_in_graphs_dir(
    mocker, tmp_path: Path
) -> None:
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
    payload = to_embedding_map_payload(_map())

    path = render_embedding_map_file(payload)

    # Assert: the graph's convention with the map's stem.
    assert path.parent == tmp_path
    assert re.fullmatch(r"embedding-map-\d{8}-\d{6}\.html", path.name), path.name
    assert path.is_file()


def test_default_map_path_lives_under_the_shared_graphs_dir() -> None:
    # Assert: the ONE output convention — maps land beside graphs, not in a
    # directory of their own (the graphs:// resource serves both).
    assert GRAPHS_DIR.name == "graphs"


def test_render_embedding_map_file_headers_the_map_with_its_summary(
    tmp_path: Path,
) -> None:
    payload = to_embedding_map_payload(_map())
    out = tmp_path / "m.html"

    render_embedding_map_file(payload, out)

    # The header line the reader sees is "47 chunks in 2 clusters (+5 noise)",
    # not "47 nodes · 0 edges" — both the summary and the branch that shows it
    # have to reach the browser.
    html = out.read_text(encoding="utf-8")
    assert '"summary": "Embedding map: 47 chunks in 2 clusters (+5 noise)"' in html
    assert 'payload.summary.replace(/^Embedding map: /, "")' in html


def test_render_embedding_map_file_accepts_a_string_output_path(
    tmp_path: Path,
) -> None:
    # The CLI's ``--output`` flag hands over a plain ``str``; a raw string would
    # otherwise reach ``Path.parent`` inside the writer and raise.
    payload = to_embedding_map_payload(_map())
    out = tmp_path / "m.html"

    path = render_embedding_map_file(payload, str(out))

    assert path == out
    assert out.is_file()
