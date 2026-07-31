"""Push code/spec updates to the Prefect Cloud deployments — the CD entrypoint.

Unlike ``deploy/prefect_pipelines_setup.py`` (which provisions the Managed work
pool + config blocks), this only re-applies the deployment definitions via
:func:`tree.orchestrator.deploy_cloud_pipelines` and exits. It is what
``.github/workflows/cd.yml`` runs on every push to ``main`` so the Cloud
deployments track the code, while Prefect's managed workers execute the runs.

It assumes ``prefect_pipelines_setup.py up`` has already created the
``tree-managed`` work pool and the ``tree-*`` Secret blocks / Variables. The
managed run env is a static ``{{ prefect.blocks/variables.* }}`` mapping (no raw
values), and the git clone authenticates via the ``tree-github-pat`` Secret block
(a run-time reference), so CD needs only ``PREFECT_API_URL`` / ``PREFECT_API_KEY``
— NOT the app's secrets.

``GIT_REF`` (optional) pins the deployments to a branch or commit; defaults to
``main`` (branch-tracking, so merges go live without a re-deploy). The CD workflow
can pass the tested commit SHA for reproducible deploys.

``GROUPS`` (optional, comma-separated) narrows the deploy to whole pipelines —
``data`` (data ETL coordinator + worker) and/or ``memory`` (extraction, indexing,
dream). Unset deploys all of them.

Usage::

    make memory-deploy-prefect
    make memory-deploy-prefect GROUPS=data
    GROUPS=data,memory uv run python deploy/prefect_pipelines.py
"""

import logging
import os

from tree.logging import init_logger
from tree.orchestrator import (
    MANAGED_WORK_POOL,
    deploy_cloud_pipelines,
    managed_env_templates,
)

init_logger()
logger = logging.getLogger(__name__)


def main() -> None:
    git_ref = os.environ.get("GIT_REF") or "main"
    groups = tuple(
        g.strip() for g in os.environ.get("GROUPS", "").split(",") if g.strip()
    )
    deployment_ids = deploy_cloud_pipelines(
        work_pool_name=MANAGED_WORK_POOL,
        git_ref=git_ref,
        job_env=managed_env_templates(),
        groups=groups,
    )
    logger.info(
        "Applied %d Prefect deployment(s) on %s @ %s: %s",
        len(deployment_ids),
        MANAGED_WORK_POOL,
        git_ref,
        ", ".join(deployment_ids),
    )


if __name__ == "__main__":
    main()
