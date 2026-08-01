"""
Prefect workflow orchestrator.

Single source of truth for the workflow deployment topology (:data:`_DEPLOYMENT_SPECS`),
consumed by two execution models that must never drift apart:

* **Local serve** (``make memory-serve-workflows`` → :func:`serve_deployments`):
  registers AND executes the deployments from LOCAL code via the in-process
  Prefect runner, against whatever ``PREFECT_API_URL`` points at (the local
  Prefect server in dev — ``make local-start``). Used for development.

* **Cloud managed** (:func:`deploy_cloud_pipelines`): registers the deployments
  on Prefect **Cloud** bound to a Prefect **Managed** work pool, with flow code
  pulled from the GitHub repo at run time. Prefect hosts the workers, so
  submitted runs execute without any self-hosted process. Provisioned by
  ``deploy/prefect_pipelines_setup.py`` (``up``); kept in sync by the CD path
  (``deploy/prefect_pipelines.py``), which only pushes code/spec updates.

Every flow exposes ``user_id`` as a required parameter — operators MUST pass it
when triggering a deployment, e.g.::

    prefect deployment run \\
        memory-extract-etl-coordinator/memory-extract-etl-coordinator \\
        -p user_id=507f1f77bcf86cd799439011
"""

from dataclasses import dataclass, field
from typing import Any

from prefect import Flow, serve
from prefect.blocks.system import Secret
from prefect.runner.storage import GitRepository
from prefect.schedules import Cron

from tree.config.app_config import app_config
from tree.data.offline_pipeline import data_etl_coordinator, data_etl_worker
from tree.offline import TAGS_ETL_OFFLINE, etl_offline
from tree.online import TAGS_ETL_ONLINE, etl_online
from tree.memory.consolidation.dream import dream_consolidation_all_users
from tree.memory.extraction.pipeline import (
    memory_extract_etl_coordinator,
    memory_extract_etl_worker,
)
from tree.memory.indexing.pipeline import memory_indexing
from tree.config.constants import (
    TAGS_DATA_OFFLINE,
    TAGS_EXTRACTION,
    TAGS_INDEXING,
    TAG_DATA_PIPELINE,
    TAG_MEMORY_PIPELINE,
)
from tree.observability import configure_opik

# Prefect Cloud managed-pool defaults (provisioned by deploy/prefect_pipelines_setup.py).
GIT_URL = "https://github.com/iusztinpaul/building-agentic-systems.git"
MANAGED_WORK_POOL = "tree-managed"
# Secret block holding the GitHub PAT Prefect uses to clone this private repo.
PAT_BLOCK_NAME = "tree-github-pat"
# The managed-run image. Pin Python 3.14 — the default (``3-latest``) is 3.12,
# and ``tree-memory`` is ``requires-python >= 3.14`` so the per-run install fails
# on anything older. ``prefect-client`` (slim) matches the pool's default flavor.
MANAGED_IMAGE = "prefecthq/prefect-client:3-python3.14"


class _GitRepoWithPipInstall(GitRepository):
    """``GitRepository`` whose pull steps ALSO ``pip install`` the cloned package.

    Prefect Managed loads the flow's entrypoint module after the pull steps but
    before our code runs, and ``env``/``PYTHONPATH`` against the clone doesn't make
    the src-layout ``tree`` package importable. So after the ``git_clone`` step we
    add a ``pip install ./apps/memory`` step (run in the clone dir): it installs
    ``tree`` + its now-slim ``[project.dependencies]`` (the heavy
    ``sentence-transformers``/``modal`` backends are in the opt-in ``local-models``
    extra, NOT pulled), so ``import tree`` works at flow load.

    Security: the clone authenticates via the :data:`PAT_BLOCK_NAME` Secret block
    (a block REFERENCE in the pull step, resolved at run time — never a literal),
    and the install runs on the local clone, so no token lands in any pip URL,
    deployment config, or run log. ``to_pull_step`` may return a list (Prefect
    splices it into the deployment's pull steps verbatim).
    """

    CLONE_DIR = "repo"

    def to_pull_step(self) -> list:  # type: ignore[override]
        clone = super().to_pull_step()
        clone_step = clone[0] if isinstance(clone, list) else clone
        clone_step[next(iter(clone_step))]["clone_directory_name"] = self.CLONE_DIR
        return [
            clone_step,
            {
                "prefect.deployments.steps.run_shell_script": {
                    "directory": self.CLONE_DIR,
                    # ``--ignore-requires-python``: on bleeding-edge Python 3.14 some
                    # pure-Python deps (e.g. beanie 2.0.1) still declare
                    # ``requires_python <3.14``, so pip filters them out ("No
                    # matching distribution"). The pinned set IS 3.14-compatible
                    # (uv installs + the suite passes on 3.14); the flag tells pip
                    # to install past the stale metadata.
                    "script": "pip install --ignore-requires-python ./apps/memory",
                    "stream_output": True,
                }
            },
        ]


# Nightly schedule for the data pipeline's scheduled run (UTC). The cron fires
# ``data-etl-coordinator`` with ``source_files=["sources/listen.yaml"]`` and no
# ``user_id`` — so it ingests the polled listen feeds, fanned out across all active
# users.
_SCHEDULED_INGEST_CRON = "0 3 * * *"


@dataclass(frozen=True)
class _DeploymentSpec:
    """One deployment's topology — shared by the local-serve and cloud paths.

    ``flow`` is the imported flow object (used for local ``to_deployment`` and as
    the ``from_source`` accessor). ``entrypoint`` is the repo-relative
    ``path:function`` Prefect's managed worker loads after cloning the repo.
    ``cron`` (+ optional ``schedule_parameters``) attaches ONE schedule to the
    deployment whose runs override the flow's default parameters — e.g. the data
    coordinator's nightly cron passes ``source_files=["sources/listen.yaml"]``.
    ``optional`` marks a deployment as beyond the free-tier 5 — registered only when
    ``app_config.prefect.deploy_optional`` is true.
    """

    flow: Flow
    name: str
    entrypoint: str
    tags: list[str]
    cron: str | None = None
    schedule_parameters: dict[str, Any] = field(default_factory=dict)
    optional: bool = False

    def schedules(self) -> list[Cron] | None:
        """The deployment's schedule list, or ``None`` when it has no cron.

        Each schedule carries ``schedule_parameters`` so scheduled runs differ
        from manual ones (Prefect overrides only the listed params; the rest fall
        back to the flow defaults).
        """

        if self.cron is None:
            return None
        return [Cron(self.cron, parameters=self.schedule_parameters)]


# The first 5 are the always-on CORE set (free-tier safe). The trailing online
# and dream deployments are OPTIONAL (``optional=True``): registered only when
# ``app_config.prefect.deploy_optional`` is true — see ``_active_deployment_specs``.
# Deployable subsets, keyed by the pipeline-identity tag every spec already
# carries — so a group never drifts from the specs it selects.
DEPLOYMENT_GROUPS: dict[str, str] = {
    "data": TAG_DATA_PIPELINE,
    "memory": TAG_MEMORY_PIPELINE,
}

_DEPLOYMENT_SPECS: list[_DeploymentSpec] = [
    _DeploymentSpec(
        data_etl_coordinator,
        "data-etl-coordinator",
        "apps/memory/src/tree/data/offline_pipeline.py:data_etl_coordinator",
        TAGS_DATA_OFFLINE,
        cron=_SCHEDULED_INGEST_CRON,
        schedule_parameters={"source_files": ["sources/listen.yaml"]},
    ),
    _DeploymentSpec(
        data_etl_worker,
        "data-etl-worker",
        "apps/memory/src/tree/data/offline_pipeline.py:data_etl_worker",
        TAGS_DATA_OFFLINE,
    ),
    _DeploymentSpec(
        memory_extract_etl_coordinator,
        "memory-extract-etl-coordinator",
        "apps/memory/src/tree/memory/extraction/pipeline.py:memory_extract_etl_coordinator",
        TAGS_EXTRACTION,
    ),
    _DeploymentSpec(
        memory_extract_etl_worker,
        "memory-extract-etl-worker",
        "apps/memory/src/tree/memory/extraction/pipeline.py:memory_extract_etl_worker",
        TAGS_EXTRACTION,
    ),
    _DeploymentSpec(
        memory_indexing,
        "memory-indexing-etl",
        "apps/memory/src/tree/memory/indexing/pipeline.py:memory_indexing",
        TAGS_INDEXING,
    ),
    # --- Optional (beyond the Prefect Cloud free-tier 5; gated by prefect.deploy_optional) ---
    # Realtime ingest. Where it is NOT registered, `dispatch_online_ingest` runs
    # the same flow in-process — so free-tier prod keeps working, synchronously.
    # ponytail: shares serve(limit) admission with backfill fan-outs; give it a
    # dedicated work queue if interactive ingest ever starves behind a backfill.
    _DeploymentSpec(
        etl_online,
        "etl-online",
        "apps/memory/src/tree/online.py:etl_online",
        TAGS_ETL_ONLINE,
        optional=True,
    ),
    # Offline end-to-end (data ingest → extraction → index) in one flow run.
    # ponytail: once off the free-tier cap, move _SCHEDULED_INGEST_CRON (+ its
    # listen.yaml parameters) from data-etl-coordinator onto THIS spec so the
    # nightly run extracts what it ingests instead of leaving documents pending.
    _DeploymentSpec(
        etl_offline,
        "etl-offline",
        "apps/memory/src/tree/offline.py:etl_offline",
        TAGS_ETL_OFFLINE,
        optional=True,
    ),
    _DeploymentSpec(
        dream_consolidation_all_users,
        "dream-consolidation-all-users",
        "apps/memory/src/tree/memory/consolidation/dream.py:dream_consolidation_all_users",
        ["memory-pipeline", "dream", "consolidation"],
        cron=app_config.dream.cron,
        optional=True,
    ),
]


def _active_deployment_specs(groups: tuple[str, ...] = ()) -> list[_DeploymentSpec]:
    """The deployments to register: the core 5 plus the optional ones when enabled.

    Reads ``app_config.prefect.deploy_optional`` at call time so the gate honours
    YAML, the ``TREE_PREFECT__DEPLOY_OPTIONAL`` env override, and test patches.

    ``groups`` narrows the set to whole pipelines by their identity tag —
    ``("data",)`` keeps the two ``data-pipeline`` specs, ``("memory",)`` keeps
    extraction + indexing (+ dream). Empty (the default) deploys everything.
    """

    deploy_optional = app_config.prefect.deploy_optional
    specs = [s for s in _DEPLOYMENT_SPECS if deploy_optional or not s.optional]
    if not groups:
        return specs
    unknown = set(groups) - set(DEPLOYMENT_GROUPS)
    if unknown:
        raise ValueError(
            f"Unknown deployment group(s): {sorted(unknown)}. "
            f"Valid: {sorted(DEPLOYMENT_GROUPS)}"
        )
    wanted = {DEPLOYMENT_GROUPS[g] for g in groups}
    return [s for s in specs if wanted & set(s.tags)]


def build_deployments() -> list:
    """Build the LOCAL-serve ``RunnerDeployment`` set (local code, no work pool).

    Consumed by :func:`serve_deployments` (``make memory-serve-workflows``). The
    deployments run from local code via the in-process runner — the dev loop,
    pointed at the local Prefect server (``make local-start``).
    """

    return [
        spec.flow.to_deployment(
            name=spec.name, tags=spec.tags, schedules=spec.schedules()
        )
        for spec in _active_deployment_specs()
    ]


def serve_deployments(limit: int) -> None:
    """Register and serve every workflow deployment with admission control.

    ``limit`` is forwarded to ``prefect.serve`` — admission control (ADR-002 §4)
    capping concurrent flow runs near ``concurrency.voyage_rpm`` so we never admit
    more runs than the shared embed budget can feed. Configures Opik once at
    startup (no-op without ``OPIK_API_KEY``).
    """

    configure_opik()
    serve(*build_deployments(), limit=limit)


def deploy_cloud_pipelines(
    *,
    work_pool_name: str,
    git_ref: str,
    job_env: dict[str, str],
    groups: tuple[str, ...] = (),
) -> list[str]:
    """Deploy every pipeline to Prefect Cloud, bound to a Managed work pool.

    Flow code is pulled from :data:`GIT_URL` at ``git_ref`` (a branch like ``main``
    or a commit SHA) using the GitHub PAT in the :data:`PAT_BLOCK_NAME` Secret
    block; :class:`_GitRepoWithPipInstall` adds a ``pip install`` pull step so the
    managed run can import ``tree``. ``job_env`` is the managed-run environment
    (``job_variables.env``) — typically :func:`managed_env_templates`, whose
    secret/config values are ``{{ prefect.blocks.secret.* }}`` /
    ``{{ prefect.variables.* }}`` references resolved at run time. So this carries
    no raw secrets and the CD path needs only the Prefect API creds. ``groups``
    (see :data:`DEPLOYMENT_GROUPS`) narrows the deploy to one pipeline family;
    empty deploys all. Returns the deployment ids.

    Synchronous: all the Prefect ``Flow``/``Secret`` helpers are sync in 3.6.
    """

    source = _GitRepoWithPipInstall(
        url=GIT_URL,
        **_git_ref_kwarg(git_ref),
        credentials={"access_token": Secret.load(PAT_BLOCK_NAME)},
    )
    job_variables = {"env": job_env, "image": MANAGED_IMAGE}

    deployment_ids: list[str] = []
    for spec in _active_deployment_specs(groups):
        flow = spec.flow.from_source(source=source, entrypoint=spec.entrypoint)
        deployment_id = flow.deploy(
            name=spec.name,
            work_pool_name=work_pool_name,
            tags=spec.tags,
            schedules=spec.schedules(),
            job_variables=job_variables,
            build=False,
            push=False,
            ignore_warnings=True,
        )
        deployment_ids.append(str(deployment_id))
    return deployment_ids


# Runtime config the managed flows need in their environment, mapped to a Prefect
# store seeded by ``prefect_pipelines_setup.py up`` from the operator's env.
# :func:`managed_env_templates` turns each into a ``{{ ... }}`` reference resolved
# at run time, so the deployment (and the CD path that re-applies it) never carries
# a raw value. ``(store_name, ENV_VAR, is_secret)`` — secrets → ``Secret`` blocks
# (hyphenated names); non-secret config → ``Variable``s (lowercase/underscore).
RUNTIME_CONFIG: list[tuple[str, str, bool]] = [
    ("tree-mongo-password", "MONGO_INITDB_ROOT_PASSWORD", True),
    ("tree-google-api-key", "GOOGLE_API_KEY", True),
    ("tree-voyage-api-key", "VOYAGE_API_KEY", True),
    ("tree-brightdata-api-key", "BRIGHTDATA_API_KEY", True),
    ("tree-opik-api-key", "OPIK_API_KEY", True),
    ("tree_mongo_scheme", "MONGO_SCHEME", False),
    ("tree_mongo_host", "MONGO_HOST", False),
    ("tree_mongo_port", "MONGO_PORT", False),
    ("tree_mongo_username", "MONGO_INITDB_ROOT_USERNAME", False),
    ("tree_mongo_database", "MONGO_INITDB_DATABASE", False),
    ("tree_brightdata_unlocker_zone", "BRIGHTDATA_UNLOCKER_ZONE", False),
    ("tree_brightdata_serp_zone", "BRIGHTDATA_SERP_ZONE", False),
    ("tree_opik_workspace", "OPIK_WORKSPACE", False),
    ("tree_opik_project_name", "OPIK_PROJECT_NAME", False),
]


def managed_env_templates() -> dict[str, str]:
    """The ``job_variables.env`` for managed runs — a STATIC template mapping.

    Every secret/config value is a ``{{ prefect.blocks.secret.* }}`` or
    ``{{ prefect.variables.* }}`` reference Prefect resolves at run time, so this
    reads NO environment and carries NO raw secrets — both ``up`` and the CD path
    apply the identical mapping. (No ``PYTHONPATH`` needed: ``tree`` is
    pip-installed by :func:`worker_pip_packages`, not imported off the clone.)
    """

    env: dict[str, str] = {"MCP_SKIP_INDEX_BOOTSTRAP": "true"}
    for store_name, var, is_secret in RUNTIME_CONFIG:
        if is_secret:
            env[var] = "{{ prefect.blocks.secret.%s }}" % store_name
        else:
            env[var] = "{{ prefect.variables.%s }}" % store_name
    return env


def deployment_full_names(groups: tuple[str, ...] = ()) -> list[str]:
    """``flow_name/deployment_name`` for each managed deployment.

    The id Prefect addresses a deployment by — used by
    ``deploy/prefect_pipelines_setup.py`` to read/delete them in ``status`` /
    ``down`` without re-listing the topology. ``groups`` narrows the set exactly
    as in :func:`deploy_cloud_pipelines`, so all four verbs share one selector.
    """

    return [
        f"{spec.flow.name}/{spec.name}" for spec in _active_deployment_specs(groups)
    ]


def _git_ref_kwarg(git_ref: str) -> dict[str, str]:
    """Route ``git_ref`` to ``commit_sha`` (40-char hex) or ``branch`` for
    :class:`GitRepository` — a clone can pin a commit OR track a branch."""

    is_sha = len(git_ref) == 40 and all(
        c in "0123456789abcdef" for c in git_ref.lower()
    )
    return {"commit_sha": git_ref} if is_sha else {"branch": git_ref}


if __name__ == "__main__":
    serve_deployments(app_config.concurrency.runner_global_limit)
