"""
Render the **Embedding map** of one user's memory as an interactive HTML file.

READS the latest **Clustering run** and draws it (ADR-007 Decision 8) — it never
clusters. `make memory-run-clustering-pipeline` is what produces the data; this
command only turns the stored `cluster_id` / `viz {x, y, run_id}` fields into a
picture, so it is instant and needs no served workflows, no Prefect and no LLM.
It plots the chunks of EVERY document — a local file has no size limit; the
``visualize_memory_embeddings`` MCP tool keeps ``query.full_graph_max_docs``
(250) because its resource read fails through Horizon on a larger map. The
summary says ``N of M chunks`` and the legend counts the whole run.

``--query`` draws the child chunks a hybrid search matched in colour over every
other chunk in grey — no cluster colours. Only this command draws the grey
cloud: the MCP tool shows the matches alone, because the cloud pushed its
inline payload past what Horizon delivers.

Three outcomes that are NOT failures of the renderer:

* No embedded chunk for this user → the command prints the "nothing to map"
  message and exits 1, rather than opening an empty canvas.
* No run for this user → nothing has 2D coordinates: the command prints the
  "N/N chunks don’t have a 2D embedding. …" warning, writes no file and exits 1.
* Chunks ingested since the last run → the FIRST line of stdout is the warning
  "N/M chunks don’t have a 2D embedding. …"; those points are
  omitted from the map and counted in its legend.

A separate command rather than a flag on ``visualize_structure.py``: that one
draws rows and their links and branches on the mode, while the map draws stored
coordinates and is identical in ``rag`` and ``graphrag``.

Every read is scoped to a ``user_id`` (#020): defaults to the current-session
user; override with ``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>`` (the
Makefile wires these).

Usage:
    make memory-visualize-embeddings                          # current user
    make memory-visualize-embeddings HULLS=true               # outline clusters
    make memory-visualize-embeddings QUERY="Agentic RAG"      # matches over grey
    make memory-visualize-embeddings USER_IDENTIFIER=paul OUTPUT=/tmp/map.html
    uv run python scripts/visualize_embeddings.py --hulls --no-open
"""

import asyncio
import logging
import webbrowser
from typing import Any

import click
from beanie import PydanticObjectId

from tree.cli import user_options
from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.sessions import resolve_user_id
from tree.logging import init_logger
from tree.memory.clustering.store import load_embedding_map, load_query_map
from tree.memory.rag.retrieval import retrieve_parents
from tree.memory.rag.search import SearchUnavailableError
from tree.memory.rag.structure import NO_RESULTS_MESSAGE
from tree.memory.visualize.embeddings import (
    NO_EMBEDDINGS_MESSAGE,
    render_embedding_map_file,
    to_embedding_map_payload,
    to_query_map_payload,
    unclustered_warning,
)
from tree.models.get_model import get_embedding_model

init_logger()
logger = logging.getLogger(__name__)


async def _query_payload(
    client: Any, database: str, user_id: PydanticObjectId, query: str, top_k: int
) -> dict[str, Any]:
    """The query view's payload: the matches over the grey reference cloud."""

    try:
        retrieval = await retrieve_parents(
            client, database, query, get_embedding_model(), user_id, top_k=top_k
        )
    except SearchUnavailableError as exc:
        click.echo(f"Search unavailable: {exc} — retryable")
        raise SystemExit(1) from exc
    hit_ids = {
        child.child_id
        for parent in retrieval.parents
        for child in parent.matched_children
    }
    if not hit_ids:
        click.echo(NO_RESULTS_MESSAGE.format(query=query))
        raise SystemExit(1)
    query_map = await load_query_map(client, database, user_id, hit_ids, capped=False)
    payload = to_query_map_payload(query_map, query)
    if not query_map.matched:
        click.echo(payload["warning"] or NO_RESULTS_MESSAGE.format(query=query))
        raise SystemExit(1)
    return payload


async def _run(
    user_id: str | None,
    user_identifier: str | None,
    query: str | None,
    top_k: int,
    hulls: bool,
    output: str | None,
    no_open: bool,
) -> None:
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = settings.mongo.mongo_initdb_database
    resolved_user_id = await resolve_user_id(user_id, user_identifier)

    if query:
        payload = await _query_payload(client, database, resolved_user_id, query, top_k)
    else:
        embedding_map = await load_embedding_map(
            client, database, resolved_user_id, capped=False
        )
        if embedding_map is None:
            click.echo(NO_EMBEDDINGS_MESSAGE)
            raise SystemExit(1)
        if not embedding_map.points:
            click.echo(unclustered_warning(embedding_map) or NO_EMBEDDINGS_MESSAGE)
            raise SystemExit(1)
        payload = to_embedding_map_payload(embedding_map, hulls=hulls)
    # The warning comes BEFORE the path: an operator who reads one line of
    # output must learn the map is stale, not where it was written.
    if payload["warning"]:
        click.echo(payload["warning"])

    path = render_embedding_map_file(payload, output)
    if not no_open:
        try:
            webbrowser.open(path.resolve().as_uri())
        except Exception:  # noqa: BLE001 — a missing browser never fails a render.
            logger.debug("Could not open a browser for %s", path, exc_info=True)

    click.echo(f"Wrote {path}")
    click.echo(payload["summary"])


@click.command()
@user_options
@click.option(
    "--query",
    "-q",
    default=None,
    help="Draw the chunks matching this query over the rest in grey (no clusters).",
)
@click.option(
    "--top-k",
    "-k",
    type=click.IntRange(min=1),
    default=app_config.query.top_k,
    show_default=True,
    help="Parent chunks to retrieve with --query; their matched children are drawn.",
)
@click.option(
    "--hulls/--no-hulls",
    default=False,
    show_default=True,
    help="Outline each cluster with a convex hull (toggleable in the viewer).",
)
@click.option(
    "--output",
    "-o",
    default=None,
    help=(
        "Output HTML file. Defaults to "
        ".tree/graphs/embedding-map-<UTC-stamp>.html under the repo root."
    ),
)
@click.option(
    "--no-open",
    is_flag=True,
    default=False,
    help="Don't open the browser automatically.",
)
def main(
    user_id: str | None,
    user_identifier: str | None,
    query: str | None,
    top_k: int,
    hulls: bool,
    output: str | None,
    no_open: bool,
) -> None:
    """Render the resolved user's embedding map from the latest clustering run."""

    asyncio.run(_run(user_id, user_identifier, query, top_k, hulls, output, no_open))


if __name__ == "__main__":
    main()
