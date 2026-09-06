"""The **Embedding map** payload: an ``EmbeddingMap`` as the renderer draws it.

The map is the graph's twin, not its cousin (ADR-007 §2): the SAME
``{nodes, edges}`` **Graph payload** shape and the SAME template
(:mod:`tree.memory.visualize.graph`), with a handful of optional keys the
template understands — ``layout: "fixed"`` (draw the stored coordinates, skip
ForceAtlas2), ``legend`` (cluster rows instead of node types), ``hulls``,
``warning`` and ``nodeSize``. No second renderer, no second template.

Pure and synchronous: it takes the :class:`~tree.memory.clustering.types.\
EmbeddingMap` a surface already READ (``clustering.store.load_embedding_map``)
and returns a JSON-safe dict. Nothing here touches Mongo or MCP — that is what
makes the same builder serve the MCP tool, the CLI and a test with a
hand-written map.

Two greys carry the two things a reader must not confuse:

* :data:`NOISE_COLOUR` — points HDBSCAN refused to group. They are drawn (they
  have coordinates) but have no cluster and no hull.
* :data:`UNCLUSTERED_COLOUR` — **Child chunk**s with no assignment or a stale
  one. They have no coordinates from this run, so they are NOT drawn at all;
  they are counted in the legend and in :func:`unclustered_warning`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree.memory.clustering.types import EmbeddingMap
from tree.memory.visualize.graph import _render_graph_file

CLUSTER_PALETTE: tuple[str, ...] = (
    "#1f77b4",
    "#aec7e8",
    "#ff7f0e",
    "#ffbb78",
    "#2ca02c",
    "#98df8a",
    "#d62728",
    "#ff9896",
    "#9467bd",
    "#c5b0d5",
    "#8c564b",
    "#c49c94",
    "#e377c2",
    "#f7b6d2",
    "#bcbd22",
    "#dbdb8d",
    "#17becf",
    "#9edae5",
    "#393b79",
    "#e7ba52",
)
"""The 20 tableau-style hues a cluster is coloured by (ADR-007 §2).

Categorical, not sequential: cluster ids carry no order, so neighbouring ids
must not read as neighbouring shades. Twenty is the point where a reader stops
being able to tell hues apart in a legend — beyond it the palette WRAPS rather
than growing, and the hull plus the legend size disambiguate the repeat.
"""

NOISE_COLOUR = "#9e9e9e"
"""Mid-grey for HDBSCAN noise (``cluster_id == -1``) — drawn, never grouped."""

UNCLUSTERED_COLOUR = "#d5d8de"
"""Light grey for the legend row of chunks that are NOT on the map at all."""

NO_CLUSTERING_RUN_MESSAGE = (
    "No clustering run found for this user — run make "
    "memory-run-clustering-pipeline to build the embedding map."
)
"""What a surface says instead of drawing an empty map (ADR-007 §8)."""

_MAP_NODE_SIZE = 4
"""Node radius for the map. Smaller than the graph's 6: a map plots hundreds of
chunks where a graph plots dozens, and at size 6 the clusters read as blobs."""

_UNTITLED = "(untitled)"


def cluster_colour(cluster_id: int) -> str:
    """The colour of one **Memory cluster**, wrapping the palette at 20.

    Noise (``-1``, and any other negative label the recipe maps onto it) is
    grey — it is the absence of a cluster, so it never consumes a palette slot
    and never collides with a real cluster's colour.
    """

    if cluster_id < 0:
        return NOISE_COLOUR
    return CLUSTER_PALETTE[cluster_id % len(CLUSTER_PALETTE)]


def unclustered_warning(embedding_map: EmbeddingMap) -> str | None:
    """The stale-map warning line, or ``None`` when every chunk is on the map.

    The warning contract of ADR-007 §8: chunks ingested after the last
    **Clustering run** (or carrying an older run's coordinates) cannot be
    placed, so the map silently under-reports the corpus. The line names both
    numbers and the command that fixes it.
    """

    if embedding_map.unclustered <= 0:
        return None
    return (
        f"{embedding_map.unclustered} of {embedding_map.total_children} chunks "
        "have no cluster assignment (or a stale one) — run make "
        "memory-run-clustering-pipeline"
    )


def to_embedding_map_payload(
    embedding_map: EmbeddingMap, *, hulls: bool = False
) -> dict[str, Any]:
    """Flatten an **Embedding map** into the payload the ONE template renders.

    Every point becomes a node at its STORED coordinates (``layout: "fixed"``
    tells the template to skip ForceAtlas2); there are no edges — the map's
    structure is proximity, not relationships.

    Args:
        embedding_map: The map a surface read from storage; never computed here.
        hulls: Initial state of the "Cluster hulls" toggle. Passing it (rather
            than leaving the key out) is what SHOWS the toggle at all.

    Returns:
        ``{nodes, edges, layout, nodeSize, hulls, legend, warning, summary}``.
    """

    labels = {cluster.cluster_id: cluster.label for cluster in embedding_map.clusters}

    nodes: list[dict[str, Any]] = []
    for point in embedding_map.points:
        title = point.title or _UNTITLED
        nodes.append(
            {
                "id": point.chunk_id,
                "type": "chunk",
                "name": title,  # the tooltip's heading
                # No canvas text: hundreds of overlapping chunk titles are
                # noise, and the tooltip already carries the full story.
                "label": "",
                "x": point.x,
                "y": point.y,
                "cluster_id": point.cluster_id,
                "color": cluster_colour(point.cluster_id),
                "meta": {
                    "cluster": _cluster_label(labels, point.cluster_id),
                    "document": title,
                    "heading_path": " > ".join(point.heading_path),
                    "snippet": point.snippet,
                },
            }
        )

    noise = sum(1 for point in embedding_map.points if point.cluster_id < 0)
    legend = _legend_rows(embedding_map, noise=noise)

    return {
        "nodes": nodes,
        "edges": [],
        "layout": "fixed",
        "nodeSize": _MAP_NODE_SIZE,
        "hulls": hulls,
        "legend": legend,
        "warning": unclustered_warning(embedding_map),
        "summary": (
            f"Embedding map: {len(embedding_map.points)} chunks in "
            f"{len(embedding_map.clusters)} clusters (+{noise} noise)"
        ),
    }


def _cluster_label(labels: dict[int, str], cluster_id: int) -> str:
    """The tooltip's cluster name; ``noise`` for -1, a stable stand-in if a
    cluster row is missing (an all-noise or partially-written run)."""

    if cluster_id < 0:
        return "noise"
    return labels.get(cluster_id) or f"Cluster {cluster_id}"


def _legend_rows(embedding_map: EmbeddingMap, *, noise: int) -> list[dict[str, Any]]:
    """One row per cluster (largest first), then the two grey rows if non-empty.

    The greys come last and only when they exist: an empty "noise · 0" row
    would read as a cluster the reader cannot find on the map.
    """

    rows: list[dict[str, Any]] = [
        {
            "label": cluster.label,
            "size": cluster.size,
            "color": cluster_colour(cluster.cluster_id),
        }
        for cluster in sorted(
            embedding_map.clusters, key=lambda cluster: cluster.size, reverse=True
        )
    ]
    if noise > 0:
        rows.append({"label": "noise", "size": noise, "color": NOISE_COLOUR})
    if embedding_map.unclustered > 0:
        rows.append(
            {
                "label": "unclustered / stale (not shown)",
                "size": embedding_map.unclustered,
                "color": UNCLUSTERED_COLOUR,
            }
        )
    return rows


def render_embedding_map_file(
    payload: dict[str, Any], output: Path | None = None
) -> Path:
    """Write the map as a self-contained HTML file and return its path.

    The graph's file convention, with the map's stem: ``output`` wins, else
    ``.tree/graphs/embedding-map-<UTC-stamp>.html``.
    """

    return _render_graph_file(payload, query="embedding-map", output=output)
