"""
Render the **Embedding map** of one user's memory as an interactive HTML file.

READS the latest **Clustering run** and draws it (ADR-007 Decision 8) — it never
clusters. `make memory-run-clustering-pipeline` is what produces the data; this
command only turns the stored `cluster_id` / `viz {x, y, run_id}` fields into a
picture, so it is instant and needs no served workflows, no Prefect and no LLM.

Two outcomes that are NOT failures of the renderer:

* No run for this user → the command prints the "run make
  memory-run-clustering-pipeline" message and exits 1, rather than opening an
  empty canvas.
* Chunks ingested since the last run → the FIRST line of stdout is the warning
  "N of M chunks have no cluster assignment (or a stale one)"; those points are
  omitted from the map and counted in its legend.

A separate command rather than a flag on ``query_graph.py``: that one is
query-driven and mode-branching, while the map takes no query and is identical
in ``rag`` and ``graphrag``.

Every read is scoped to a ``user_id`` (#020): defaults to the current-session
user; override with ``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>`` (the
Makefile wires these).

Usage:
    make memory-visualize-embeddings                          # current user
    make memory-visualize-embeddings HULLS=true               # outline clusters
    make memory-visualize-embeddings USER_IDENTIFIER=paul OUTPUT=/tmp/map.html
    uv run python scripts/visualize_embeddings.py --hulls --no-open
"""

import asyncio
import logging
import webbrowser

import click

from tree.cli import user_options
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.sessions import resolve_user_id
from tree.logging import init_logger
from tree.memory.clustering.store import load_embedding_map
from tree.memory.visualize.embeddings import (
    NO_CLUSTERING_RUN_MESSAGE,
    render_embedding_map_file,
    to_embedding_map_payload,
)

init_logger()
logger = logging.getLogger(__name__)


async def _run(
    user_id: str | None,
    user_identifier: str | None,
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

    embedding_map = await load_embedding_map(client, database, resolved_user_id)
    if embedding_map is None:
        click.echo(NO_CLUSTERING_RUN_MESSAGE)
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
    hulls: bool,
    output: str | None,
    no_open: bool,
) -> None:
    """Render the resolved user's embedding map from the latest clustering run."""

    asyncio.run(_run(user_id, user_identifier, hulls, output, no_open))


if __name__ == "__main__":
    main()
