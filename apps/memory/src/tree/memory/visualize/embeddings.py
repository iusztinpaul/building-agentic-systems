"""The **Embedding map** payload: an ``EmbeddingMap`` as the renderer draws it.

The map is the graph's twin, not its cousin (ADR-007 §2): the SAME
``{nodes, edges}`` **Graph payload** shape and the SAME template
(:mod:`tree.memory.visualize.graph`), with a handful of optional keys the
template understands — ``layout: "fixed"`` (draw the stored coordinates, run
no simulation), ``legend`` (cluster rows instead of node types), ``hulls`` and
``warning``. No second renderer, no second template — the query view
(:func:`to_query_map_payload`) is the same shape with two legend rows.

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

from tree.memory.clustering.types import EmbeddingMap, MapPoint, QueryMap
from tree.memory.visualize.graph import _DEFAULT_DISPLAY, _render_graph_file

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
    "No clustering run found for this user. Showing the raw embeddings. "
    "Trigger it manually."
)
"""The **unclustered preview**'s warning line: the map is drawn, but it is not
the topic map — clustering is manual only (``make memory-run-clustering-pipeline``)."""

NO_EMBEDDINGS_MESSAGE = (
    "No embedded chunks for this user yet — nothing to map. Ingest documents "
    "first (make memory-run-pipeline)."
)
"""What a surface says instead of drawing an empty map (ADR-007 §8)."""

PREVIEW_COLOUR = "#1f77b4"
"""One hue for every point of the **unclustered preview** — a real colour, not
noise grey: those points are not residue, they are simply not clustered yet."""

_PREVIEW_LABEL = "not clustered yet"
_MATCHED_LABEL = "matched chunks"
_REFERENCE_LABEL = "other chunks"
_REFERENCE_ID = -2
"""The query view's reference cloud — a legend id of its own so the template
paints it grey, distinct from the matches (``-1``)."""

REFERENCE_COLOUR = "#bcc1c9"
"""Light grey for the query view's reference cloud: lighter than noise grey, so
the matched chunks read on top and the cloud reads as context. OPAQUE on
purpose: Sigma blends with ``gl.blendFunc(ONE, ONE_MINUS_SRC_ALPHA)`` (it
expects premultiplied colours), so an ``rgba(…, 0.3)`` grey adds past 255 on the
white canvas and renders as white — invisible."""

_REFERENCE_NODE_SIZE = 3
"""One step smaller than a matched point (``_MAP_NODE_SIZE``): the cloud is
context, not the answer."""

_MAP_NODE_SIZE = 4
"""Node radius for every map point. Smaller than a graph entity's 6: a map plots
hundreds of chunks where a graph plots dozens, and at size 6 the clusters read
as blobs."""

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
    numbers and the two ways to fix it (cluster now, or wait for the offline
    pipeline).
    """

    if embedding_map.run_id is None:
        return NO_CLUSTERING_RUN_MESSAGE
    if embedding_map.unclustered <= 0:
        return None
    return (
        f"{embedding_map.unclustered}/{embedding_map.total_children} chunks don’t "
        "have a 2D embedding. Run the clustering algorithm manually or wait for "
        "the scheduled offline pipeline to compute them."
    )


def to_embedding_map_payload(
    embedding_map: EmbeddingMap, *, hulls: bool = False
) -> dict[str, Any]:
    """Flatten an **Embedding map** into the payload the ONE template renders.

    Every point becomes a node at its STORED coordinates (``layout: "fixed"``,
    and ``controls`` carrying no ``forces``, tell the template to build no
    simulation); there are no edges — the map's structure is proximity, not
    relationships. Dragging, pinning and the display knobs still apply.

    The legend counts the RUN, the header counts the PLOT — by design (ADR-013
    §3), do not "fix" one to match the other: the points are only the chunks of
    the ``plotted_documents`` most-recent documents, so the ``summary`` reads
    ``N of M chunks (the P most-recent of D documents) in K clusters (+X
    noise)``, while the legend's cluster sizes, its noise row and the stale
    warning describe the whole **Clustering run** — clusters are labelled from
    the whole memory, whichever slice of it is drawn.

    Args:
        embedding_map: The map a surface read from storage; never computed here.
        hulls: Initial state of the "Cluster hulls" toggle. Passing it (rather
            than leaving the key out) is what SHOWS the toggle at all.

    Returns:
        ``{nodes, edges, layout, controls, hulls, legend, warning, summary}``.
    """

    labels = {cluster.cluster_id: cluster.label for cluster in embedding_map.clusters}
    preview = embedding_map.run_id is None

    nodes: list[dict[str, Any]] = []
    for point in embedding_map.points:
        node = _map_node(
            point,
            cluster=_PREVIEW_LABEL
            if preview
            else _cluster_label(labels, point.cluster_id),
        )
        # The one exception: a cluster with NO legend row (a partially written
        # run — points stamped, ``memory_clusters`` row missing) ships its own
        # palette colour, which the template prefers. Never on a healthy map.
        if point.cluster_id >= 0 and point.cluster_id not in labels:
            node["color"] = cluster_colour(point.cluster_id)
        nodes.append(node)

    legend = _legend_rows(embedding_map)
    clusters = (
        "— no clusters yet (unclustered preview)"
        if preview
        else (
            f"in {len(embedding_map.clusters)} clusters (+{embedding_map.noise} noise)"
        )
    )
    # The preview has nothing to outline: ``None`` hides the hull toggle.
    hulls_state: bool | None = None if preview else hulls
    # The run's ``clustered`` is the preview's whole corpus of embedded chunks.
    of = embedding_map.total_children if preview else embedding_map.clustered

    return {
        "nodes": nodes,
        "edges": [],
        "layout": "fixed",
        "controls": {"display": dict(_DEFAULT_DISPLAY)},
        "hulls": hulls_state,
        "legend": legend,
        "warning": unclustered_warning(embedding_map),
        "summary": (
            f"Embedding map: {len(embedding_map.points)} of {of} chunks "
            f"(the {embedding_map.plotted_documents} most-recent of "
            f"{embedding_map.total_documents} documents) {clusters}"
        ),
    }


def to_query_map_payload(query_map: QueryMap, query: str) -> dict[str, Any]:
    """The query view: the matched chunks in colour over the rest in light grey.

    The points come from :func:`~tree.memory.clustering.store.load_query_map` —
    ONE PCA space for both — so the grey reference cloud shows where the
    matches sit in the wider memory. No clusters (they belong to the
    whole-memory map), no hull toggle (``hulls: None``), no stale warning. The
    reference nodes go FIRST: Sigma draws in insertion order, so a match is
    never buried under the cloud. Same fixed-layout template and lean nodes as
    :func:`to_embedding_map_payload`; the grey is opaque (see
    :data:`REFERENCE_COLOUR`).

    Returns:
        ``{nodes, edges, layout, controls, hulls, legend, warning, summary}``.
    """

    nodes = [
        {**_map_node(point), "cluster_id": _REFERENCE_ID, "size": _REFERENCE_NODE_SIZE}
        for point in query_map.reference
    ]
    nodes += [_map_node(point) for point in query_map.matched]
    return {
        "nodes": nodes,
        "edges": [],
        "layout": "fixed",
        "controls": {"display": dict(_DEFAULT_DISPLAY)},
        "hulls": None,
        "legend": [
            {
                "label": _MATCHED_LABEL,
                "size": len(query_map.matched),
                "color": PREVIEW_COLOUR,
                "cluster_id": -1,
            },
            {
                "label": _REFERENCE_LABEL,
                "size": len(query_map.reference),
                "color": REFERENCE_COLOUR,
                "cluster_id": _REFERENCE_ID,
            },
        ],
        "warning": None,
        "summary": (
            f"Embedding map for {query!r}: {len(query_map.matched)} matched chunks "
            f"over {len(query_map.reference)} other chunks in grey (the "
            f"{query_map.plotted_documents} most-recent of "
            f"{query_map.total_documents} documents; a PCA projection, no clusters)"
        ),
    }


def _map_node(point: MapPoint, *, cluster: str | None = None) -> dict[str, Any]:
    """One lean map node; ``cluster`` names the tooltip's cluster row, if any."""

    meta: dict[str, Any] = {} if cluster is None else {"cluster": cluster}
    # Omitted when empty (the ``_curated_meta`` rule): an empty tooltip row
    # carries nothing, and on a 17k-point map every key is ~17k copies.
    if point.heading_path:
        meta["heading_path"] = " > ".join(point.heading_path)
    if point.snippet:
        meta["snippet"] = point.snippet
    # Lean on purpose (ADR-013 §3): no ``label`` (hundreds of overlapping
    # chunk titles are canvas noise — the template defaults it to ""), no
    # ``color`` (the template looks it up from the legend's ``cluster_id``)
    # and no ``meta.document`` (the tooltip rebuilds it from ``name``).
    # Three decimals is sub-pixel on any UMAP extent a screen can show.
    return {
        "id": point.chunk_id,
        "type": "chunk",
        "name": point.title or _UNTITLED,  # the tooltip's heading
        "x": round(point.x, 3),
        "y": round(point.y, 3),
        "cluster_id": point.cluster_id,
        "size": _MAP_NODE_SIZE,
        "meta": meta,
    }


def _cluster_label(labels: dict[int, str], cluster_id: int) -> str:
    """The tooltip's cluster name; ``noise`` for -1, a stable stand-in if a
    cluster row is missing (an all-noise or partially-written run)."""

    if cluster_id < 0:
        return "noise"
    return labels.get(cluster_id) or f"Cluster {cluster_id}"


def _legend_rows(embedding_map: EmbeddingMap) -> list[dict[str, Any]]:
    """One row per cluster (largest first), then the two grey rows if non-empty.

    Run-wide counts: a cluster row's ``size`` is the run's, the noise row is
    ``embedding_map.noise`` — not what the document cap left on the plot.

    The greys come last and only when they exist: an empty "noise · 0" row
    would read as a cluster the reader cannot find on the map. Every DRAWN
    row carries its ``cluster_id`` (noise ``-1``) — the template's only source
    of a point's colour; the "unclustered / stale" row has no points, so none.

    The **unclustered preview** has ONE row instead: every point, one colour,
    under the noise id the template paints by.
    """

    if embedding_map.run_id is None:
        return [
            {
                "label": _PREVIEW_LABEL,
                "size": embedding_map.total_children,
                "color": PREVIEW_COLOUR,
                "cluster_id": -1,
            }
        ]

    rows: list[dict[str, Any]] = [
        {
            "label": cluster.label,
            "size": cluster.size,
            "color": cluster_colour(cluster.cluster_id),
            "cluster_id": cluster.cluster_id,
        }
        for cluster in sorted(
            embedding_map.clusters, key=lambda cluster: cluster.size, reverse=True
        )
    ]
    if embedding_map.noise > 0:
        rows.append(
            {
                "label": "noise",
                "size": embedding_map.noise,
                "color": NOISE_COLOUR,
                "cluster_id": -1,
            }
        )
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
    payload: dict[str, Any], output: str | Path | None = None
) -> Path:
    """Write the map as a self-contained HTML file and return its path.

    The graph's file convention, with the map's stem: ``output`` wins, else
    ``.tree/graphs/embedding-map-<UTC-stamp>.html``.

    ``output`` takes a ``str`` as well as a ``Path`` — it comes straight off a
    CLI ``--output`` flag (``visualize_query_result`` accepts the same), and a
    raw string would otherwise reach ``Path.parent`` inside the writer and
    raise ``AttributeError``.
    """

    return _render_graph_file(
        payload, query="embedding-map", output=Path(output) if output else None
    )
