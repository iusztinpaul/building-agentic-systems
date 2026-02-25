"""
Prefect workflow orchestrator.

Registers and serves all workflow deployments.
Start the Prefect server first, then run this file to serve the workflows.

Usage:
    make prefect-server    # in one terminal
    make serve-workflows   # in another terminal
"""

from prefect import serve

from twin.data.substack_rss import run_substack_rss_etl

if __name__ == "__main__":
    serve(
        run_substack_rss_etl.to_deployment(
            name="substack-rss-etl",
            tags=["data-pipeline", "substack"],
        ),
    )
