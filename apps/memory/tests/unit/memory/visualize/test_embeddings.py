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
from tree.memory.clustering.types import (
    EmbeddingMap,
    MapPoint,
    MemoryClusterInfo,
    QueryMap,
)
from tree.memory.visualize.embeddings import (
    CLUSTER_PALETTE,
    NO_CLUSTERING_RUN_MESSAGE,
    PREVIEW_COLOUR,
    REFERENCE_COLOUR,
    NOISE_COLOUR,
    UNCLUSTERED_COLOUR,
    cluster_colour,
    render_embedding_map_file,
    to_embedding_map_payload,
    to_query_map_payload,
    unclustered_warning,
)
from tree.memory.visualize.graph import _DEFAULT_DISPLAY


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
    cut_noise: int = 0,
    plotted_documents: int = 10,
    total_documents: int = 10,
) -> EmbeddingMap:
    """An ``EmbeddingMap`` with the ACs' shape: {0: 30 pts, 1: 12 pts} + noise.

    ``clustered`` is ``total_children - unclustered``, so a ``total_children``
    above the drawn points + ``unclustered`` models chunks of documents beyond
    the cap (``cut_noise`` of them noise): counted run-wide, never plotted.
    """

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
        clustered=total_children - unclustered,
        noise=noise + cut_noise,
        plotted_documents=plotted_documents,
        total_documents=total_documents,
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


def test_no_clustering_run_message_says_clustering_is_manual() -> None:
    # Assert: the exact wording — the warning says the run must be triggered.
    assert NO_CLUSTERING_RUN_MESSAGE == (
        "No clustering run found for this user. Showing the raw embeddings. Trigger it manually."
    )


def _preview(points: int = 4, total_children: int = 6) -> EmbeddingMap:
    """An unclustered preview: no run, every point noise-labelled."""

    return EmbeddingMap(
        run_id=None,
        clusters=[],
        points=[_point(index, -1) for index in range(points)],
        total_children=total_children,
        unclustered=total_children,
        clustered=0,
        noise=0,
        plotted_documents=2,
        total_documents=3,
    )


def test_preview_payload_warns_first_with_the_no_run_message() -> None:
    payload = to_embedding_map_payload(_preview())

    assert payload["warning"] == NO_CLUSTERING_RUN_MESSAGE


def test_preview_legend_is_one_coloured_row_for_every_chunk() -> None:
    payload = to_embedding_map_payload(_preview())

    # A real hue, not noise grey: these points are unclustered, not residue.
    assert payload["legend"] == [
        {
            "label": "not clustered yet",
            "size": 6,
            "color": PREVIEW_COLOUR,
            "cluster_id": -1,
        }
    ]
    assert {node["meta"]["cluster"] for node in payload["nodes"]} == {
        "not clustered yet"
    }


def test_preview_hides_the_hull_toggle_even_when_asked() -> None:
    # ``None`` (not a bool) is what hides the toggle: there are no clusters.
    assert to_embedding_map_payload(_preview(), hulls=True)["hulls"] is None


def test_preview_summary_counts_the_corpus_and_says_no_clusters() -> None:
    payload = to_embedding_map_payload(_preview())

    assert payload["summary"] == (
        "Embedding map: 4 of 6 chunks (the 2 most-recent of 3 documents) "
        "— no clusters yet (unclustered preview)"
    )


def test_a_preview_cannot_claim_clusters() -> None:
    with pytest.raises(ValueError, match="unclustered preview"):
        EmbeddingMap(
            run_id=None,
            clusters=[_cluster(0, "A", 3)],
            points=[],
            total_children=3,
            unclustered=3,
            clustered=0,
            noise=0,
            plotted_documents=1,
            total_documents=1,
        )


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


def test_payload_sizes_every_point_per_node() -> None:
    payload = to_embedding_map_payload(_map())

    # Assert: ONE sizing mechanism (per node, like the graph) — no
    # payload-wide ``nodeSize`` key any more.
    assert "nodeSize" not in payload
    assert {n["size"] for n in payload["nodes"]} == {4}


def test_payload_ships_display_controls_but_no_forces() -> None:
    payload = to_embedding_map_payload(_map())

    # Assert: no ``forces`` is how the template knows there is nothing to
    # simulate (with ``layout: "fixed"``) — the UMAP coordinates stay put.
    assert payload["controls"] == {"display": _DEFAULT_DISPLAY}
    assert "forces" not in payload["controls"]
    # No Documents slider either: that gate belongs to the Full graph alone.
    assert "documents" not in payload["controls"]
    assert all("docRank" not in node for node in payload["nodes"])


def test_payload_display_controls_are_a_copy_of_the_graph_defaults() -> None:
    payload = to_embedding_map_payload(_map())

    payload["controls"]["display"]["arrows"] = False

    assert _DEFAULT_DISPLAY["arrows"] is True


def test_payload_colours_clusters_through_the_legend_and_noise_grey() -> None:
    payload = to_embedding_map_payload(_map())

    # Act: the template's resolver — cluster_id -> legend colour.
    colour_by_cluster = {
        row["cluster_id"]: row["color"]
        for row in payload["legend"]
        if "cluster_id" in row
    }

    # Assert: every drawn point resolves to its cluster's hue, noise to grey.
    assert colour_by_cluster == {
        0: CLUSTER_PALETTE[0],
        1: CLUSTER_PALETTE[1],
        -1: NOISE_COLOUR,
    }
    assert {n["cluster_id"] for n in payload["nodes"]} <= set(colour_by_cluster)


def test_payload_keeps_the_colour_of_a_cluster_with_no_legend_row() -> None:
    # Arrange: a partially written run — cluster 1's points are stamped but
    # its ``memory_clusters`` row is missing, so no legend row carries its hue.
    embedding_map = _map()
    embedding_map.clusters = [c for c in embedding_map.clusters if c.cluster_id != 1]

    nodes = to_embedding_map_payload(embedding_map)["nodes"]

    # Assert: the orphan points ship their own palette colour (the template's
    # ``n.color`` wins); clustered and noise points stay lean.
    orphans = [n for n in nodes if n["cluster_id"] == 1]
    assert orphans
    assert {n.get("color") for n in orphans} == {cluster_colour(1)}
    assert all("color" not in n for n in nodes if n["cluster_id"] != 1)


def test_payload_ships_no_colour_on_a_healthy_map() -> None:
    nodes = to_embedding_map_payload(_map())["nodes"]

    assert all("color" not in n for n in nodes)


def test_payload_leaves_a_negative_cluster_id_to_the_template_noise_rule() -> None:
    # Arrange: a degenerate label below -1 (the pipeline normalises these to
    # -1, so this is defence in depth).
    embedding_map = _map()
    embedding_map.points[-1].cluster_id = -2

    nodes = to_embedding_map_payload(embedding_map)["nodes"]

    # Assert: no per-node colour — the template paints any negative id grey.
    assert "color" not in nodes[-1]
    assert nodes[-1]["meta"]["cluster"] == "noise"


def test_payload_nodes_carry_exactly_the_lean_key_set() -> None:
    payload = to_embedding_map_payload(_map())

    # Assert: no ``label`` (empty anyway), no ``color`` (the legend carries it),
    # no ``meta.document`` (the tooltip rebuilds it from ``name``).
    for node in payload["nodes"]:
        assert set(node) == {
            "id",
            "type",
            "name",
            "x",
            "y",
            "cluster_id",
            "size",
            "meta",
        }
        assert set(node["meta"]) == {"cluster", "heading_path", "snippet"}

    first = payload["nodes"][0]
    assert first["type"] == "chunk"
    assert first["name"] == "Paper"
    assert first["meta"] == {
        "cluster": "Cluster 0 topic",
        "heading_path": "Memory > Retrieval",
        "snippet": "Snippet 0.",
    }


def test_payload_rounds_coordinates_to_three_decimals() -> None:
    embedding_map = _map()
    embedding_map.points[0].x = 1.23456
    embedding_map.points[0].y = -7.65432

    node = to_embedding_map_payload(embedding_map)["nodes"][0]

    assert node["x"] == 1.235
    assert node["y"] == -7.654


def test_payload_omits_an_empty_heading_path_and_snippet() -> None:
    embedding_map = _map()
    embedding_map.points[0].heading_path = []
    embedding_map.points[0].snippet = ""

    nodes = to_embedding_map_payload(embedding_map)["nodes"]

    # Assert: the _curated_meta rule — an empty value is absent, not "".
    assert nodes[0]["meta"] == {"cluster": "Cluster 0 topic"}
    assert set(nodes[1]["meta"]) == {"cluster", "heading_path", "snippet"}


def test_payload_names_an_untitled_document() -> None:
    embedding_map = _map()
    embedding_map.points[0].title = None

    node = to_embedding_map_payload(embedding_map)["nodes"][0]

    assert node["name"] == "(untitled)"
    assert "document" not in node["meta"]


def test_payload_meta_labels_noise_points_as_noise() -> None:
    payload = to_embedding_map_payload(_map())

    noise_node = next(n for n in payload["nodes"] if n["cluster_id"] == -1)

    assert noise_node["meta"]["cluster"] == "noise"


def test_payload_legend_lists_clusters_by_size_then_the_two_greys() -> None:
    payload = to_embedding_map_payload(_map())

    # Assert: the coloured rows carry ``cluster_id`` (noise -1) for the
    # template's colour lookup; the not-drawn grey row carries none.
    assert payload["legend"] == [
        {
            "label": "Cluster 0 topic",
            "size": 30,
            "color": CLUSTER_PALETTE[0],
            "cluster_id": 0,
        },
        {
            "label": "Cluster 1 topic",
            "size": 12,
            "color": CLUSTER_PALETTE[1],
            "cluster_id": 1,
        },
        {"label": "noise", "size": 5, "color": NOISE_COLOUR, "cluster_id": -1},
        {
            "label": "unclustered / stale (not shown)",
            "size": 3,
            "color": UNCLUSTERED_COLOUR,
        },
    ]


def test_payload_legend_orders_clusters_largest_first() -> None:
    # Arrange: the small cluster comes FIRST in the map (store order is not a
    # contract of the builder).
    payload = to_embedding_map_payload(_map(sizes={0: 4, 1: 40}, total_children=52))

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

    assert payload["summary"] == (
        "Embedding map: 47 of 47 chunks (the 10 most-recent of 10 documents) "
        "in 2 clusters (+5 noise)"
    )


def test_payload_summary_says_how_much_the_document_cap_cut() -> None:
    # Arrange: 20 more chunks (4 of them noise) of this run belong to
    # documents beyond the 250 most-recent.
    embedding_map = _map(
        unclustered=0,
        total_children=67,
        cut_noise=4,
        plotted_documents=250,
        total_documents=1200,
    )

    payload = to_embedding_map_payload(embedding_map)

    # Assert: the header counts the PLOT (47 of the run's 67), the noise the RUN.
    assert payload["summary"] == (
        "Embedding map: 47 of 67 chunks (the 250 most-recent of 1200 documents) "
        "in 2 clusters (+9 noise)"
    )


def _query_map(matched: int = 2, reference: int = 3) -> QueryMap:
    return QueryMap(
        matched=[_point(index, -1) for index in range(matched)],
        reference=[_point(100 + index, -1) for index in range(reference)],
        plotted_documents=4,
        total_documents=5,
    )


def test_query_payload_draws_the_cloud_first_and_the_matches_on_top() -> None:
    payload = to_query_map_payload(_query_map(), "agent memory")

    # Assert: Sigma draws in insertion order — the matches must come last.
    ids = [node["id"] for node in payload["nodes"]]
    assert ids == ["chunk-100", "chunk-101", "chunk-102", "chunk-0", "chunk-1"]
    assert {n["cluster_id"] for n in payload["nodes"][:3]} == {-2}
    assert {n["cluster_id"] for n in payload["nodes"][3:]} == {-1}
    assert payload["nodes"][0]["size"] < payload["nodes"][-1]["size"]


def test_query_payload_legend_is_the_matches_then_the_faint_grey_cloud() -> None:
    payload = to_query_map_payload(_query_map(), "agent memory")

    assert payload["legend"] == [
        {
            "label": "matched chunks",
            "size": 2,
            "color": PREVIEW_COLOUR,
            "cluster_id": -1,
        },
        {
            "label": "other chunks",
            "size": 3,
            "color": REFERENCE_COLOUR,
            "cluster_id": -2,
        },
    ]
    # Opaque: Sigma's premultiplied blending turns a translucent grey into white.
    assert re.fullmatch(r"#[0-9a-f]{6}", REFERENCE_COLOUR)


def test_query_payload_carries_no_cluster_hulls_or_warning() -> None:
    payload = to_query_map_payload(_query_map(), "agent memory")

    # Clusters belong to the whole-memory map only.
    assert payload["hulls"] is None
    assert payload["warning"] is None
    assert all("cluster" not in node["meta"] for node in payload["nodes"])


def test_query_payload_summary_names_the_query_and_both_counts() -> None:
    payload = to_query_map_payload(_query_map(), "agent memory")

    assert payload["summary"] == (
        "Embedding map for 'agent memory': 2 matched chunks over 3 other chunks in "
        "grey (the 4 most-recent of 5 documents; a PCA projection, no clusters)"
    )


def test_payload_legend_counts_the_whole_run_not_the_plot() -> None:
    # Arrange: every noise chunk of the run sits in a cut document.
    embedding_map = _map(noise=0, unclustered=0, total_children=45, cut_noise=3)

    payload = to_embedding_map_payload(embedding_map)

    # Assert: cluster rows keep the run's size and the noise row is run-wide,
    # even though no noise point is drawn (ADR-013 §3, by design).
    assert [(row["label"], row["size"]) for row in payload["legend"]] == [
        ("Cluster 0 topic", 30),
        ("Cluster 1 topic", 12),
        ("noise", 3),
    ]
    assert all(node["cluster_id"] >= 0 for node in payload["nodes"])
    assert payload["warning"] is None


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
    assert '"size": 4' in html
    assert '"nodeSize": 4' not in html
    # …and so does everything that consumes them.
    assert "convexHull" in html
    assert "hulls-toggle" in html
    assert "graphToViewport" in html
    assert 'renderer.on("afterRender", drawOverlay)' in html
    assert 'renderer.on("resize", drawOverlay)' in html


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

    # The header line the reader sees is "47 of 47 chunks (the 10 most-recent
    # of 10 documents) in 2 clusters (+5 noise)", not "47 nodes · 0 edges" —
    # both the summary and the branch that shows it have to reach the browser.
    html = out.read_text(encoding="utf-8")
    assert (
        '"summary": "Embedding map: 47 of 47 chunks (the 10 most-recent of 10 '
        'documents) in 2 clusters (+5 noise)"'
    ) in html
    assert 'payload.summary.replace(/^Embedding map(?: for )?:? ?/, "")' in html


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
