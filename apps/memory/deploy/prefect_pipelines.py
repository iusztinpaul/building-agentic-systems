"""Register/update Prefect deployments on the configured API — the CD entrypoint.

Unlike ``make memory-serve-workflows`` (which serves AND blocks as the worker),
this script only *applies* the deployment definitions returned by
:func:`tree.orchestrator.build_deployments` against ``PREFECT_API_URL`` and then
exits. It is what ``.github/workflows/cd.yml`` runs on every push to ``main`` so
the Prefect Cloud deployments stay in lock-step with the code, while the
long-running worker runs elsewhere.

Requires ``PREFECT_API_URL`` and ``PREFECT_API_KEY`` in the environment (the CD
workflow injects them from repository secrets).

Usage::

    make memory-deploy-prefect
    uv run python deploy/prefect_pipelines.py
"""

import asyncio
import logging

from tree.logging import init_logger
from tree.orchestrator import apply_deployments

init_logger()
logger = logging.getLogger(__name__)


def main() -> None:
    deployment_ids = asyncio.run(apply_deployments())
    logger.info(
        "Applied %d Prefect deployment(s): %s",
        len(deployment_ids),
        ", ".join(str(d) for d in deployment_ids),
    )


if __name__ == "__main__":
    main()
