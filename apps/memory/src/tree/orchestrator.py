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
        memory-extract-etl-orchestrator/memory-extract-etl-orchestrator \\
        -p user_id=507f1f77bcf86cd799439011
"""

from dataclasses import dataclass

from prefect import Flow, serve
from prefect.blocks.system import Secret
from prefect.runner.storage import GitRepository

from tree.config.app_config import app_config
from tree.data.pipeline import data_etl_orchestrator, data_etl_worker
from tree.memory.extraction.pipeline import (
    memory_extract_etl_orchestrator,
    memory_extract_etl_worker,
)
from tree.memory.indexing.pipeline import memory_indexing
from tree.observability import configure_opik

# --- [Prefect Cloud free-tier cap: 5 deployments] --------------------------
# The free tier allows only 5 deployments per workspace, so the five flows below
# are temporarily not served/deployed. Re-enable (uncomment these imports AND add
# the matching ``_DeploymentSpec`` entries) once the Cloud plan is upgraded.
# from tree.data.conversation_pipeline import ingest_conversation
# from tree.data.file_pipeline import ingest_file
# from tree.data.youtube.youtube_rss_pipeline import ingest_youtube_rss_feed_batch
# from tree.data.youtube.youtube_video_pipeline import ingest_youtube_video_batch
# from tree.memory.consolidation.dream import dream_consolidation_all_users
# ---------------------------------------------------------------------------

# Cloud managed-pool defaults (provisioned by deploy/prefect_pipelines_setup.py).
GIT_URL = "https://github.com/iusztinpaul/building-agentic-systems.git"
MANAGED_WORK_POOL = "tree-managed"
# Secret block holding the GitHub PAT Prefect uses to clone this private repo.
PAT_BLOCK_NAME = "tree-github-pat"

# Slim runtime deps installed per-run by Prefect Managed Execution: the
# ``apps/memory/pyproject.toml`` ``[project.dependencies]`` MINUS
# ``sentence-transformers`` (PyTorch) and ``modal`` — the cloud flows embed via
# the Voyage API and reason via the Gemini API, never local torch/Modal models,
# so dropping them keeps the per-run install fast. KEEP IN SYNC with pyproject;
# ``datasets`` stays because ``huggingface_dataset`` is a configured source.
WORKER_PIP_PACKAGES: list[str] = [
    "beanie>=2.0.1",
    "pymongo>=4.16.0",
    "feedparser>=6.0",
    "httpx>=0.28",
    "beautifulsoup4>=4.13",
    "pydantic-settings>=2.9",
    "langchain-mongodb>=0.5",
    "langchain-google-genai>=2.1",
    "langchain-text-splitters>=0.3",
    "tiktoken>=0.12.0",
    "click>=8.0",
    "pyvis>=0.3",
    "networkx>=3.0",
    "google-genai>=1.65.0",
    "pyyaml>=6.0",
    "openai>=1.0",
    "datasets>=3.0",
    "fastmcp[apps]>=3.1.0",
    "youtube-transcript-api>=1.2.4",
    "rapidfuzz>=3",
    "opik>=2.0.60",
]


@dataclass(frozen=True)
class _DeploymentSpec:
    """One deployment's topology — shared by the local-serve and cloud paths.

    ``flow`` is the imported flow object (used for local ``to_deployment`` and as
    the ``from_source`` accessor). ``entrypoint`` is the repo-relative
    ``path:function`` Prefect's managed worker loads after cloning the repo.
    """

    flow: Flow
    name: str
    entrypoint: str
    tags: list[str]
    cron: str | None = None


# The deployment topology. Operators trigger the ORCHESTRATORs:
# - data-etl-orchestrator: partitions the configured ``sources:`` into shards and
#   dispatches one ``data-etl-worker`` per shard (no trailing index).
# - memory-extract-etl-orchestrator: shards the user's pending docs across
#   ``memory-extract-etl-worker`` runs, then fires one ``memory-indexing-etl``.
# The workers / indexing flows are also triggerable directly.
_DEPLOYMENT_SPECS: list[_DeploymentSpec] = [
    _DeploymentSpec(
        data_etl_orchestrator,
        "data-etl-orchestrator",
        "apps/memory/src/tree/data/pipeline.py:data_etl_orchestrator",
        ["data-pipeline", "orchestrator"],
    ),
    _DeploymentSpec(
        data_etl_worker,
        "data-etl-worker",
        "apps/memory/src/tree/data/pipeline.py:data_etl_worker",
        ["data-pipeline", "worker"],
    ),
    _DeploymentSpec(
        memory_extract_etl_orchestrator,
        "memory-extract-etl-orchestrator",
        "apps/memory/src/tree/memory/extraction/pipeline.py:memory_extract_etl_orchestrator",
        ["memory-pipeline", "extraction", "orchestrator"],
    ),
    _DeploymentSpec(
        memory_extract_etl_worker,
        "memory-extract-etl-worker",
        "apps/memory/src/tree/memory/extraction/pipeline.py:memory_extract_etl_worker",
        ["memory-pipeline", "extraction", "worker"],
    ),
    _DeploymentSpec(
        memory_indexing,
        "memory-indexing-etl",
        "apps/memory/src/tree/memory/indexing/pipeline.py:memory_indexing",
        ["memory-pipeline", "indexing"],
    ),
]


def build_deployments() -> list:
    """Build the LOCAL-serve ``RunnerDeployment`` set (local code, no work pool).

    Consumed by :func:`serve_deployments` (``make memory-serve-workflows``). The
    deployments run from local code via the in-process runner — the dev loop,
    pointed at the local Prefect server (``make local-start``).
    """

    return [
        spec.flow.to_deployment(name=spec.name, tags=spec.tags, cron=spec.cron)
        for spec in _DEPLOYMENT_SPECS
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
    *, work_pool_name: str, git_ref: str, job_env: dict[str, str]
) -> list[str]:
    """Deploy every pipeline to Prefect Cloud, bound to a Managed work pool.

    Flow code is pulled from :data:`GIT_URL` at ``git_ref`` (a branch like
    ``main`` or a commit SHA) using the GitHub PAT in the :data:`PAT_BLOCK_NAME`
    Secret block. ``job_env`` is the managed-run environment (``job_variables.env``)
    — typically :func:`prefect_pipelines_setup.managed_env_templates`, whose
    values are ``{{ prefect.blocks.secret.* }}`` / ``{{ prefect.variables.* }}``
    references resolved at run time (so this carries no raw secrets and the CD
    path needs none). Returns the deployment ids.

    Synchronous: all the Prefect ``Flow``/``Secret`` helpers are sync in 3.6.
    """

    source = GitRepository(
        url=GIT_URL,
        **_git_ref_kwarg(git_ref),
        credentials={"access_token": Secret.load(PAT_BLOCK_NAME)},
    )
    job_variables = {"pip_packages": WORKER_PIP_PACKAGES, "env": job_env}

    deployment_ids: list[str] = []
    for spec in _DEPLOYMENT_SPECS:
        flow = spec.flow.from_source(source=source, entrypoint=spec.entrypoint)
        deployment_id = flow.deploy(
            name=spec.name,
            work_pool_name=work_pool_name,
            tags=spec.tags,
            cron=spec.cron,
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
    apply the identical mapping. ``PYTHONPATH`` makes the cloned src-layout
    ``tree`` package importable.
    """

    env: dict[str, str] = {
        "PYTHONPATH": "apps/memory/src",
        "MCP_SKIP_INDEX_BOOTSTRAP": "true",
    }
    for store_name, var, is_secret in RUNTIME_CONFIG:
        if is_secret:
            env[var] = "{{ prefect.blocks.secret.%s }}" % store_name
        else:
            env[var] = "{{ prefect.variables.%s }}" % store_name
    return env


def deployment_full_names() -> list[str]:
    """``flow_name/deployment_name`` for each managed deployment.

    The id Prefect addresses a deployment by — used by
    ``deploy/prefect_pipelines_setup.py`` to read/delete them in ``status`` /
    ``down`` without re-listing the topology.
    """

    return [f"{spec.flow.name}/{spec.name}" for spec in _DEPLOYMENT_SPECS]


def _git_ref_kwarg(git_ref: str) -> dict[str, str]:
    """Route ``git_ref`` to ``commit_sha`` (40-char hex) or ``branch`` for
    :class:`GitRepository` — a clone can pin a commit OR track a branch."""

    is_sha = len(git_ref) == 40 and all(
        c in "0123456789abcdef" for c in git_ref.lower()
    )
    return {"commit_sha": git_ref} if is_sha else {"branch": git_ref}


if __name__ == "__main__":
    serve_deployments(app_config.concurrency.runner_global_limit)
